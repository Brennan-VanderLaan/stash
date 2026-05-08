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


def authenticate(plaintext: str) -> Optional[dict]:
    """Resolve a bearer plaintext to ``{"id": ..., "tenant_id": ...,
    "role": ..., "name": ...}``, or None if the token is unknown,
    revoked, or malformed.

    Bumps ``last_used_at`` as a side effect when auth succeeds —
    best-effort, we don't fail the request if the bump fails (the
    column is for forensics + the /usage table, not authorization).

    Caller (the middleware) is responsible for translating None →
    401.  The DAO never raises here; auth failures are the rule
    not the exception (every browser hit on /api would otherwise
    raise + log noise)."""
    if not plaintext or not plaintext.startswith(_TOKEN_PREFIX):
        return None
    token_hash = _hash(plaintext)
    with db() as conn:
        row = conn.execute(
            "SELECT id, tenant_id, role, name "
            "FROM api_tokens "
            "WHERE token_hash = ? AND revoked_at IS NULL",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        # Best-effort timestamp bump.  Wrapped in its own try so a
        # tight WAL contention can't make a successful auth look
        # failed to the caller.
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


# ── Revoke ──────────────────────────────────────────────────────────


def revoke(actor: Actor, token_id: int) -> None:
    """Revoke a token by id.  Maintainer of the granting tenant
    only.  Idempotent: already-revoked or already-gone returns
    NotFoundError so the route can 404 cleanly."""
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
            "UPDATE api_tokens SET revoked_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (token_id,),
        )
        obs.write_audit(
            conn,
            tenant_id=actor.tenant_id,
            actor_email=actor.email,
            action="api_token.revoke",
            target_kind="api_token",
            target_id=token_id,
            metadata={"name": row["name"]},
        )
        conn.commit()
    _log.info("api_token.revoke id=%s name=%r", token_id, row["name"])
