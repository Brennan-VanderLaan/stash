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


_VALID_SOURCES = {"user_widget", "mcp"}


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
    page_html: str | None = None,
    console_log: str | None = None,
    focused_selector: str | None = None,
    scroll_x: int | None = None,
    scroll_y: int | None = None,
    page_title: str | None = None,
    color_scheme: str | None = None,
    client_timestamp: str | None = None,
    perf_timing: str | None = None,
    source: str = "user_widget",
) -> int:
    """Insert one feedback row.  Returns the new id.

    ``tenant_id`` + ``actor_email`` are optional because anonymous
    feedback paths (e.g., from a public share page) might land here
    without a resolved actor.  The route layer is the one that
    decides whether to allow that.

    The extended telemetry fields (``page_html`` through
    ``perf_timing``) are only populated when the user opts in via
    the "Capture this page" widget button.

    ``source`` distinguishes ingestion paths.  Default
    ``user_widget`` matches the historical floating-button submit.
    ``mcp`` flags rows created via the admin_create_feedback MCP
    tool — typically an agent walking a visual-sweep manifest.
    Operators can filter on this in /admin to keep automated
    findings from drowning real-user reports.
    """
    body = (body or "").strip()
    if not body:
        raise ValueError("feedback body is empty")
    if source not in _VALID_SOURCES:
        raise ValueError(f"unknown feedback source {source!r}")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO feedback "
            "(tenant_id, actor_email, body, screenshot, source_url, "
            " user_agent, viewport_w, viewport_h, "
            " page_html, console_log, focused_selector, "
            " scroll_x, scroll_y, page_title, color_scheme, "
            " client_timestamp, perf_timing, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, actor_email, body, screenshot, source_url,
             user_agent, viewport_w, viewport_h,
             page_html, console_log, focused_selector,
             scroll_x, scroll_y, page_title, color_scheme,
             client_timestamp, perf_timing, source),
        )
        feedback_id = cur.lastrowid
        conn.commit()
    _log.info(
        "feedback.create id=%s tenant_id=%s len=%d "
        "screenshot=%s page_html=%s source=%s",
        feedback_id, tenant_id, len(body),
        bool(screenshot), bool(page_html), source,
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


# ── Stars + leaderboard ────────────────────────────────────────────────
#
# Operator-side "done" on a feedback row earns the submitter a star —
# fake currency tracked by counting the rows.  No new column, no
# migration; the existing ``feedback.status='done' AND actor_email=X``
# query IS the star count.  Two helpers below back the user's /usage
# "your contributions" card (per-actor) and the /leaderboard page
# (cross-actor top-N, with an ignore list so the operator doesn't
# trophy themselves).


def stars_for_actor(actor_email: str) -> int:
    """Number of stars (= shipped feedback rows) credited to this
    email.  Cheap one-row query; no caching needed at app scale."""
    if not actor_email:
        return 0
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM feedback "
            "WHERE status = 'done' AND actor_email = ?",
            (actor_email,),
        ).fetchone()
    return row["n"] if row else 0


def list_for_actor(actor_email: str, *, limit: int = 50) -> list[dict]:
    """Every feedback row the actor has ever submitted, newest first.
    Used by /usage to surface "here's what you've sent in + where
    each item stands" so the submitter sees real status, not a
    black hole.  No tenant filter — feedback is keyed by actor
    email regardless of which tenant the submitter was in when
    they hit the widget."""
    if not actor_email:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT id, body, source_url, status, "
            "       created_at, resolved_at "
            "FROM feedback "
            "WHERE actor_email = ? "
            # ``id DESC`` as a tiebreaker keeps the ordering
            # deterministic for rows inserted within the same
            # second (CURRENT_TIMESTAMP only has 1-s resolution).
            "ORDER BY created_at DESC, id DESC "
            "LIMIT ?",
            (actor_email, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def leaderboard(
    *, exclude_emails: tuple[str, ...] = (), limit: int = 3,
) -> list[dict]:
    """Top contributors across the whole stash, ordered by shipped-
    feedback count.  ``exclude_emails`` is the ignore list — the
    operator's own email belongs here so they don't trophy
    themselves on their own platform.  Empty actor emails (legacy
    rows from before the actor middleware always populated this)
    are filtered server-side.

    Returns: ``[{"actor_email": str, "stars": int}, ...]`` sorted
    stars desc, capped at ``limit``.  Ties break alphabetically by
    email so the result is deterministic across page loads."""
    excluded = {e.lower() for e in exclude_emails if e}
    with db() as conn:
        rows = conn.execute(
            "SELECT actor_email, COUNT(*) AS stars "
            "  FROM feedback "
            " WHERE status = 'done' "
            "   AND actor_email IS NOT NULL "
            "   AND actor_email != '' "
            " GROUP BY actor_email "
            " ORDER BY COUNT(*) DESC, actor_email ASC"
        ).fetchall()
    out = []
    for r in rows:
        if r["actor_email"].lower() in excluded:
            continue
        out.append({"actor_email": r["actor_email"], "stars": r["stars"]})
        if len(out) >= limit:
            break
    return out
