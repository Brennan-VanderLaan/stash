"""Box CRUD + the queries that hang off it (item-thumb mosaic, label
sheet selection).  See spec § "Roles" for who can do what.
"""

from __future__ import annotations

import obs
from dao._base import (
    Actor,
    ConflictError,
    NotFoundError,
    db,
    require_role,
)


_log = obs.get_logger("dao.boxes")


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
        new_id = cur.lastrowid
        obs.write_audit(
            conn, tenant_id=actor.tenant_id, actor_email=actor.email,
            action="box.create", target_kind="box", target_id=new_id,
            metadata={"name": name.strip(), "room_id": room_id},
        )
        conn.commit()
    _log.info("box.create id=%s name=%r", new_id, name.strip())
    return new_id


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
        obs.write_audit(
            conn, tenant_id=actor.tenant_id, actor_email=actor.email,
            action="box.update", target_kind="box", target_id=box_id,
            metadata={"name": name.strip(), "room_id": room_id,
                      "version": new_version},
        )
        conn.commit()
    _log.info("box.update id=%s version=%s", box_id, new_version)
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
        obs.write_audit(
            conn, tenant_id=actor.tenant_id, actor_email=actor.email,
            action="box.set_room", target_kind="box", target_id=box_id,
            metadata={"room_id": room_id},
        )
        conn.commit()
    _log.info("box.set_room id=%s room_id=%s", box_id, room_id)


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
        obs.write_audit(
            conn, tenant_id=actor.tenant_id, actor_email=actor.email,
            action="box.audit", target_kind="box", target_id=box_id,
        )
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
        obs.write_audit(
            conn, tenant_id=actor.tenant_id, actor_email=actor.email,
            action="box.delete", target_kind="box", target_id=box_id,
            metadata={"name": box["name"], "items_dropped": len(photos)},
        )
        conn.execute("DELETE FROM boxes WHERE id = ?", (box_id,))
        conn.commit()
    _log.warning("box.delete id=%s name=%r items=%d",
                 box_id, box["name"], len(photos))
    return {"name": box["name"], "tenant_id": box["tenant_id"], "photos": photos}


