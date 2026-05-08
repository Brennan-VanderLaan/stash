"""Rooms — rectangles drawn on a floorplan, assignable to a single
floor.  Boxes belong to rooms (or to none, in which case they fall
into the "Unassigned" bucket on the index).
"""

from __future__ import annotations

from dao._base import Actor, NotFoundError, db, require_role


def list_for_floor(actor: Actor, floor_id: int) -> list[dict]:
    if actor.tenant_id is None:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM rooms WHERE floor_id = ? AND tenant_id = ?",
            (floor_id, actor.tenant_id),
        ).fetchall()
    return [dict(r) for r in rows]


def get_with_location(actor: Actor, room_id: int) -> dict:
    """Used by /rooms/{id}/boxes — returns the room joined with its
    location's name + id so the breadcrumb renders without a second
    query."""
    if actor.tenant_id is None:
        raise NotFoundError(f"room {room_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT r.*, l.name AS location_name, l.id AS location_id "
            "FROM rooms r JOIN locations l ON l.id = r.location_id "
            "WHERE r.id = ? AND r.tenant_id = ?",
            (room_id, actor.tenant_id),
        ).fetchone()
    if row is None:
        raise NotFoundError(f"room {room_id}")
    return dict(row)


def list_for_picker(actor: Actor) -> list[dict]:
    """Flat list suitable for an optgroup'd select.  Includes floor
    name so a location with two rooms of the same name (two
    'Bathroom's on different floors) can be visually disambiguated
    in the dropdown."""
    if actor.tenant_id is None:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT r.id, r.name, "
            "       l.id AS location_id, l.name AS location_name, "
            "       f.name AS floor_name "
            "FROM rooms r "
            "JOIN locations l ON l.id = r.location_id "
            "LEFT JOIN floors f ON f.id = r.floor_id "
            "WHERE r.tenant_id = ? "
            "ORDER BY l.name, f.name IS NULL, f.name, r.name",
            (actor.tenant_id,),
        ).fetchall()
    rooms = [dict(r) for r in rows]
    # Mark each room with whether its name collides with another
    # room in the same location — the template uses this flag to
    # append the floor name so the user can tell them apart.
    by_loc_name: dict[tuple[int, str], int] = {}
    for r in rooms:
        key = (r["location_id"], r["name"].casefold())
        by_loc_name[key] = by_loc_name.get(key, 0) + 1
    for r in rooms:
        key = (r["location_id"], r["name"].casefold())
        r["needs_floor_disambiguation"] = by_loc_name[key] > 1
    return rooms


def create(
    actor: Actor,
    floor_id: int,
    name: str,
    *,
    x: float, y: float, w: float, h: float,
    color: str,
) -> int:
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"floor {floor_id}")
    with db() as conn:
        floor = conn.execute(
            "SELECT location_id FROM floors WHERE id = ? AND tenant_id = ?",
            (floor_id, actor.tenant_id),
        ).fetchone()
        if floor is None:
            raise NotFoundError(f"floor {floor_id}")
        cur = conn.execute(
            "INSERT INTO rooms "
            "(location_id, floor_id, name, x, y, w, h, color, tenant_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (floor["location_id"], floor_id, name.strip(),
             x, y, w, h, color, actor.tenant_id),
        )
        conn.commit()
    return cur.lastrowid


def update(
    actor: Actor,
    room_id: int,
    *,
    name: str | None = None,
    x: float | None = None, y: float | None = None,
    w: float | None = None, h: float | None = None,
    color: str | None = None,
) -> None:
    """Sparse update — only fields with non-None values change."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"room {room_id}")
    fields, values = [], []
    for col, val in (
        ("name", name.strip() if name is not None else None),
        ("x", x), ("y", y), ("w", w), ("h", h),
        ("color", color),
    ):
        if val is not None:
            fields.append(f"{col} = ?")
            values.append(val)
    if not fields:
        return
    values.extend([room_id, actor.tenant_id])
    with db() as conn:
        cur = conn.execute(
            f"UPDATE rooms SET {', '.join(fields)} "
            f"WHERE id = ? AND tenant_id = ?",
            values,
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"room {room_id}")
        conn.commit()


def delete(actor: Actor, room_id: int) -> dict:
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"room {room_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT location_id, floor_id FROM rooms "
            "WHERE id = ? AND tenant_id = ?",
            (room_id, actor.tenant_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"room {room_id}")
        conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
        conn.commit()
    return {"location_id": row["location_id"], "floor_id": row["floor_id"]}
