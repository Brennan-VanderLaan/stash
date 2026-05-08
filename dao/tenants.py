"""Tenant + member queries.

The actor middleware in app.py uses these to resolve
``X-Forwarded-Email`` into an :class:`Actor`; everything downstream
trusts that resolution.
"""

from __future__ import annotations

from dao._base import Actor, NotFoundError, db


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
    """Members of a tenant.  Visible to any member of that tenant; the
    UI surfaces it on /usage (and the maintenance page until /usage
    ships in roadmap step 13)."""
    if actor.has_membership(tenant_id) is None and not actor.is_operator:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT email, role, joined_at, invited_at, invited_by_email "
            "FROM tenant_members WHERE tenant_id = ? "
            "ORDER BY joined_at IS NULL, joined_at, email",
            (tenant_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def memberships_for_email(email: str) -> tuple[tuple[int, str], ...]:
    """All (tenant_id, role) pairs the email is a member of, sorted
    so the first entry is the natural "active" choice (oldest joined
    membership wins until the switcher in roadmap step 15)."""
    with db() as conn:
        rows = conn.execute(
            "SELECT tenant_id, role FROM tenant_members "
            "WHERE email = ? "
            "ORDER BY joined_at IS NULL, joined_at, tenant_id",
            (email,),
        ).fetchall()
    return tuple((r["tenant_id"], r["role"]) for r in rows)
