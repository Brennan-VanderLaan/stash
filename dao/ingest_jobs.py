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


def create(actor: Actor, photo_filename: str) -> int:
    """Insert a fresh pending job for a just-uploaded photo.  Returns
    the job id so the caller can hand it to the background worker."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        from dao._base import ForbiddenError
        raise ForbiddenError(f"{actor.email} has no active tenant")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO ingest_jobs (photo, status, tenant_id) "
            "VALUES (?, 'pending', ?)",
            (photo_filename, actor.tenant_id),
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
) -> int:
    """Background-worker hook: insert a pending_item row keyed to the
    job's tenant.  Bypasses actor.role gating because the worker
    runs out-of-band — its role check happened when the operator
    submitted the upload."""
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO pending_items "
            "(name, description, photo, bbox_y_min, bbox_x_min, bbox_y_max, bbox_x_max, tenant_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, description, photo, *bbox, job_tenant_id),
        )
        conn.commit()
    return cur.lastrowid


def get_for_retry(actor: Actor, job_id: int) -> dict:
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"job {job_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT photo, tenant_id FROM ingest_jobs "
            "WHERE id = ? AND tenant_id = ? AND status = 'failed'",
            (job_id, actor.tenant_id),
        ).fetchone()
    if row is None:
        raise NotFoundError(f"job {job_id} (or not in failed state)")
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
    """Drop a failed-or-done job from the list."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"job {job_id}")
    with db() as conn:
        conn.execute(
            "DELETE FROM ingest_jobs "
            "WHERE id = ? AND tenant_id = ? AND status IN ('failed', 'done')",
            (job_id, actor.tenant_id),
        )
        conn.commit()
