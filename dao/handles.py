"""Public leaderboard handles — opt-in display names.

Stars (shipped feedback) are bound to ``actor_email`` in the
``feedback`` table.  The public ``/leaderboard`` page must not
expose those emails, so every email keeps a NULL handle until the
user explicitly opts in via /usage by picking a display name.

Operators can revoke a handle at any time (the user keeps their
stars but reverts to "Anonymous" on the public board).  The
revocation is audited; the row stays in place so we have an audit
trail of who picked what, when, and why it was nuked.

Validation:
* 2-24 characters
* ASCII letters, digits, hyphen, underscore
* Doesn't start with hyphen or underscore (those positions are
  reserved so we can prefix internal markers later without
  colliding with a real user-set handle)
* Uniqueness is case-INSENSITIVE — "brennan_v" and "Brennan_V"
  conflict; the case the user typed is preserved for display.
"""

from __future__ import annotations

import re
from typing import Optional

import obs
from dao._base import Actor, NotFoundError, db, require_operator


_log = obs.get_logger("dao.handles")


HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,23}$")


class HandleError(ValueError):
    """Validation / uniqueness failure when setting a handle.
    Carries a user-readable ``reason`` field the form can echo
    back to the submitter."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _validate(handle: str) -> str:
    handle = (handle or "").strip()
    if not handle:
        raise HandleError("Handle can't be blank.")
    if len(handle) < 2:
        raise HandleError("Handle must be at least 2 characters.")
    if len(handle) > 24:
        raise HandleError("Handle must be 24 characters or fewer.")
    if not HANDLE_PATTERN.match(handle):
        raise HandleError(
            "Handle can use letters, digits, hyphen, and underscore "
            "only, and must start with a letter or digit."
        )
    return handle


def get_handle(actor_email: str) -> Optional[dict]:
    """Return the handle row for an email, or None if there isn't
    one.  Returns the row even when revoked — callers decide
    whether to treat a revoked handle as "active" (operators
    care; the public board does not)."""
    if not actor_email:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT actor_email, handle, handle_lower, created_at, "
            "       updated_at, revoked_at, revoked_by, revoked_reason "
            "FROM feedback_handles WHERE actor_email = ?",
            (actor_email,),
        ).fetchone()
    return dict(row) if row else None


def active_handle(actor_email: str) -> Optional[str]:
    """Public-display helper.  Returns the handle string if the
    email has set one AND it hasn't been revoked; None otherwise.
    The leaderboard renderer uses this to decide between the
    handle and "Anonymous"."""
    row = get_handle(actor_email)
    if row and row["handle"] and row["revoked_at"] is None:
        return row["handle"]
    return None


def set_handle(actor: Actor, handle: str) -> dict:
    """Set or update the actor's own handle.  Upsert keyed by
    email.  Re-setting after a revocation clears the revoke
    columns (the user picked something new after the operator
    nuked the offensive one).

    Raises :class:`HandleError` for validation + uniqueness
    failures — the form route catches these and re-renders the
    page with the reason inline.  ``actor.email`` must be set;
    bearer-token / share-only actors can't pick handles."""
    if not actor.email:
        raise HandleError("Sign in with an email account first.")
    clean = _validate(handle)
    lower = clean.lower()
    with db() as conn:
        existing = conn.execute(
            "SELECT actor_email FROM feedback_handles "
            "WHERE handle_lower = ? "
            "  AND revoked_at IS NULL "
            "  AND actor_email != ?",
            (lower, actor.email),
        ).fetchone()
        if existing is not None:
            raise HandleError(
                f"{clean!r} is already taken — pick something else."
            )
        # Upsert.  ON CONFLICT clears the revoked_* columns so a
        # user who comes back after a revocation with a fresh
        # acceptable handle picks up a clean slate.
        conn.execute(
            "INSERT INTO feedback_handles "
            "  (actor_email, handle, handle_lower, "
            "   updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(actor_email) DO UPDATE SET "
            "  handle = excluded.handle, "
            "  handle_lower = excluded.handle_lower, "
            "  updated_at = CURRENT_TIMESTAMP, "
            "  revoked_at = NULL, "
            "  revoked_by = NULL, "
            "  revoked_reason = NULL",
            (actor.email, clean, lower),
        )
        obs.write_audit(
            conn, tenant_id=None, actor_email=actor.email,
            action="handle.set",
            target_kind="handle", target_id=None,
            metadata={"handle": clean},
        )
        conn.commit()
    _log.info("handle.set email=%s handle=%r", actor.email, clean)
    return get_handle(actor.email) or {}


def revoke_handle(actor: Actor, target_email: str, reason: str = "") -> dict:
    """Operator-only revocation.  Marks the handle revoked but
    leaves the row in place so the audit trail survives.  The
    user keeps their stars and can pick a new handle later."""
    require_operator(actor)
    with db() as conn:
        row = conn.execute(
            "SELECT handle FROM feedback_handles WHERE actor_email = ?",
            (target_email,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"no handle for {target_email!r}")
        conn.execute(
            "UPDATE feedback_handles SET "
            "  revoked_at = CURRENT_TIMESTAMP, "
            "  revoked_by = ?, "
            "  revoked_reason = ? "
            "WHERE actor_email = ?",
            (actor.email, (reason or "")[:200], target_email),
        )
        obs.write_audit(
            conn, tenant_id=None, actor_email=actor.email,
            action="handle.revoke",
            target_kind="handle", target_id=None,
            metadata={
                "target_email": target_email,
                "old_handle": row["handle"],
                "reason": (reason or "")[:200],
            },
        )
        conn.commit()
    _log.warning(
        "handle.revoke target=%s old_handle=%r by=%s reason=%r",
        target_email, row["handle"], actor.email, reason,
    )
    return get_handle(target_email) or {}


def list_all_for_operator(actor: Actor) -> list[dict]:
    """Operator view: every handle row, active + revoked, for the
    /admin moderation panel.  Sorted active-first, alphabetical
    within."""
    require_operator(actor)
    with db() as conn:
        rows = conn.execute(
            "SELECT actor_email, handle, created_at, updated_at, "
            "       revoked_at, revoked_by, revoked_reason "
            "FROM feedback_handles "
            "ORDER BY (revoked_at IS NULL) DESC, handle_lower"
        ).fetchall()
    return [dict(r) for r in rows]
