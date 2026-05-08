"""Object shares — per-box / per-item access grants to a specific
email outside the granting tenant.

Spec § "Sharing model" defines the contract.  The two layers are:

* **Tenant invites** add a member with full-tenant access (handled by
  :mod:`dao.invites`).
* **Object shares** grant access to one box (and its items) or one
  item only.  The recipient need not be a member of the granting
  tenant.

This module is the second half.  Edge cases all live here:

* **Cascade on add.**  A box share grants access to *all* the box's
  current and future items.  The grant is recorded as one row keyed
  on the box; per-item access is derived at access-time.
* **Follows-on-move.**  Per-item shares stick to the item.  Per-box
  shares scope by box, so an item moving out of a shared box loses
  the share — the recipient sees a one-time "this item moved out
  and is no longer shared" notification.
* **Dedupe with membership.**  If the recipient is also a member of
  the granting tenant, effective role is ``max(membership_role,
  share_role)`` — the share never narrows tenant access.
* **Paused on soft-delete.**  If the granting tenant is in soft-
  delete, share access pauses (returns 403 with a "this stash is
  suspended" message).  On reactivation it resumes; on hard-delete
  the share row is gone via the FK CASCADE.
"""

from __future__ import annotations

import json
from typing import Iterable

from dao._base import (
    Actor,
    ForbiddenError,
    NotFoundError,
    db,
    require_role,
)


# ── Audit helper ────────────────────────────────────────────────────


def _audit(conn, *, tenant_id: int, actor_email: str, action: str,
           target_kind: str, target_id: int,
           metadata: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log "
        "(tenant_id, actor_email, action, target_kind, target_id, "
        " metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tenant_id, actor_email, action, target_kind, target_id,
         json.dumps(metadata or {})),
    )


# ── Internal target-resolution ──────────────────────────────────────


def _box_owner(conn, box_id: int) -> tuple[int, str] | None:
    row = conn.execute(
        "SELECT tenant_id, name FROM boxes WHERE id = ?",
        (box_id,),
    ).fetchone()
    if row is None:
        return None
    return (row["tenant_id"], row["name"])


def _item_owner(conn, item_id: int) -> tuple[int, int, str] | None:
    """Return ``(tenant_id, box_id, name)`` for the item."""
    row = conn.execute(
        "SELECT tenant_id, box_id, name FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    return (row["tenant_id"], row["box_id"], row["name"])


# ── Mutations ───────────────────────────────────────────────────────


def create(
    actor: Actor,
    *,
    target_kind: str,
    target_id: int,
    recipient_email: str,
    role: str = "readonly",
) -> dict:
    """Mint a fresh share row.  Maintainer-only on the granting
    tenant.  Returns the inserted row's id + tenant_id so the
    caller can build links / audit trails.

    A row already exists for ``(target_kind, target_id,
    recipient_email)``?  We treat that as idempotent: if the
    existing row is *active* and at the same role we no-op; if at
    a different role we update; if revoked we resurrect by clearing
    ``revoked_at``.  Same-row-different-role is a common UX case
    (the granter wants to widen a readonly share to maintainer)
    and we'd rather not force them to revoke + re-create."""
    if target_kind not in ("box", "item"):
        raise ValueError(f"unknown share target_kind {target_kind!r}")
    if role not in ("maintainer", "readonly"):
        raise ValueError(f"unknown share role {role!r}")
    recipient_email = recipient_email.strip().lower()
    if "@" not in recipient_email:
        raise ValueError("share recipient email must contain '@'")
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise ForbiddenError(f"{actor.email} has no active tenant")

    with db() as conn:
        # Resolve the target so we can confirm it's owned by the
        # actor's tenant — a maintainer of T1 must not be able to
        # share a T2 box even if they happen to know its id.
        if target_kind == "box":
            owner = _box_owner(conn, target_id)
            if owner is None:
                raise NotFoundError(f"box {target_id}")
            owner_tenant, target_label = owner
        else:
            owner = _item_owner(conn, target_id)
            if owner is None:
                raise NotFoundError(f"item {target_id}")
            owner_tenant, _box_id, target_label = owner
        if owner_tenant != actor.tenant_id:
            # Don't reveal whether the row exists in another tenant;
            # behave the same as "not found" from the actor's side.
            raise NotFoundError(f"{target_kind} {target_id}")

        # Idempotent path: existing share for the same triple?
        existing = conn.execute(
            "SELECT id, role, revoked_at FROM object_shares "
            "WHERE target_kind = ? AND target_id = ? "
            "  AND recipient_email = ?",
            (target_kind, target_id, recipient_email),
        ).fetchone()
        if existing is not None:
            updates = []
            params: list = []
            if existing["revoked_at"] is not None:
                updates.append("revoked_at = NULL")
                updates.append("created_at = CURRENT_TIMESTAMP")
                updates.append("created_by_email = ?")
                params.append(actor.email)
            if existing["role"] != role:
                updates.append("role = ?")
                params.append(role)
            if updates:
                params.append(existing["id"])
                conn.execute(
                    f"UPDATE object_shares SET {', '.join(updates)} "
                    f"WHERE id = ?",
                    params,
                )
                _audit(
                    conn, tenant_id=actor.tenant_id,
                    actor_email=actor.email, action="share.update",
                    target_kind=target_kind, target_id=target_id,
                    metadata={"recipient_email": recipient_email,
                              "role": role,
                              "from_role": existing["role"],
                              "resurrected":
                                  existing["revoked_at"] is not None},
                )
                conn.commit()
            return {"id": existing["id"],
                    "tenant_id": actor.tenant_id,
                    "target_kind": target_kind,
                    "target_id": target_id,
                    "target_label": target_label,
                    "recipient_email": recipient_email,
                    "role": role}

        # Fresh insert.
        cur = conn.execute(
            "INSERT INTO object_shares "
            "(tenant_id, target_kind, target_id, recipient_email, "
            " role, created_by_email) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (actor.tenant_id, target_kind, target_id,
             recipient_email, role, actor.email),
        )
        share_id = cur.lastrowid
        _audit(
            conn, tenant_id=actor.tenant_id, actor_email=actor.email,
            action="share.create",
            target_kind=target_kind, target_id=target_id,
            metadata={"recipient_email": recipient_email, "role": role},
        )
        conn.commit()
    return {"id": share_id,
            "tenant_id": actor.tenant_id,
            "target_kind": target_kind,
            "target_id": target_id,
            "target_label": target_label,
            "recipient_email": recipient_email,
            "role": role}


