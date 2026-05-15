"""Per-tenant API tokens for the /api/v1 router (phase 11).

Token shape: ``stash_<43 url-safe chars>``.  43 chars of urlsafe
b64 ≈ 32 bytes of entropy — comfortably above the brute-force cap
for reasonable token lifetimes.  The ``stash_`` prefix is for
greppability in logs, terminals, and screenshots; spec § "API
tokens" doesn't require it but it costs nothing.

Storage: only the SHA-256 hash lands in the DB.  The plaintext is
shown exactly once at mint time and never persisted, so a database
leak surfaces dead tokens (an attacker would need the live KEK to
do anything anyway, but token-level isolation is cheap).

Authentication: :func:`authenticate_token` takes the bearer string,
hashes it, and looks up the active row.  On success it bumps
``last_used_at`` (best-effort; we don't fail auth if the bump
fails) and returns enough context to build an :class:`Actor`.

Audit log: ``api_token.create`` and ``api_token.revoke`` rows land
on every state transition for the same operator-visibility
reasons as invites + shares.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Optional

import obs
from dao._base import (
    Actor,
    ForbiddenError,
    NotFoundError,
    db,
    require_role,
)


_log = obs.get_logger("dao.api_tokens")


_TOKEN_PREFIX = "stash_"
_TOKEN_BODY_BYTES = 32  # → 43 chars of urlsafe-b64


def _hash(token: str) -> str:
    """sha256 of the bearer plaintext.  Constant-time comparison
    isn't needed at the DAO layer — the lookup is by exact-match
    on a hashed column, and the hash is never returned to the
    user — but a future ``constant_time.compare`` pass when we
    add scoped lookups would be welcome."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── Mint ────────────────────────────────────────────────────────────


