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


def create_self_serve_tenant(
    *,
    name: str,
    plan: str,
    owner_email: str,
    client_ip: str = "",
) -> int:
    """Self-serve tenant creation — called from the bootstrap-invite
    redemption path so the operator doesn't have to pre-name the
    tenant.  Returns the new id.

    No ``Actor`` parameter because the caller is a fresh user with
    no current memberships and ``require_operator`` would (correctly)
    reject them.  The bootstrap-invite token *itself* is the
    authority: ``dao.invites.redeem_bootstrap`` checks the token is
    still un-consumed, locks the plan to whatever the operator
    minted, then calls this helper to do the actual INSERT.

    Audit-log records the owner_email as the actor for traceability
    + ``action='tenant.self_create'`` to distinguish from the
    operator-driven ``tenant.create`` path."""
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
        obs.write_audit(
            conn,
            tenant_id=tenant_id,
            actor_email=owner_email,
            action="tenant.self_create",
            target_kind="tenant",
            target_id=tenant_id,
            metadata={
                "name": name,
                "plan": plan,
                "ip": client_ip or "unknown",
            },
        )
        conn.commit()
    _log.info("tenant.self_create id=%s name=%r plan=%s ip=%s by=%s",
              tenant_id, name, plan, client_ip or "unknown", owner_email)
    return tenant_id


# Soft-delete grace window before a hard-delete becomes the
# default operator action.  30 days matches typical SaaS practice
# (and the spec's GDPR posture) — the operator can still force
# an immediate hard-delete via the explicit endpoint.
SOFT_DELETE_GRACE_DAYS = 30