def set_label_orientation(
    actor: Actor, box_id: int, orientation: str,
) -> None:
    """Update the box's printed-label orientation (``landscape``
    or ``portrait``).  Maintainer only.  Persisted on the box so
    the next /labels render picks up the choice automatically."""
    if orientation not in ("landscape", "portrait"):
        raise ValueError(
            f"orientation must be 'landscape' or 'portrait', "
            f"got {orientation!r}",
        )
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"box {box_id}")
    with db() as conn:
        cur = conn.execute(
            "UPDATE boxes SET label_orientation = ?, "
            "  updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND tenant_id = ?",
            (orientation, box_id, actor.tenant_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"box {box_id}")
        obs.write_audit(
            conn, tenant_id=actor.tenant_id, actor_email=actor.email,
            action="box.label_orientation",
            target_kind="box", target_id=box_id,
            metadata={"orientation": orientation},
        )
        conn.commit()
    _log.info("box.label_orientation id=%s orientation=%s",
              box_id, orientation)


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


# ── Tinder-style swipe audit ───────────────────────────────────────


def _audit_resolve_box(conn, box_id: int, tenant_id: int):
    """Look up + tenant-verify a box for the audit endpoints.
    Returns the row dict or raises NotFoundError."""
    row = conn.execute(
        "SELECT id, name, tenant_id, last_audit_started_at "
        "FROM boxes WHERE id = ? AND tenant_id = ?",
        (box_id, tenant_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"box {box_id}")
    return row


def audit_session_remaining(actor: Actor, box_id: int) -> list[dict]:
    """Items in ``box_id`` that haven't been audited in the current
    session.  When no session is running every item is "remaining".

    String compare on ``last_seen_at >= started_at`` is safe because
    SQLite's ``CURRENT_TIMESTAMP`` always produces
    ``YYYY-MM-DD HH:MM:SS``, which is lex-sortable."""
    if actor.tenant_id is None:
        raise NotFoundError(f"box {box_id}")
    with db() as conn:
        box = _audit_resolve_box(conn, box_id, actor.tenant_id)
        started_at = box["last_audit_started_at"]
        if started_at:
            rows = conn.execute(
                "SELECT id, name, notes, photo, last_seen_at FROM items "
                "WHERE box_id = ? AND tenant_id = ? "
                "  AND (last_seen_at IS NULL OR last_seen_at < ?) "
                "ORDER BY name COLLATE NOCASE",
                (box_id, actor.tenant_id, started_at),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, notes, photo, last_seen_at FROM items "
                "WHERE box_id = ? AND tenant_id = ? "
                "ORDER BY name COLLATE NOCASE",
                (box_id, actor.tenant_id),
            ).fetchall()
    return [dict(r) for r in rows]


def audit_session_start(actor: Actor, box_id: int) -> None:
    """Begin (or restart) an audit session.  Idempotent on the
    "already running" case — Start while running resets the
    session timestamp, which matches user expectations when
    they reopen the page in a fresh state."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"box {box_id}")
    with db() as conn:
        _audit_resolve_box(conn, box_id, actor.tenant_id)
        conn.execute(
            "UPDATE boxes SET last_audit_started_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND tenant_id = ?",
            (box_id, actor.tenant_id),
        )
        conn.commit()
    _log.info("audit.start box_id=%s by=%s", box_id, actor.email)


def audit_mark_present(actor: Actor, box_id: int, item_id: int) -> int:
    """Mark one item as found-in-the-box this session.  Stamps
    ``items.last_seen_at = now`` so the remaining-items query
    drops it.  Returns the remaining count after the update."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"item {item_id} not in box {box_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT i.id FROM items i "
            "JOIN boxes b ON b.id = i.box_id "
            "WHERE i.id = ? AND i.box_id = ? "
            "  AND b.tenant_id = ?",
            (item_id, box_id, actor.tenant_id),
        ).fetchone()
        if not row:
            raise NotFoundError(f"item {item_id} not in box {box_id}")
        conn.execute(
            "UPDATE items SET last_seen_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (item_id,),
        )
        conn.commit()
    return len(audit_session_remaining(actor, box_id))


def audit_mark_missing(
    actor: Actor, box_id: int, item_id: int,
) -> tuple[int, int]:
    """Mark one item as missing.  Moves it to the sort queue with
    provenance (``previous_box_name``) + tags preserved, then
    deletes the items row.  Returns ``(remaining_count, pending_id)``
    so the caller can link to the just-created queue row."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"item {item_id} not in box {box_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT i.id, i.name, i.notes, i.photo, b.name AS box_name, "
            "       b.tenant_id "
            "FROM items i "
            "JOIN boxes b ON b.id = i.box_id "
            "WHERE i.id = ? AND i.box_id = ? "
            "  AND b.tenant_id = ?",
            (item_id, box_id, actor.tenant_id),
        ).fetchone()
        if not row:
            raise NotFoundError(f"item {item_id} not in box {box_id}")
        tenant_id = row["tenant_id"] or actor.tenant_id
        cur = conn.execute(
            "INSERT INTO pending_items "
            "(name, description, photo, previous_box_name, tenant_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["name"], row["notes"], row["photo"],
             row["box_name"], tenant_id),
        )
        pending_id = cur.lastrowid
        conn.execute(
            "INSERT INTO pending_item_tags "
            "(pending_item_id, tag_id, value, tenant_id) "
            "SELECT ?, tag_id, value, ? FROM item_tags WHERE item_id = ?",
            (pending_id, tenant_id, item_id),
        )
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        conn.commit()
    _log.info(
        "audit.missing box_id=%s item_id=%s pending_id=%s by=%s",
        box_id, item_id, pending_id, actor.email,
    )
    return len(audit_session_remaining(actor, box_id)), pending_id


def audit_session_finish(actor: Actor, box_id: int) -> None:
    """Wrap up an audit session: stamp ``last_audited_at = now`` +
    clear ``last_audit_started_at``.  Items still un-audited stay
    in the box — finishing is "I'm done", not "auto-flag the rest
    as missing"."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"box {box_id}")
    with db() as conn:
        _audit_resolve_box(conn, box_id, actor.tenant_id)
        conn.execute(
            "UPDATE boxes SET last_audited_at = CURRENT_TIMESTAMP, "
            "                  last_audit_started_at = NULL "
            "WHERE id = ? AND tenant_id = ?",
            (box_id, actor.tenant_id),
        )
        conn.commit()
    _log.info("audit.finish box_id=%s by=%s", box_id, actor.email)
