"""Tags + the per-item / per-pending-item tag join tables.

Tags are per-tenant (spec § "Tag uniqueness") so two tenants can use
the same name without colliding.
"""

from __future__ import annotations

from dao._base import Actor, db, require_role


def list_names(actor: Actor) -> list[str]:
    """All tag names in the actor's tenant, alphabetised — used by
    the datalist on the queue + item-detail forms for autocomplete."""
    if actor.tenant_id is None:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT name FROM tags WHERE tenant_id = ? OR tenant_id IS NULL "
            "ORDER BY name",
            (actor.tenant_id,),
        ).fetchall()
    return [r["name"] for r in rows]


def list_with_counts(actor: Actor) -> list[dict]:
    """Tag rows with how many items each tag is attached to."""
    if actor.tenant_id is None:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT t.id, t.name, "
            "       (SELECT COUNT(*) FROM item_tags it "
            "         WHERE it.tag_id = t.id AND it.tenant_id = ?) AS use_count "
            "FROM tags t "
            "WHERE (t.tenant_id = ? OR t.tenant_id IS NULL) "
            "ORDER BY t.name",
            (actor.tenant_id, actor.tenant_id),
        ).fetchall()
    return [dict(r) for r in rows]


def ensure(actor: Actor, name: str) -> int:
    """Get-or-create.  Tags are per-tenant via (tenant_id, name) but
    legacy rows (pre-multi-tenancy) carry tenant_id NULL — match
    them too so an upgrade doesn't multiply the catalog."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        from dao._base import ForbiddenError
        raise ForbiddenError(f"{actor.email} has no active tenant")
    name = name.strip()
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tags (name, tenant_id) VALUES (?, ?)",
            (name, actor.tenant_id),
        )
        row = conn.execute(
            "SELECT id FROM tags WHERE name = ? AND "
            "(tenant_id = ? OR tenant_id IS NULL) "
            "ORDER BY tenant_id IS NULL, id LIMIT 1",
            (name, actor.tenant_id),
        ).fetchone()
        conn.commit()
    return row["id"]


def attach_to_item(
    actor: Actor,
    item_id: int,
    tag_entries: list[tuple[str, str | None]],
) -> None:
    """Attach a list of (name, optional value) pairs to an item.
    Creates tag rows as needed; replaces any existing (item, tag)
    pairing with the new value."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        from dao._base import ForbiddenError
        raise ForbiddenError(f"{actor.email} has no active tenant")
    for tag_name, value in tag_entries:
        tag_id = ensure(actor, tag_name)
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO item_tags "
                "(item_id, tag_id, value, tenant_id) "
                "VALUES (?, ?, ?, ?)",
                (item_id, tag_id, value, actor.tenant_id),
            )
            conn.commit()
