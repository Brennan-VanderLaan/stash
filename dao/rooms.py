"""Rooms — rectangles drawn on a floorplan, assignable to a single
floor.  Boxes belong to rooms (or to none, in which case they fall
into the "Unassigned" bucket on the index).
"""

from __future__ import annotations

import obs
from dao._base import Actor, NotFoundError, db, require_role


_log = obs.get_logger("dao.rooms")


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
        new_id = cur.lastrowid
        obs.write_audit(
            conn, tenant_id=actor.tenant_id, actor_email=actor.email,
            action="room.create", target_kind="room", target_id=new_id,
            metadata={"name": name.strip(), "floor_id": floor_id,
                      "location_id": floor["location_id"]},
        )
        conn.commit()
    return new_id


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


def find_by_name(actor: Actor, name: str) -> int | None:
    """Return the id of an existing room with the given name
    (case-insensitive) in the actor's tenant, or None.

    Used by the AI-suggest flow to resolve a free-text room name
    coming back from Claude into an existing room id without
    creating duplicates."""
    if actor.tenant_id is None or not name.strip():
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM rooms "
            "WHERE tenant_id = ? AND LOWER(name) = LOWER(?) "
            "LIMIT 1",
            (actor.tenant_id, name.strip()),
        ).fetchone()
    return int(row["id"]) if row else None


def attach_to_floor(
    actor: Actor, room_id: int, floor_id: int,
    *, x: float = 0.05, y: float = 0.05,
    w: float = 0.2, h: float = 0.2,
) -> int:
    """Returns the location_id the room now belongs to (so the
    route can redirect back to the right location without
    re-querying)."""
    """Move an unassigned room (or one on a different floor) onto a
    specific floor.  Used by /rooms/{id}/move-to-floor — fixes the
    feedback #76 case where the AI-suggest flow creates a box
    pointed at a free-text "location" that doesn't match any real
    room, leaving the operator with a dangling reference no UI can
    resolve.

    Defaults give the room a small visible rectangle near the
    top-left of the floorplan; the operator drags it into shape
    later via the in-page editor.  Caller can override coords if
    they have a better position in mind."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"room {room_id}")
    with db() as conn:
        floor = conn.execute(
            "SELECT location_id FROM floors "
            "WHERE id = ? AND tenant_id = ?",
            (floor_id, actor.tenant_id),
        ).fetchone()
        if floor is None:
            raise NotFoundError(f"floor {floor_id}")
        cur = conn.execute(
            "UPDATE rooms "
            "SET floor_id = ?, location_id = ?, "
            "    x = ?, y = ?, w = ?, h = ? "
            "WHERE id = ? AND tenant_id = ?",
            (floor_id, floor["location_id"], x, y, w, h,
             room_id, actor.tenant_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"room {room_id}")
        obs.write_audit(
            conn, tenant_id=actor.tenant_id, actor_email=actor.email,
            action="room.attach_to_floor",
            target_kind="room", target_id=room_id,
            metadata={
                "floor_id": floor_id,
                "location_id": floor["location_id"],
            },
        )
        conn.commit()
    return int(floor["location_id"])


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
        obs.write_audit(
            conn, tenant_id=actor.tenant_id, actor_email=actor.email,
            action="room.delete", target_kind="room",
            target_id=room_id,
            metadata={"floor_id": row["floor_id"],
                      "location_id": row["location_id"]},
        )
        conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
        conn.commit()
    _log.warning("room.delete id=%s", room_id)
    return {"location_id": row["location_id"], "floor_id": row["floor_id"]}
