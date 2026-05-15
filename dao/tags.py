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


def list_with_distribution(actor: Actor) -> list[dict]:
    """Tag rows + the boxes / rooms / locations where each tag's
    items currently live.  Feeds the /tags landing page so the
    user sees "this tag is on 12 items, spread across 3 rooms in
    2 locations" instead of just a flat name + count list.

    Returned shape per tag::

        {
          "id": ..., "name": ..., "item_count": N,
          "boxes":     [{"id": ..., "name": ..., "count": K}, ...],
          "rooms":     [{"id": ..., "name": ..., "color": ..., "count": K}, ...],
          "locations": [{"id": ..., "name": ..., "count": K}, ...],
        }

    All three nested lists are sorted by count desc + capped at
    8 so the panel stays readable for power-tagged stashes.
    Tags with zero items appear with empty nested lists — they
    still want to be visible so the user can rename or delete
    them, but they don't claim location space they don't have.
    """
    if actor.tenant_id is None:
        return []
    with db() as conn:
        # One pass: every (tag_id, item, box, room?, location?)
        # row in the tenant.  Aggregate in Python — the row count
        # is bounded by item_tags + items, and the alternative
        # (three correlated subqueries per tag) is uglier and not
        # measurably faster.
        rows = conn.execute(
            """
            SELECT t.id AS tag_id, t.name AS tag_name,
                   b.id AS box_id, b.name AS box_name,
                   r.id AS room_id, r.name AS room_name, r.color AS room_color,
                   l.id AS loc_id, l.name AS loc_name
              FROM tags t
              LEFT JOIN item_tags it
                     ON it.tag_id = t.id AND it.tenant_id = ?
              LEFT JOIN items i
                     ON i.id = it.item_id AND i.tenant_id = ?
              LEFT JOIN boxes b
                     ON b.id = i.box_id AND b.tenant_id = ?
              LEFT JOIN rooms r
                     ON r.id = b.room_id AND r.tenant_id = ?
              LEFT JOIN locations l
                     ON l.id = r.location_id
                    AND l.tenant_id = ?
             WHERE (t.tenant_id = ? OR t.tenant_id IS NULL)
            """,
            (actor.tenant_id,) * 6,
        ).fetchall()
    # Bucket per tag.
    by_tag: dict[int, dict] = {}
    for row in rows:
        tid = row["tag_id"]
        bucket = by_tag.setdefault(tid, {
            "id": tid, "name": row["tag_name"], "item_count": 0,
            "_boxes": {}, "_rooms": {}, "_locations": {},
        })
        if row["box_id"] is None:
            continue
        bucket["item_count"] += 1
        bk = row["box_id"]
        if bk not in bucket["_boxes"]:
            bucket["_boxes"][bk] = {
                "id": bk, "name": row["box_name"], "count": 0,
            }
        bucket["_boxes"][bk]["count"] += 1
        if row["room_id"]:
            rk = row["room_id"]
            if rk not in bucket["_rooms"]:
                bucket["_rooms"][rk] = {
                    "id": rk, "name": row["room_name"],
                    "color": row["room_color"], "count": 0,
                }
            bucket["_rooms"][rk]["count"] += 1
        if row["loc_id"]:
            lk = row["loc_id"]
            if lk not in bucket["_locations"]:
                bucket["_locations"][lk] = {
                    "id": lk, "name": row["loc_name"], "count": 0,
                }
            bucket["_locations"][lk]["count"] += 1

    def _top_n(d: dict, n: int = 8) -> list:
        return sorted(d.values(), key=lambda x: (-x["count"], x["name"]))[:n]

    out = []
    for tid, b in by_tag.items():
        out.append({
            "id": b["id"], "name": b["name"],
            "item_count": b["item_count"],
            "boxes":     _top_n(b["_boxes"]),
            "rooms":     _top_n(b["_rooms"]),
            "locations": _top_n(b["_locations"]),
        })
    out.sort(key=lambda x: (-x["item_count"], x["name"].lower()))
    return out


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


def attach_to_box(
    actor: Actor,
    box_id: int,
    tag_entries: list[tuple[str, str | None]],
) -> int:
    """Attach every ``(name, value)`` entry to every item currently
    in ``box_id``.  Returns the number of items touched.

    Single transaction so a half-applied tag mid-loop doesn't leave
    the box with inconsistent labelling.  The box must belong to
    the actor's tenant; cross-tenant box_id silently no-ops (0
    rows) because the items SELECT is tenant-scoped.

    A new tag row gets created up front (via ``ensure``) so the
    inner loop is purely the item_tags upsert — no repeated
    "INSERT OR IGNORE" overhead per item.
    """
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        from dao._base import ForbiddenError
        raise ForbiddenError(f"{actor.email} has no active tenant")
    if not tag_entries:
        return 0
    # Resolve tag ids first so the per-item loop is one INSERT each.
    resolved = [(ensure(actor, name), value) for name, value in tag_entries]
    with db() as conn:
        item_ids = [
            row["id"] for row in conn.execute(
                "SELECT i.id FROM items i "
                "JOIN boxes b ON b.id = i.box_id "
                "WHERE b.id = ? AND b.tenant_id = ?",
                (box_id, actor.tenant_id),
            ).fetchall()
        ]
        for item_id in item_ids:
            for tag_id, value in resolved:
                conn.execute(
                    "INSERT OR REPLACE INTO item_tags "
                    "(item_id, tag_id, value, tenant_id) "
                    "VALUES (?, ?, ?, ?)",
                    (item_id, tag_id, value, actor.tenant_id),
                )
        conn.commit()
    return len(item_ids)
