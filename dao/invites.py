"""Tenant invites — single-use tokens that add an email to a tenant
at a chosen role.

Phase-5 surface, link-only flavour: the maintainer mints a token, the
URL ``${PUBLIC_URL}/invite/{token}`` is copy-pasted out-of-band (no
email yet), and the recipient signs in via oauth2-proxy and visits
the link.  Spec § "Sign-up + onboarding" covers the
identity-vs-invite collision: the token binds to whatever email
oauth2-proxy validates at click time, not the email originally typed
into the form — so a slight typo doesn't strand the recipient.

Audit-log entries land at ``invite.send`` (mint) and ``invite.accept``
(redeem) per spec § "Audit log".
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

from dao._base import (
    Actor,
    ForbiddenError,
    NotFoundError,
    db,
    require_role,
)


# Default expiry on a freshly-minted invite.  30 days is enough that a
# weekend mover can still send a link Monday and have it work the next
# weekend; tighter than 90 days so an abandoned token doesn't loiter.
DEFAULT_EXPIRY_DAYS = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _audit(conn, *, tenant_id: int | None, actor_email: str, action: str,
           target_id: int | None = None, metadata: dict | None = None) -> None:
    """One-line audit-log helper — kept module-local because the
    audit_log table itself isn't exposed via a DAO module yet (it's
    write-mostly with operator-only reads in phase 12)."""
    conn.execute(
        "INSERT INTO audit_log "
        "(tenant_id, actor_email, action, target_kind, target_id, metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tenant_id, actor_email, action, "invite", target_id,
         json.dumps(metadata or {})),
    )


# ── Mint ────────────────────────────────────────────────────────────


def create(
    actor: Actor,
    *,
    email: str,
    role: str = "maintainer",
    expires_in_days: int = DEFAULT_EXPIRY_DAYS,
) -> dict:
    """Mint a new invite token for ``email`` at ``role``.  Returns
    ``{"token": ..., "expires_at": ..., "email": ..., "role": ...}``.

    Maintainer-only.  ``role`` is the role the *invitee* will be
    granted, not the actor's — so a maintainer can invite a readonly
    helper.  Off-palette roles raise ValueError so a typo doesn't
    silently mint a useless token."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise ForbiddenError(f"{actor.email} has no active tenant")
    if role not in ("maintainer", "readonly"):
        raise ValueError(f"unknown role {role!r}")
    email = email.strip().lower()
    if "@" not in email:
        raise ValueError("invite email must contain '@'")
    token = secrets.token_urlsafe(24)
    expires_at = (_utcnow() + timedelta(days=expires_in_days)).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO tenant_invites "
            "(token, tenant_id, email, role, created_by_email, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (token, actor.tenant_id, email, role, actor.email, expires_at),
        )
        _audit(conn, tenant_id=actor.tenant_id, actor_email=actor.email,
               action="invite.send",
               metadata={"email": email, "role": role,
                         "expires_at": expires_at})
        conn.commit()
    return {
        "token": token,
        "tenant_id": actor.tenant_id,
        "email": email,
        "role": role,
        "expires_at": expires_at,
    }


# ── Read ────────────────────────────────────────────────────────────