def revoke(actor: Actor, share_id: int) -> None:
    """Revoke a share by id.  Maintainer of the granting tenant
    only.  Idempotent — already-revoked or already-gone returns
    NotFoundError so the route can 404 cleanly."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"share {share_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT tenant_id, target_kind, target_id, "
            "       recipient_email, role, revoked_at "
            "FROM object_shares WHERE id = ?",
            (share_id,),
        ).fetchone()
        if row is None or row["tenant_id"] != actor.tenant_id:
            raise NotFoundError(f"share {share_id}")
        if row["revoked_at"] is not None:
            raise NotFoundError(f"share {share_id}")
        conn.execute(
            "UPDATE object_shares SET revoked_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (share_id,),
        )
        _audit(
            conn, tenant_id=actor.tenant_id, actor_email=actor.email,
            action="share.revoke",
            target_kind=row["target_kind"], target_id=row["target_id"],
            metadata={"recipient_email": row["recipient_email"],
                      "role": row["role"]},
        )
        conn.commit()


# ── Reads ───────────────────────────────────────────────────────────


def list_outbound(actor: Actor) -> list[dict]:
    """Active shares the actor's tenant has issued, oldest first.
    Joins to the target so the table can render a name (box name /
    item name) without the caller doing a second fetch."""
    if actor.tenant_id is None:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT s.id, s.target_kind, s.target_id, s.recipient_email, "
            "       s.role, s.created_at, s.created_by_email, "
            "       COALESCE(b.name, i.name) AS target_label "
            "FROM object_shares s "
            "LEFT JOIN boxes b ON s.target_kind = 'box' AND b.id = s.target_id "
            "LEFT JOIN items i ON s.target_kind = 'item' AND i.id = s.target_id "
            "WHERE s.tenant_id = ? AND s.revoked_at IS NULL "
            "ORDER BY s.created_at DESC",
            (actor.tenant_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_for_recipient(email: str) -> list[dict]:
    """Active shares pointed at ``email``, with the granting
    tenant's name + the target's label.  Soft-deleted granting
    tenants are filtered out — the recipient sees them as gone
    until reactivation, per spec § "Sharing model · paused on
    granting-tenant-soft-delete"."""
    email = email.strip().lower()
    if not email:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT s.id, s.target_kind, s.target_id, s.role, "
            "       s.created_at, s.created_by_email, "
            "       s.tenant_id, t.name AS tenant_name, "
            "       COALESCE(b.name, i.name) AS target_label, "
            "       i.box_id AS item_box_id "
            "FROM object_shares s "
            "JOIN tenants t ON t.id = s.tenant_id "
            "LEFT JOIN boxes b ON s.target_kind = 'box' AND b.id = s.target_id "
            "LEFT JOIN items i ON s.target_kind = 'item' AND i.id = s.target_id "
            "WHERE s.recipient_email = ? "
            "  AND s.revoked_at IS NULL "
            "  AND t.deleted_at IS NULL "
            "ORDER BY s.created_at DESC",
            (email,),
        ).fetchall()
    return [dict(r) for r in rows]


