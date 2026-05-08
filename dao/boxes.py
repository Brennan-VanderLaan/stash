"""Box CRUD + the queries that hang off it (item-thumb mosaic, label
sheet selection).  See spec § "Roles" for who can do what.
"""

from __future__ import annotations

from dao._base import (
    Actor,
    ConflictError,
    NotFoundError,
    db,
    require_role,
)


# ── Reads ───────────────────────────────────────────────────────────


def list_with_counts(actor: Actor) -> list[dict]:
    """Every box the actor's tenant owns, joined with its room +
    location for the home-page card.  Includes per-box item count and
    location_id so the index template doesn't need a follow-up query."""
    if actor.tenant_id is None:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT b.*, COUNT(i.id) AS item_count, "
            "       r.name AS room_name, r.color AS room_color, "
            "       l.id AS location_id, l.name AS location_name "
            "FROM boxes b "
            "LEFT JOIN rooms r ON r.id = b.room_id "
            "LEFT JOIN locations l ON l.id = r.location_id "
            "LEFT JOIN items i ON i.box_id = b.id "
            "WHERE b.tenant_id = ? "
            "GROUP BY b.id "
            # Order so the index's bucketing loop can pick groups out
            # in stable order: rooms first (sorted location → room),
            # then legacy free-text locations, then unassigned at the
            # end.  Newest within a group surfaces first.
            "ORDER BY "
            "  CASE "
            "    WHEN r.id IS NOT NULL THEN 0 "
            "    WHEN b.location IS NOT NULL AND TRIM(b.location) != '' THEN 1 "
            "    ELSE 2 "
            "  END, "
            "  COALESCE(l.name, ''), "
            "  COALESCE(r.name, ''), "
            "  COALESCE(b.location, ''), "
            "  b.created_at DESC",
            (actor.tenant_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_by_id(actor: Actor, box_id: int) -> dict:
    """A single box scoped to the actor's tenant.  404s if the row
    doesn't exist OR belongs to a different tenant — never leaks the
    distinction."""
    if actor.tenant_id is None:
        raise NotFoundError(f"box {box_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT b.*, r.name AS room_name, r.color AS room_color, "
            "       l.id AS location_id, l.name AS location_name "
            "FROM boxes b "
            "LEFT JOIN rooms r ON r.id = b.room_id "
            "LEFT JOIN locations l ON l.id = r.location_id "
            "WHERE b.id = ? AND b.tenant_id = ?",
            (box_id, actor.tenant_id),
        ).fetchone()
    if row is None:
        raise NotFoundError(f"box {box_id}")
    return dict(row)


def list_for_picker(actor: Actor) -> list[dict]:
    """Lightweight list for the box-picker dropdown on the queue page.
    Same ordering rules as list_with_counts but without the JOINs we
    don't need."""
    if actor.tenant_id is None:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT b.id, b.name, b.location, "
            "       r.name AS room_name, "
            "       l.id AS location_id, l.name AS location_name "
            "FROM boxes b "
            "LEFT JOIN rooms r ON r.id = b.room_id "
            "LEFT JOIN locations l ON l.id = r.location_id "
            "WHERE b.tenant_id = ? "
            "ORDER BY l.name IS NULL, l.name, r.name, b.name",
            (actor.tenant_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_for_room(actor: Actor, room_id: int) -> list[dict]:
    """Boxes that live in a given room, scoped to the actor's tenant.
    Used by /rooms/{id}/boxes."""
    if actor.tenant_id is None:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT b.*, COUNT(i.id) AS item_count "
            "FROM boxes b "
            "LEFT JOIN items i ON i.box_id = b.id "
            "WHERE b.room_id = ? AND b.tenant_id = ? "
            "GROUP BY b.id ORDER BY b.name",
            (room_id, actor.tenant_id),
        ).fetchall()
    return [dict(r) for r in rows]


def list_with_art(actor: Actor, box_ids: list[int] | None = None) -> list[dict]:
    """Boxes (filtered by id list, or all) for the labels page.
    Returns dicts the labels renderer can attach decrypted ``art_bytes``
    to."""
    if actor.tenant_id is None:
        return []
    with db() as conn:
        if box_ids:
            placeholders = ",".join("?" * len(box_ids))
            rows = conn.execute(
                f"SELECT id, name, notes, background_art, tenant_id "
                f"FROM boxes "
                f"WHERE id IN ({placeholders}) AND tenant_id = ? "
                f"ORDER BY name",
                [*[int(b) for b in box_ids], actor.tenant_id],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, notes, background_art, tenant_id "
                "FROM boxes WHERE tenant_id = ? ORDER BY name",
                (actor.tenant_id,),
            ).fetchall()
    return [dict(r) for r in rows]


# ── Mutations ───────────────────────────────────────────────────────


def create(
    actor: Actor,
    name: str,
    location: str = "",
    notes: str = "",
    room_id: int | None = None,
) -> int:
    """Create a box in the actor's tenant.  Returns the new id.
    Maintainer role required."""
    require_role(actor, "maintainer")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO boxes (name, location, notes, room_id, tenant_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (name.strip(), location.strip(), notes.strip(), room_id, actor.tenant_id),
        )
        conn.commit()
    return cur.lastrowid


def update(
    actor: Actor,
    box_id: int,
    *,
    name: str,
    location: str = "",
    notes: str = "",
    room_id: int | None = None,
    color: str | None = None,
    if_match: int | None = None,
) -> int:
    """Edit box metadata.  Returns the new version.

    Optimistic concurrency: pass ``if_match`` to require the row's
    current version match.  Mismatch raises ConflictError (route → 409
    "this box was edited under you, refresh and reapply")."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"box {box_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT version FROM boxes WHERE id = ? AND tenant_id = ?",
            (box_id, actor.tenant_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"box {box_id}")
        if if_match is not None and row["version"] != if_match:
            raise ConflictError(
                f"box {box_id} version {row['version']} != expected {if_match}"
            )
        new_version = row["version"] + 1
        conn.execute(
            "UPDATE boxes SET name = ?, location = ?, notes = ?, "
            "room_id = ?, color = ?, version = ?, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND tenant_id = ?",
            (name.strip(), location.strip(), notes.strip(),
             room_id, color, new_version, box_id, actor.tenant_id),
        )
        conn.commit()
    return new_version


def set_room(actor: Actor, box_id: int, room_id: int | None) -> None:
    """Reassign a box to a different room (or clear).  Used by the
    floorplan drag-and-drop.  Maintainer only.

    The existing room_id can be NULL (legacy boxes) — accepted, the
    update sets it for the first time."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"box {box_id}")
    with db() as conn:
        cur = conn.execute(
            "UPDATE boxes SET room_id = ?, "
            "version = version + 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND tenant_id = ?",
            (room_id, box_id, actor.tenant_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"box {box_id}")
        conn.commit()


def mark_audited(actor: Actor, box_id: int) -> None:
    """Stamp last_audited_at on the box; called when the user
    confirms a walk-through audit."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"box {box_id}")
    with db() as conn:
        cur = conn.execute(
            "UPDATE boxes SET last_audited_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND tenant_id = ?",
            (box_id, actor.tenant_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"box {box_id}")
        conn.commit()


def delete(actor: Actor, box_id: int) -> dict:
    """Delete a box and cascade-remove its items.  Returns a dict of
    the deleted box's photo references so the caller can clean up
    the on-disk encrypted blobs.

    Maintainer only.  Items cascade via the FK ON DELETE CASCADE."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"box {box_id}")
    with db() as conn:
        box = conn.execute(
            "SELECT id, name, tenant_id FROM boxes WHERE id = ? AND tenant_id = ?",
            (box_id, actor.tenant_id),
        ).fetchone()
        if box is None:
            raise NotFoundError(f"box {box_id}")
        photos = [
            r["photo"] for r in conn.execute(
                "SELECT photo FROM items "
                "WHERE box_id = ? AND tenant_id = ? AND photo IS NOT NULL",
                (box_id, actor.tenant_id),
            ).fetchall()
        ]
        conn.execute("DELETE FROM boxes WHERE id = ?", (box_id,))
        conn.commit()
    return {"name": box["name"], "tenant_id": box["tenant_id"], "photos": photos}


def set_background_art(actor: Actor, box_id: int, art_filename: str) -> str | None:
    """Update boxes.background_art for the box.  Returns the previous
    filename so the caller can clean up the old on-disk blob."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"box {box_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT background_art FROM boxes WHERE id = ? AND tenant_id = ?",
            (box_id, actor.tenant_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"box {box_id}")
        old = row["background_art"]
        conn.execute(
            "UPDATE boxes SET background_art = ? WHERE id = ? AND tenant_id = ?",
            (art_filename, box_id, actor.tenant_id),
        )
        conn.commit()
    return old


def clear_background_art(actor: Actor, box_id: int) -> str | None:
    """NULL out background_art and return the previous filename."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"box {box_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT background_art FROM boxes WHERE id = ? AND tenant_id = ?",
            (box_id, actor.tenant_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"box {box_id}")
        conn.execute(
            "UPDATE boxes SET background_art = NULL WHERE id = ? AND tenant_id = ?",
            (box_id, actor.tenant_id),
        )
        conn.commit()
    return row["background_art"]
