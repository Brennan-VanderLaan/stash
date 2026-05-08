"""Ingest jobs — the per-photo state machine the /ingest page polls."""

from __future__ import annotations

import hashlib

from dao._base import Actor, NotFoundError, db, require_role


def list_active(actor: Actor) -> list[dict]:
    """Recent ingest jobs (excluding ``done`` because we hide those).
    Newest first, capped at 50 to keep the page small."""
    if actor.tenant_id is None:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM ingest_jobs "
            "WHERE tenant_id = ? AND status != 'done' "
            "ORDER BY created_at DESC LIMIT 50",
            (actor.tenant_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def fingerprint(actor: Actor) -> dict:
    """SHA-1 of the active jobs + has-active flag, for the ingest
    page's lightweight polling."""
    rows = list_active(actor)
    payload = "|".join(
        f"{j['id']}:{j['status']}:{j['item_count']}:{j['error']}" for j in rows
    )
    return {
        "fingerprint": hashlib.sha1(payload.encode()).hexdigest(),
        "has_active": any(j["status"] in ("pending", "processing") for j in rows),
    }


def create(
    actor: Actor,
    photo_filename: str,
    *,
    target_box_id: int | None = None,
) -> int:
    """Insert a fresh pending job for a just-uploaded photo.  Returns
    the job id so the caller can hand it to the background worker.

    ``target_box_id`` is the packing-session hint from /ingest's
    box picker — when set, the worker will write each pending_item
    with ``suggested_box_id = target_box_id`` so the sort queue
    pre-fills the box selection (very similar to the existing AI
    suggest flow).  We validate the box belongs to the actor's
    tenant here so a forged hidden form field can't pre-dispose
    items into another tenant's box.  ``None`` is the no-session
    default and means "no hint, run normal AI suggest path"."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        from dao._base import ForbiddenError
        raise ForbiddenError(f"{actor.email} has no active tenant")
    if target_box_id is not None:
        with db() as conn:
            row = conn.execute(
                "SELECT 1 FROM boxes WHERE id = ? AND tenant_id = ?",
                (target_box_id, actor.tenant_id),
            ).fetchone()
        if row is None:
            # Crash toward happy path: an invalid hint becomes "no
            # hint" rather than a 500.  The user still gets to sort
            # the items normally; only the pre-fill is lost.
            target_box_id = None
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO ingest_jobs (photo, status, tenant_id, target_box_id) "
            "VALUES (?, 'pending', ?, ?)",
            (photo_filename, actor.tenant_id, target_box_id),
        )
        conn.commit()
    return cur.lastrowid


def mark_processing(job_id: int) -> None:
    """Background-worker hook: flip the row to ``processing``.  No
    actor parameter — the worker runs inside the request that
    enqueued the job, but actually executes after the response.  By
    that point we've left the request scope; the row is still safe
    to update because the worker has the tenant_id captured at job
    creation."""
    with db() as conn:
        conn.execute(
            "UPDATE ingest_jobs SET status = 'processing' WHERE id = ?",
            (job_id,),
        )
        conn.commit()


def mark_done(job_id: int, item_count: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE ingest_jobs SET status = 'done', item_count = ?, "
            "completed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (item_count, job_id),
        )
        conn.commit()


def mark_failed(job_id: int, error: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE ingest_jobs SET status = 'failed', error = ?, "
            "completed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (error[:500], job_id),
        )
        conn.commit()


def insert_pending_item(
    job_tenant_id: int,
    *,
    name: str,
    description: str,
    photo: str,
    bbox: tuple[int | None, int | None, int | None, int | None],
    suggested_box_id: int | None = None,
) -> int:
    """Background-worker hook: insert a pending_item row keyed to the
    job's tenant.  Bypasses actor.role gating because the worker
    runs out-of-band — its role check happened when the operator
    submitted the upload.

    ``suggested_box_id`` is the packing-session hint — when set,
    pre-fills the sort queue's box selection so the user just
    confirms the auto-detected name + crop instead of also
    picking a box.  The column is shared with the AI suggest
    flow on purpose: they're both "where should this go" hints
    and the sort UI already renders ``suggested_box_id`` as the
    pre-filled selection."""
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO pending_items "
            "(name, description, photo, bbox_y_min, bbox_x_min, bbox_y_max, bbox_x_max, "
            " tenant_id, suggested_box_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, description, photo, *bbox, job_tenant_id, suggested_box_id),
        )
        conn.commit()
    return cur.lastrowid


def get_target_box_id(job_id: int) -> int | None:
    """Worker hook: read the packing-session hint stamped onto the
    job by ``create``.  No actor — the worker runs out-of-band, the
    role + tenant check already happened at create time."""
    with db() as conn:
        row = conn.execute(
            "SELECT target_box_id FROM ingest_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    return row["target_box_id"] if row else None


def get_for_retry(actor: Actor, job_id: int) -> dict:
    """Pull the bits the retry path needs.  Accepts ``failed`` or
    ``processing`` rows: ``processing`` is included because a
    worker that hung mid-call (e.g. a Gemini timeout the network
    layer never returned from) leaves the row stuck — Retry must
    be able to abandon-and-respawn it without forcing the user
    through a server restart."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"job {job_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT photo, tenant_id, target_box_id FROM ingest_jobs "
            "WHERE id = ? AND tenant_id = ? "
            "  AND status IN ('failed', 'processing')",
            (job_id, actor.tenant_id),
        ).fetchone()
    if row is None:
        raise NotFoundError(f"job {job_id} (or not retry-able)")
    return dict(row)


def reset_to_pending(actor: Actor, job_id: int) -> None:
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"job {job_id}")
    with db() as conn:
        conn.execute(
            "UPDATE ingest_jobs SET status = 'pending', error = NULL, "
            "completed_at = NULL WHERE id = ? AND tenant_id = ?",
            (job_id, actor.tenant_id),
        )
        conn.commit()


def dismiss(actor: Actor, job_id: int) -> None:
    """Drop a job from the list.  Accepts every terminal-or-stuck
    state: ``failed``, ``done``, AND ``processing``.  Including
    ``processing`` is the escape hatch for jobs whose worker
    hung — without it the row sits forever and the user has no
    UI affordance to clear it (they'd have to restart the server
    so the orphan-sweep on boot picks it up)."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"job {job_id}")
    with db() as conn:
        conn.execute(
            "DELETE FROM ingest_jobs "
            "WHERE id = ? AND tenant_id = ? "
            "  AND status IN ('failed', 'done', 'processing')",
            (job_id, actor.tenant_id),
        )
        conn.commit()