def shares_for_email(email: str) -> tuple[dict, ...]:
    """Compact tuple suitable for stuffing onto :class:`Actor` so
    middleware bypass + DAO access checks can consult it cheaply.
    Each entry: ``{"target_kind": ..., "target_id": ..., "role":
    ..., "tenant_id": ...}``.  Filters soft-deleted granting
    tenants exactly like :func:`list_for_recipient`."""
    email = email.strip().lower()
    if not email:
        return ()
    with db() as conn:
        rows = conn.execute(
            "SELECT s.target_kind, s.target_id, s.role, s.tenant_id "
            "FROM object_shares s "
            "JOIN tenants t ON t.id = s.tenant_id "
            "WHERE s.recipient_email = ? "
            "  AND s.revoked_at IS NULL "
            "  AND t.deleted_at IS NULL",
            (email,),
        ).fetchall()
    return tuple(dict(r) for r in rows)


# ── Access resolution ───────────────────────────────────────────────


_ROLE_RANK = {None: 0, "readonly": 1, "maintainer": 2}


def _max_role(*roles: str | None) -> str | None:
    """Pick the strongest of the given roles (None = no access)."""
    best: str | None = None
    for r in roles:
        if _ROLE_RANK.get(r, 0) > _ROLE_RANK.get(best, 0):
            best = r
    return best


def effective_role_for_box(actor: Actor, box_id: int) -> str | None:
    """Return ``"maintainer"`` / ``"readonly"`` / ``None`` reflecting
    what the actor can do on this box, considering both:

    * tenant membership (the membership-role on the box's tenant),
    * any active object share targeting this box directly.

    Operators get None — operators don't gain data access via this
    surface, even on tenants they could administer.

    The cascade rule (item access via box share) is *not* applied
    here — see :func:`effective_role_for_item`."""
    with db() as conn:
        owner = _box_owner(conn, box_id)
    if owner is None:
        return None
    owner_tenant, _label = owner
    membership = actor.has_membership(owner_tenant)
    share_role: str | None = None
    for s in actor.shares:
        if s["target_kind"] == "box" and s["target_id"] == box_id:
            share_role = _max_role(share_role, s["role"])
    return _max_role(membership, share_role)


def effective_role_for_item(actor: Actor, item_id: int) -> str | None:
    """Like :func:`effective_role_for_box` but for items.  Honours
    cascade-on-add: a box share grants the same role on every item
    currently in that box.  Per-item shares stick to the item even
    when it moves; per-box shares are evaluated against the item's
    *current* box, so an item moving out loses the box-share's
    coverage automatically (follows-on-move)."""
    with db() as conn:
        owner = _item_owner(conn, item_id)
    if owner is None:
        return None
    owner_tenant, box_id, _label = owner
    membership = actor.has_membership(owner_tenant)
    share_role: str | None = None
    for s in actor.shares:
        if s["target_kind"] == "item" and s["target_id"] == item_id:
            share_role = _max_role(share_role, s["role"])
        elif s["target_kind"] == "box" and s["target_id"] == box_id:
            # Cascade: the item is currently in a box the recipient
            # has share access to.
            share_role = _max_role(share_role, s["role"])
    return _max_role(membership, share_role)


# ── Recipient-side fetches ──────────────────────────────────────────


def fetch_box_for_recipient(actor: Actor, box_id: int) -> dict | None:
    """The recipient view of a shared box.  Returns the box row
    joined with its room + location for breadcrumb rendering,
    regardless of the actor's active tenant — but only if the
    actor has at least readonly access via either share or
    membership.  Used by the /shared/box/{id} route.

    Returns None if the actor has no access (route → 404)."""
    role = effective_role_for_box(actor, box_id)
    if role is None:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT b.*, r.name AS room_name, r.color AS room_color, "
            "       l.id AS location_id, l.name AS location_name, "
            "       t.name AS tenant_name "
            "FROM boxes b "
            "JOIN tenants t ON t.id = b.tenant_id "
            "LEFT JOIN rooms r ON r.id = b.room_id "
            "LEFT JOIN locations l ON l.id = r.location_id "
            "WHERE b.id = ?",
            (box_id,),
        ).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["share_role"] = role
    return out


def fetch_box_items_for_recipient(actor: Actor, box_id: int) -> list[dict]:
    """Items in a shared box, regardless of active tenant.  Caller
    is expected to have already verified access via
    :func:`effective_role_for_box`."""
    if effective_role_for_box(actor, box_id) is None:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, notes, photo, source_photo, is_missing, "
            "       created_at, last_seen_at, tenant_id "
            "FROM items WHERE box_id = ? "
            "ORDER BY created_at DESC",
            (box_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_item_for_recipient(actor: Actor, item_id: int) -> dict | None:
    """The recipient view of a shared item.  Returns the item joined
    with its box + tenant for breadcrumb rendering, or None if the
    actor has no access."""
    role = effective_role_for_item(actor, item_id)
    if role is None:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT i.*, b.name AS box_name, t.name AS tenant_name "
            "FROM items i "
            "JOIN boxes b ON b.id = i.box_id "
            "JOIN tenants t ON t.id = i.tenant_id "
            "WHERE i.id = ?",
            (item_id,),
        ).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["share_role"] = role
    return out