def operator_set_plan(
    actor: Actor, tenant_id: int, plan: str, *, reason: str = "",
) -> dict:
    """Operator-side plan override.  Bypasses Stripe.

    Use case: comping friends + family to Pro out-of-band, beta
    testers, support credit, etc.  The Stripe webhook still owns
    plan transitions driven by real subscription events — this
    sets ``tenants.plan`` directly without touching the Stripe
    columns (``stripe_customer_id`` / ``stripe_subscription_id``
    / ``subscription_status``), so a real paid subscription that
    later cancels still flips back to free via the webhook.

    Effects are immediate.  Quotas come from
    ``_PLAN_DEFAULTS[plan]`` and apply on the next request.

    Idempotent — setting the same plan returns ``changed=False``
    without writing an audit row.

    Args:
        plan: 'free' or 'pro'.
        reason: Optional free-text note saved in the audit log.
            Operators are encouraged to leave a paper trail
            (``"comp: brother — bday gift"``, ``"beta tester"``)
            so the org understands the historical state.
    """
    require_operator(actor)
    if plan not in ("free", "pro"):
        raise ValueError(
            f"plan must be 'free' or 'pro', got {plan!r}",
        )
    with db() as conn:
        row = conn.execute(
            "SELECT name, plan FROM tenants WHERE id = ?",
            (tenant_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"tenant {tenant_id}")
        old_plan = row["plan"]
        if old_plan == plan:
            return {
                "id": tenant_id, "name": row["name"],
                "plan": plan, "changed": False,
            }
        conn.execute(
            "UPDATE tenants SET plan = ? WHERE id = ?",
            (plan, tenant_id),
        )
        obs.write_audit(
            conn, tenant_id=tenant_id, actor_email=actor.email,
            action="tenant.plan_override",
            target_kind="tenant", target_id=tenant_id,
            metadata={
                "old_plan": old_plan,
                "new_plan": plan,
                "reason": (reason or "")[:200],
            },
        )
        conn.commit()
    _log.warning(
        "tenant.plan_override id=%s name=%r %s->%s by=%s reason=%r",
        tenant_id, row["name"], old_plan, plan, actor.email, reason,
    )
    return {
        "id": tenant_id, "name": row["name"],
        "plan": plan, "changed": True,
    }


def soft_delete(actor: Actor, tenant_id: int) -> dict:
    """Operator-only: mark a tenant as soft-deleted.

    Sets ``tenants.deleted_at = now`` and
    ``hard_delete_after = now + 30d``.  Phase 6 already enforces
    the share-pause behaviour off the ``deleted_at`` column (the
    soft-deleted tenant's outbound shares filter out of the
    recipient's view + access checks); members of the
    soft-deleted tenant still resolve to membership in the
    middleware so they can see their data — they just can't
    cross-tenant share into other stashes.  Reactivate via
    ``reactivate`` to undo before the grace window expires; the
    eventual hard-delete sweep (phase 14) reads
    ``hard_delete_after`` to know when to permanently drop the
    rows.

    Idempotent on a tenant that's already soft-deleted — bumps
    ``hard_delete_after`` forward to today+30 so a re-soft-delete
    extends the grace window rather than shrinking it."""
    require_operator(actor)
    with db() as conn:
        row = conn.execute(
            "SELECT name FROM tenants WHERE id = ?", (tenant_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"tenant {tenant_id}")
        conn.execute(
            "UPDATE tenants SET "
            "  deleted_at = CURRENT_TIMESTAMP, "
            "  hard_delete_after = datetime("
            "    CURRENT_TIMESTAMP, ?"
            "  ) "
            "WHERE id = ?",
            (f"+{SOFT_DELETE_GRACE_DAYS} days", tenant_id),
        )
        obs.write_audit(
            conn, tenant_id=tenant_id, actor_email=actor.email,
            action="tenant.soft_delete",
            target_kind="tenant", target_id=tenant_id,
            metadata={
                "name": row["name"],
                "grace_days": SOFT_DELETE_GRACE_DAYS,
            },
        )
        conn.commit()
    _log.warning("tenant.soft_delete id=%s name=%r grace_days=%s",
                 tenant_id, row["name"], SOFT_DELETE_GRACE_DAYS)
    return {"id": tenant_id, "name": row["name"]}


def reactivate(actor: Actor, tenant_id: int) -> dict:
    """Operator-only: undo a soft-delete.  Clears both
    ``deleted_at`` and ``hard_delete_after``.  No-op on a tenant
    that's already active."""
    require_operator(actor)
    with db() as conn:
        row = conn.execute(
            "SELECT name, deleted_at FROM tenants WHERE id = ?",
            (tenant_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"tenant {tenant_id}")
        if row["deleted_at"] is None:
            # Operator clicked reactivate on an active tenant —
            # benign; no audit row to avoid filling the log with
            # idempotent retries.
            return {"id": tenant_id, "name": row["name"],
                    "already_active": True}
        conn.execute(
            "UPDATE tenants SET deleted_at = NULL, "
            "  hard_delete_after = NULL WHERE id = ?",
            (tenant_id,),
        )
        obs.write_audit(
            conn, tenant_id=tenant_id, actor_email=actor.email,
            action="tenant.reactivate",
            target_kind="tenant", target_id=tenant_id,
            metadata={"name": row["name"]},
        )
        conn.commit()
    _log.warning("tenant.reactivate id=%s name=%r",
                 tenant_id, row["name"])
    return {"id": tenant_id, "name": row["name"],
            "already_active": False}


def hard_delete(actor: Actor, tenant_id: int) -> dict:
    """Operator-only: permanently delete a tenant and everything
    that references it.  Cascades through every ``ON DELETE
    CASCADE`` foreign key (boxes, items, rooms, members,
    invites, shares, audit_log rows for this tenant, …).

    No grace window — this is the "the soft-deleted tenant has
    been quiet for 30 days and the sweep job ran" / "operator
    explicitly chose to nuke immediately" endpoint.  The audit
    entry is written with ``tenant_id=NULL`` (the row's about
    to vanish along with any tenant-scoped audit rows) so the
    permanent record lives in the cross-tenant operator stream.

    Refuses to delete the actor's own tenant — accidentally
    blowing up the tenant you're signed into would lock you out
    of the operator surface.  An operator with no membership
    (the typical bootstrap stance) isn't blocked."""
    require_operator(actor)
    if actor.tenant_id == tenant_id:
        raise ValueError(
            "Cannot hard-delete the tenant you are currently a member of; "
            "switch tenants first or sign out and back in."
        )
    with db() as conn:
        row = conn.execute(
            "SELECT name FROM tenants WHERE id = ?", (tenant_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"tenant {tenant_id}")
        obs.write_audit(
            conn, tenant_id=None, actor_email=actor.email,
            action="tenant.hard_delete",
            target_kind="tenant", target_id=tenant_id,
            metadata={"name": row["name"]},
        )
        conn.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
        conn.commit()
    _log.warning("tenant.hard_delete id=%s name=%r", tenant_id, row["name"])
    return {"id": tenant_id, "name": row["name"]}