def list_for_tenant(actor: Actor) -> list[dict]:
    """Outstanding (un-consumed, un-expired) invites for the actor's
    tenant.  Maintainer-only — readonly members shouldn't see who else
    is being invited per spec § "Roles · Operations matrix"."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        return []
    now = _utcnow().isoformat()
    with db() as conn:
        rows = conn.execute(
            "SELECT token, email, role, created_by_email, "
            "       created_at, expires_at "
            "FROM tenant_invites "
            "WHERE tenant_id = ? AND consumed_at IS NULL "
            "  AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY created_at DESC",
            (actor.tenant_id, now),
        ).fetchall()
    return [dict(r) for r in rows]


def get_by_token(token: str) -> dict | None:
    """Look up an invite by its token, regardless of consumed/expired
    state.  Used by the middleware bypass (which needs to know the
    token *exists* to let the request through to the redemption page)
    and by the redemption page itself (which renders different copy
    for already-consumed / expired tokens).

    Returns None for unknown tokens; the dict for known ones carries
    enough to render the accept page (tenant name + role) and decide
    whether redemption is still possible."""
    with db() as conn:
        row = conn.execute(
            "SELECT i.token, i.tenant_id, i.email, i.role, "
            "       i.created_by_email, i.created_at, i.expires_at, "
            "       i.consumed_at, t.name AS tenant_name "
            "FROM tenant_invites i "
            "JOIN tenants t ON t.id = i.tenant_id "
            "WHERE i.token = ?",
            (token,),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    now = _utcnow()
    expired = bool(d["expires_at"]) and d["expires_at"] < now.isoformat()
    consumed = d["consumed_at"] is not None
    d["expired"] = expired
    d["consumed"] = consumed
    d["redeemable"] = not expired and not consumed
    return d


# ── Mutate ──────────────────────────────────────────────────────────


def redeem(token: str, *, actual_email: str) -> dict:
    """Atomically consume the invite and add the redeeming email to
    ``tenant_members``.

    ``actual_email`` is whatever email oauth2-proxy validated at
    sign-in — *not* necessarily the ``email`` the inviter typed.  Per
    spec § "Identity-vs-invite collision" we bind the membership to
    the actual sign-in identity and audit the rebind so the inviter
    can spot a surprise.

    Raises NotFoundError if the token is unknown / expired / already
    consumed.  Returns ``{"tenant_id": ..., "role": ...}`` on success
    so the caller can build a redirect into the freshly-joined
    tenant."""
    actual_email = actual_email.strip().lower()
    if not actual_email:
        raise ForbiddenError("sign-in email is missing")
    invite = get_by_token(token)
    if invite is None or not invite["redeemable"]:
        raise NotFoundError("invite token unknown / expired / consumed")
    tenant_id = invite["tenant_id"]
    role = invite["role"]
    typed_email = invite["email"]
    rebind = (typed_email != actual_email)
    with db() as conn:
        # Race-safe consume: stamp the row only if it's still
        # un-consumed.  rowcount==0 means somebody else just claimed
        # it (or it was consumed between get_by_token and here).
        cur = conn.execute(
            "UPDATE tenant_invites SET consumed_at = CURRENT_TIMESTAMP "
            "WHERE token = ? AND consumed_at IS NULL",
            (token,),
        )
        if cur.rowcount == 0:
            raise NotFoundError("invite already consumed")
        # tenant_members PK is (tenant_id, email), so an idempotent
        # second-click by the same user just no-ops via OR IGNORE
        # rather than 500'ing on a UNIQUE violation.
        conn.execute(
            "INSERT OR IGNORE INTO tenant_members "
            "(tenant_id, email, role, invited_by_email, invited_at, joined_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (tenant_id, actual_email, role, invite["created_by_email"]),
        )
        _audit(conn, tenant_id=tenant_id, actor_email=actual_email,
               action="invite.accept",
               metadata={
                   "role": role,
                   "typed_email": typed_email,
                   "actual_email": actual_email,
                   "rebound": rebind,
               })
        conn.commit()
    return {"tenant_id": tenant_id, "role": role, "rebound": rebind}


def revoke(actor: Actor, token: str) -> None:
    """Cancel an outstanding invite.  Maintainer-only.  Idempotent:
    revoking an already-consumed or already-revoked token is a
    NotFoundError so the route can 404 cleanly without leaking
    whether the token ever existed."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"invite {token}")
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM tenant_invites "
            "WHERE token = ? AND tenant_id = ? AND consumed_at IS NULL",
            (token, actor.tenant_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"invite {token}")
        _audit(conn, tenant_id=actor.tenant_id, actor_email=actor.email,
               action="invite.revoke", metadata={"token_prefix": token[:8]})
        conn.commit()
