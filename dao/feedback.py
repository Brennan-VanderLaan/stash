"""In-app feedback widget — submissions land here, operators triage
on /admin.

Spec: the floating "tell us what's wrong" button on every page POSTs
description + optional screenshot + context (URL, viewport).  This
DAO is the persistence layer; the widget JS + endpoints + /admin
queue live in app.py.

The screenshot is stored as a tenant-encrypted blob (same pipeline
as item photos) so cross-tenant leakage on disk is impossible.  The
``screenshot`` column holds the filename; reading the bytes goes
through the standard ``_read_encrypted`` path in app.py.
"""

from __future__ import annotations

import obs
from dao._base import Actor, NotFoundError, db


_log = obs.get_logger("dao.feedback")


_VALID_STATUSES = {"open", "accepted", "rejected", "done"}


def create(
    *,
    tenant_id: int | None,
    actor_email: str | None,
    body: str,
    screenshot: str | None = None,
    source_url: str | None = None,
    user_agent: str | None = None,
    viewport_w: int | None = None,
    viewport_h: int | None = None,
) -> int:
    """Insert one feedback row.  Returns the new id.

    ``tenant_id`` + ``actor_email`` are optional because anonymous
    feedback paths (e.g., from a public share page) might land here
    without a resolved actor.  The route layer is the one that
    decides whether to allow that.
    """
    body = (body or "").strip()
    if not body:
        raise ValueError("feedback body is empty")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO feedback "
            "(tenant_id, actor_email, body, screenshot, source_url, "
            " user_agent, viewport_w, viewport_h) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, actor_email, body, screenshot, source_url,
             user_agent, viewport_w, viewport_h),
        )
        feedback_id = cur.lastrowid
        conn.commit()
    _log.info(
        "feedback.create id=%s tenant_id=%s len=%d screenshot=%s",
        feedback_id, tenant_id, len(body), bool(screenshot),
    )
    return feedback_id


def list_for_operator(status: str | None = None, limit: int = 100) -> list[dict]:
    """Operator queue read — every feedback row across tenants, joined
    with the tenant name for display.  Filter by status when set
    (default returns everything so the operator can see the queue
    plus what's already been triaged)."""
    sql = (
        "SELECT f.*, t.name AS tenant_name "
        "FROM feedback f "
        "LEFT JOIN tenants t ON t.id = f.tenant_id "
    )
    params: tuple = ()
    if status and status in _VALID_STATUSES:
        sql += "WHERE f.status = ? "
        params = (status,)
    sql += "ORDER BY f.created_at DESC LIMIT ?"
    params = params + (max(1, min(limit, 500)),)
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get(feedback_id: int) -> dict:
    """Single feedback row.  404s if missing — no tenant scoping
    here because the operator panel is the only caller."""
    with db() as conn:
        row = conn.execute(
            "SELECT f.*, t.name AS tenant_name "
            "FROM feedback f "
            "LEFT JOIN tenants t ON t.id = f.tenant_id "
            "WHERE f.id = ?",
            (feedback_id,),
        ).fetchone()
    if row is None:
        raise NotFoundError(f"feedback {feedback_id}")
    return dict(row)


def set_status(
    feedback_id: int, status: str,
    *, operator_email: str, notes: str | None = None,
) -> dict:
    """Operator transitions a row from ``open`` → ``accepted`` /
    ``rejected`` / ``done``.  Stamps ``resolved_at`` + ``resolved_by``
    so the queue has a clear audit trail."""
    if status not in _VALID_STATUSES:
        raise ValueError(f"bad status: {status!r}")
    with db() as conn:
        cur = conn.execute(
            "UPDATE feedback SET status = ?, "
            "       resolved_at = CASE WHEN ? IN ('accepted', 'rejected', 'done') "
            "                           THEN CURRENT_TIMESTAMP ELSE NULL END, "
            "       resolved_by = CASE WHEN ? IN ('accepted', 'rejected', 'done') "
            "                           THEN ? ELSE NULL END, "
            "       operator_notes = COALESCE(?, operator_notes) "
            "WHERE id = ?",
            (status, status, status, operator_email, notes, feedback_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"feedback {feedback_id}")
        conn.commit()
    _log.info("feedback.status feedback_id=%s status=%s by=%s",
              feedback_id, status, operator_email)
    return get(feedback_id)


def queue_counts() -> dict[str, int]:
    """Per-status counts — used by /admin to surface the open queue
    size in a single number."""
    with db() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM feedback GROUP BY status"
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}