def create(actor: Actor, name: str, role: str = "maintainer") -> dict:
    """Mint a new API token in the actor's tenant.  Returns
    ``{"id": ..., "name": ..., "plaintext": ..., "role": ...}``.

    The ``plaintext`` field is the only time the user ever sees the
    token bytes; the route's job is to render it once + never store
    it server-side beyond the SHA-256 hash.

    Maintainer-only (the route also gates).  ``name`` is a short
    human label so a user can tell tokens apart on the listing
    (e.g. "MCP server", "ad-hoc CLI")."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise ForbiddenError(f"{actor.email} has no active tenant")
    name = name.strip()
    if not name:
        raise ValueError("token name required")
    # Generous cap that covers anything human-typed (MCP server,
    # Sister's iPad, etc.) without letting a malicious caller bloat
    # the DB or break the listing UI with a 10MB string.
    if len(name) > 100:
        raise ValueError("token name must be 100 characters or fewer")
    if role not in ("maintainer", "readonly"):
        raise ValueError(f"unknown role {role!r}")

    plaintext = _TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BODY_BYTES)
    token_hash = _hash(plaintext)
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO api_tokens "
            "(tenant_id, token_hash, name, role, created_by_email) "
            "VALUES (?, ?, ?, ?, ?)",
            (actor.tenant_id, token_hash, name, role, actor.email),
        )
        token_id = cur.lastrowid
        obs.write_audit(
            conn,
            tenant_id=actor.tenant_id,
            actor_email=actor.email,
            action="api_token.create",
            target_kind="api_token",
            target_id=token_id,
            metadata={"name": name, "role": role},
        )
        conn.commit()
    _log.info("api_token.create id=%s name=%r role=%s",
              token_id, name, role)
    return {
        "id": token_id,
        "name": name,
        "role": role,
        "plaintext": plaintext,
    }


# ── Read ────────────────────────────────────────────────────────────


def list_for_tenant(actor: Actor) -> list[dict]:
    """Active tokens (un-revoked) for the actor's tenant.
    Maintainer-only.  Plaintext never appears here — only the
    metadata + the ``last_used_at`` watermark."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, role, created_at, last_used_at, "
            "       created_by_email "
            "FROM api_tokens "
            "WHERE tenant_id = ? AND revoked_at IS NULL "
            "ORDER BY created_at DESC",
            (actor.tenant_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Auth ────────────────────────────────────────────────────────────


def authenticate(
    plaintext: str,
    *,
    expected_audience: str | None = None,
) -> Optional[dict]:
    """Resolve a bearer plaintext to ``{"id": ..., "tenant_id": ...,
    "role": ..., "name": ...}``, or None if the token is unknown,
    revoked, suspended, expired, or malformed.

    Audience binding (OAuth 2.1 §RS validation): when
    ``expected_audience`` is given (set by the route handling
    /mcp), reject any token whose ``audience`` column is non-NULL
    and doesn't match.  Tokens with NULL audience (the original
    user-minted phase-11 surface) pass — they predate the OAuth
    flow and are not bound to a resource.

    Expiry: tokens with ``expires_at`` set are rejected after
    that timestamp.  NULL = no expiry (existing user-minted
    tokens stay long-lived).

    Bumps ``last_used_at`` as a side effect when auth succeeds —
    best-effort, we don't fail the request if the bump fails (the
    column is for forensics + the /usage table, not authorization).
    """
    if not plaintext or not plaintext.startswith(_TOKEN_PREFIX):
        return None
    token_hash = _hash(plaintext)
    now = _now_iso()
    with db() as conn:
        row = conn.execute(
            "SELECT id, tenant_id, role, name, audience, expires_at, "
            "       oauth_client_id, created_by_email "
            "FROM api_tokens "
            "WHERE token_hash = ? "
            "  AND revoked_at IS NULL "
            "  AND suspended_at IS NULL "
            "  AND (expires_at IS NULL OR expires_at > ?)",
            (token_hash, now),
        ).fetchone()
        if row is None:
            return None
        # Audience check (OAuth 2.1 §5.2).  Tokens issued via the
        # OAuth flow carry a non-NULL audience; legacy user-minted
        # tokens don't and stay valid for any tenant-scoped path.
        if expected_audience and row["audience"]:
            if row["audience"].rstrip("/") != expected_audience.rstrip("/"):
                _log.warning(
                    "api_token.audience_mismatch id=%s "
                    "presented_audience=%s expected=%s",
                    row["id"], row["audience"], expected_audience,
                )
                return None
        # Best-effort timestamp bump.
        try:
            conn.execute(
                "UPDATE api_tokens SET last_used_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (row["id"],),
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            _log.warning("api_token.last_used_bump_failed id=%s err=%s",
                         row["id"], exc)
    return dict(row)


def _now_iso() -> str:
    """SQLite-CURRENT_TIMESTAMP-shaped string so the
    ``expires_at > ?`` comparison doesn't trip on the lex
    difference between ``' '`` and ``'T'``."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def lookup_by_plaintext(plaintext: str) -> Optional[dict]:
    """Look up an active or inactive token by plaintext.  Used by
    the auto-revoke-on-leak path: when the middleware spots a token
    in the wrong place (URL, non-Authorization header, plain HTTP),
    it needs the row id to call ``revoke_with_reason`` even if the
    token is already revoked/suspended.

    Returns the row regardless of state — caller decides what to do
    with it.  Don't expose this on the auth path; it's the
    forensics surface."""
    if not plaintext or not plaintext.startswith(_TOKEN_PREFIX):
        return None
    token_hash = _hash(plaintext)
    with db() as conn:
        row = conn.execute(
            "SELECT id, tenant_id, role, name, revoked_at, suspended_at, "
            "       revoked_reason "
            "FROM api_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
    return dict(row) if row else None


# ── Revoke ──────────────────────────────────────────────────────────


def revoke(actor: Actor, token_id: int, *,
           reason: str = "manual") -> None:
    """Revoke a token by id.  Maintainer of the granting tenant
    only.  Idempotent: already-revoked or already-gone returns
    NotFoundError so the route can 404 cleanly.

    ``reason`` is a short tag stored alongside ``revoked_at`` so an
    operator audit view can tell deliberate revokes from the
    automatic ones (``seen_over_http``, ``leaked_in_url``,
    ``operator_revoke``).  Default ``manual`` covers the plain
    /usage revoke button."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"api_token {token_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT name FROM api_tokens "
            "WHERE id = ? AND tenant_id = ? AND revoked_at IS NULL",
            (token_id, actor.tenant_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"api_token {token_id}")
        conn.execute(
            "UPDATE api_tokens SET revoked_at = CURRENT_TIMESTAMP, "
            "                       revoked_reason = ? "
            "WHERE id = ?",
            (reason, token_id),
        )
        obs.write_audit(
            conn,
            tenant_id=actor.tenant_id,
            actor_email=actor.email,
            action="api_token.revoke",
            target_kind="api_token",
            target_id=token_id,
            metadata={"name": row["name"], "reason": reason},
        )
        conn.commit()
    _log.info("api_token.revoke id=%s name=%r reason=%s",
              token_id, row["name"], reason)


def revoke_for_leak(token_id: int, reason: str,
                    *, request_path: str = "") -> None:
    """Operator-bypass revoke — used by the middleware's automatic
    revoke-on-leak path.  No actor required (the middleware doesn't
    yet have one resolved when this fires).  ``reason`` should be
    one of:

    * ``seen_over_http`` — bearer travelled over plaintext HTTP.
    * ``leaked_in_url`` — token-shaped string found in the URL
      query string.
    * ``leaked_in_header`` — token-shaped string found in a
      non-Authorization header.

    The path is recorded in the audit metadata so a follow-up
    investigation can find the offending request without grepping
    the request log."""
    with db() as conn:
        row = conn.execute(
            "SELECT name, tenant_id FROM api_tokens "
            "WHERE id = ? AND revoked_at IS NULL",
            (token_id,),
        ).fetchone()
        if row is None:
            return
        conn.execute(
            "UPDATE api_tokens SET revoked_at = CURRENT_TIMESTAMP, "
            "                       revoked_reason = ? "
            "WHERE id = ?",
            (reason, token_id),
        )
        obs.write_audit(
            conn,
            tenant_id=row["tenant_id"],
            actor_email="<system>",
            action="api_token.auto_revoke",
            target_kind="api_token",
            target_id=token_id,
            metadata={"name": row["name"], "reason": reason,
                      "path": request_path[:200]},
        )
        conn.commit()
    _log.warning(
        "api_token.auto_revoke id=%s name=%r reason=%s path=%s",
        token_id, row["name"], reason, request_path[:120],
    )


# ── Operator surface (suspend/resume) ───────────────────────────────


def list_all_for_operator(actor: Actor) -> list[dict]:
    """Cross-tenant token roster for the /admin token panel.
    Operator-only.  Returns enough metadata to render the table:
    id + tenant + name + role + lifecycle state + last_used_at.
    Plaintext is never on this surface (it never appears anywhere
    after mint); ``token_hash`` would be useful for forensics but
    surfaces the hash to the operator UI which we'd rather not
    log/screenshot, so it's omitted too."""
    from dao._base import require_operator
    require_operator(actor)
    with db() as conn:
        rows = conn.execute(
            "SELECT t.id, t.tenant_id, t.name, t.role, "
            "       t.created_at, t.created_by_email, "
            "       t.last_used_at, t.revoked_at, t.revoked_reason, "
            "       t.suspended_at, te.name AS tenant_name "
            "FROM api_tokens t "
            "JOIN tenants te ON te.id = t.tenant_id "
            "ORDER BY t.created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def operator_revoke(actor: Actor, token_id: int) -> None:
    """Operator-driven revoke.  Records ``operator_revoke`` as the
    reason so a tenant maintainer reading their own /usage row
    can see the operator (not them, not an auto-revoke) killed
    the token."""
    from dao._base import require_operator
    require_operator(actor)
    with db() as conn:
        row = conn.execute(
            "SELECT name, tenant_id FROM api_tokens "
            "WHERE id = ? AND revoked_at IS NULL",
            (token_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"api_token {token_id}")
        conn.execute(
            "UPDATE api_tokens SET revoked_at = CURRENT_TIMESTAMP, "
            "                       revoked_reason = 'operator_revoke' "
            "WHERE id = ?",
            (token_id,),
        )
        obs.write_audit(
            conn,
            tenant_id=row["tenant_id"],
            actor_email=actor.email,
            action="api_token.operator_revoke",
            target_kind="api_token",
            target_id=token_id,
            metadata={"name": row["name"]},
        )
        conn.commit()
    _log.warning("api_token.operator_revoke id=%s by=%s name=%r",
                 token_id, actor.email, row["name"])


def operator_suspend(actor: Actor, token_id: int) -> None:
    """Temporary pause — auth fails while ``suspended_at`` is
    non-null but a follow-up :func:`operator_resume` clears it.
    Use case: "I suspect this token might be compromised but
    I'm not sure" — pauses the bearer's access without losing
    the original mint metadata."""
    from dao._base import require_operator
    require_operator(actor)
    with db() as conn:
        row = conn.execute(
            "SELECT name, tenant_id, revoked_at FROM api_tokens "
            "WHERE id = ?",
            (token_id,),
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise NotFoundError(f"api_token {token_id}")
        conn.execute(
            "UPDATE api_tokens SET suspended_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND suspended_at IS NULL",
            (token_id,),
        )
        obs.write_audit(
            conn,
            tenant_id=row["tenant_id"],
            actor_email=actor.email,
            action="api_token.operator_suspend",
            target_kind="api_token",
            target_id=token_id,
            metadata={"name": row["name"]},
        )
        conn.commit()
    _log.warning("api_token.operator_suspend id=%s by=%s name=%r",
                 token_id, actor.email, row["name"])


def operator_resume(actor: Actor, token_id: int) -> None:
    """Inverse of :func:`operator_suspend`.  Clears ``suspended_at``
    so auth resumes.  Refuses to resume a fully-revoked token —
    operators have to mint a fresh one for that."""
    from dao._base import require_operator
    require_operator(actor)
    with db() as conn:
        row = conn.execute(
            "SELECT name, tenant_id, revoked_at FROM api_tokens "
            "WHERE id = ?",
            (token_id,),
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise NotFoundError(f"api_token {token_id}")
        conn.execute(
            "UPDATE api_tokens SET suspended_at = NULL "
            "WHERE id = ?",
            (token_id,),
        )
        obs.write_audit(
            conn,
            tenant_id=row["tenant_id"],
            actor_email=actor.email,
            action="api_token.operator_resume",
            target_kind="api_token",
            target_id=token_id,
            metadata={"name": row["name"]},
        )
        conn.commit()
    _log.info("api_token.operator_resume id=%s by=%s name=%r",
              token_id, actor.email, row["name"])
