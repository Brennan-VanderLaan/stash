"""Tenant + member queries.

The actor middleware in app.py uses these to resolve
``X-Forwarded-Email`` into an :class:`Actor`; everything downstream
trusts that resolution.
"""

from __future__ import annotations

import obs
from dao._base import Actor, NotFoundError, db, require_operator


_log = obs.get_logger("dao.tenants")


def get_tenant(actor: Actor, tenant_id: int) -> dict:
    """The actor's view of a tenant — name, plan, lifecycle state.
    Raises NotFoundError if the actor is not a member."""
    role = actor.has_membership(tenant_id)
    if role is None and not actor.is_operator:
        raise NotFoundError(f"tenant {tenant_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT id, name, plan, deleted_at, hard_delete_after, created_at "
            "FROM tenants WHERE id = ?",
            (tenant_id,),
        ).fetchone()
    if row is None:
        raise NotFoundError(f"tenant {tenant_id}")
    return dict(row)


def list_members(actor: Actor, tenant_id: int) -> list[dict]:
    """Members of a tenant + a ``last_active_at`` watermark per
    member (the latest audit_log entry where that email is the
    actor — covers UI mutations, MCP tool calls, OAuth flows,
    everything that writes through ``obs.write_audit``).  Visible
    to any member of the tenant; the UI surfaces it on /usage and
    /admin."""
    if actor.has_membership(tenant_id) is None and not actor.is_operator:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT m.email, m.role, m.joined_at, m.invited_at, "
            "       m.invited_by_email, "
            "       (SELECT MAX(a.created_at) FROM audit_log a "
            "         WHERE a.actor_email = m.email) AS last_active_at "
            "FROM tenant_members m WHERE m.tenant_id = ? "
            "ORDER BY m.joined_at IS NULL, m.joined_at, m.email",
            (tenant_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def memberships_for_email(email: str) -> tuple[tuple[int, str], ...]:
    """All (tenant_id, role) pairs the email is a member of, sorted
    so the first entry is the natural "active" choice (oldest joined
    membership wins; the switcher cookie picks a different one when
    the user prefers)."""
    with db() as conn:
        rows = conn.execute(
            "SELECT tenant_id, role FROM tenant_members "
            "WHERE email = ? "
            "ORDER BY joined_at IS NULL, joined_at, tenant_id",
            (email,),
        ).fetchall()
    return tuple((r["tenant_id"], r["role"]) for r in rows)


def tenant_names_for_email(email: str) -> dict[int, str]:
    """``{tenant_id: name}`` for every tenant the email belongs to.
    Powers the header tenant switcher's dropdown labels — the
    middleware fetches this once per request and stashes it on
    ``request.state.tenant_names`` so base.html doesn't have to
    round-trip to the DB.  Soft-deleted tenants stay in the map
    so the switcher can grey them out instead of vanishing."""
    if not email:
        return {}
    with db() as conn:
        rows = conn.execute(
            "SELECT t.id, t.name "
            "FROM tenant_members m "
            "JOIN tenants t ON t.id = m.tenant_id "
            "WHERE m.email = ?",
            (email,),
        ).fetchall()
    return {int(r["id"]): r["name"] for r in rows}


# ── Operator surface (spec § "Operator surface") ────────────────────


def list_all(actor: Actor) -> list[dict]:
    """Operator-only roster of every tenant on the deployment with
    member / box / item counts, lifecycle state, and a
    ``last_activity_at`` watermark (max audit_log timestamp across
    that tenant — covers every mutation the DAO writes through
    ``obs.write_audit``).  Hard-rule from the spec: operators see
    counts + metadata only, *never* the contents (no box names,
    item names, or photos).  This method obeys that — only
    aggregate counters and the tenant's own name leave the DAO."""
    require_operator(actor)
    with db() as conn:
        rows = conn.execute(
            "SELECT t.id, t.name, t.plan, t.created_at, "
            "       t.deleted_at, t.hard_delete_after, "
            "       (SELECT COUNT(*) FROM tenant_members "
            "         WHERE tenant_id = t.id) AS member_count, "
            "       (SELECT COUNT(*) FROM tenant_invites "
            "         WHERE tenant_id = t.id "
            "           AND consumed_at IS NULL) AS open_invites, "
            "       (SELECT COUNT(*) FROM boxes "
            "         WHERE tenant_id = t.id) AS box_count, "
            "       (SELECT COUNT(*) FROM items "
            "         WHERE tenant_id = t.id) AS item_count, "
            "       (SELECT MAX(created_at) FROM audit_log "
            "         WHERE tenant_id = t.id) AS last_activity_at "
            "FROM tenants t "
            "ORDER BY t.created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def create_tenant(
    actor: Actor,
    name: str,
    *,
    plan: str = "free",
    client_ip: str = "",
) -> int:
    """Operator-driven tenant creation.  Returns the new id.

    Spec § "Sign-up + onboarding" path #1 covers self-serve creation
    by a freshly-signed-in user; that's a separate phase.  This
    surface is for the operator-bootstrapping case (e.g. setting up
    a friend on their own tenant) — and so the operator does NOT
    automatically become a member.  The expectation is that they
    immediately mint an invite for the intended owner; until that's
    accepted, the new tenant has zero members.

    ``client_ip`` is recorded in the audit_log metadata so the
    per-IP throttle in :func:`dao.quotas.check_tenant_creation_rate`
    can count cleanly against the same source."""
    require_operator(actor)
    name = name.strip()
    if not name:
        raise ValueError("tenant name required")
    if plan not in ("free", "pro"):
        raise ValueError(f"unknown plan {plan!r}")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES (?, ?)",
            (name, plan),
        )
        tenant_id = cur.lastrowid
        # Audit-log the create — operators can later prove who set up
        # which tenant when (and the lifecycle audit is the only
        # cross-tenant view of operator activity that exists today).
        obs.write_audit(
            conn,
            tenant_id=tenant_id,
            actor_email=actor.email,
            action="tenant.create",
            target_kind="tenant",
            target_id=tenant_id,
            metadata={
                "name": name,
                "plan": plan,
                "ip": client_ip or "unknown",
            },
        )
        conn.commit()
    _log.info("tenant.create id=%s name=%r plan=%s ip=%s",
              tenant_id, name, plan, client_ip or "unknown")
    return tenant_id
