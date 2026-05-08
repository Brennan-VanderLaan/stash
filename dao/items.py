"""Items + their tag joins.  Photo file lifecycle is owned by app.py
(encryption, on-disk paths) — the DAO returns the filename only and
trusts the caller to encrypt/decrypt.
"""

from __future__ import annotations

import obs
from dao._base import Actor, NotFoundError, db, require_role


_log = obs.get_logger("dao.items")


# ── Reads ───────────────────────────────────────────────────────────


def list_for_box(actor: Actor, box_id: int) -> list[dict]:
    """Items in a box, scoped to the actor's tenant."""
    if actor.tenant_id is None:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE box_id = ? AND tenant_id = ? "
            "ORDER BY created_at",
            (box_id, actor.tenant_id),
        ).fetchall()
    return [dict(r) for r in rows]


def get_by_id(actor: Actor, item_id: int) -> dict:
    if actor.tenant_id is None:
        raise NotFoundError(f"item {item_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM items WHERE id = ? AND tenant_id = ?",
            (item_id, actor.tenant_id),
        ).fetchone()
    if row is None:
        raise NotFoundError(f"item {item_id}")
    return dict(row)


def list_recent_photos_per_box(
    actor: Actor, limit_per_box: int = 5,
) -> dict[int, list[str]]:
    """Up to ``limit_per_box`` recent item photos per box for the
    index thumbnail strip.  Returns {box_id: [photo, ...]} in
    newest-first order."""
    if actor.tenant_id is None:
        return {}
    with db() as conn:
        rows = conn.execute(
            "SELECT box_id, photo FROM items "
            "WHERE photo IS NOT NULL AND tenant_id = ? "
            "ORDER BY box_id, created_at DESC",
            (actor.tenant_id,),
        ).fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        lst = out.setdefault(r["box_id"], [])
        if len(lst) < limit_per_box:
            lst.append(r["photo"])
    return out


def list_recent_photos_for_room(
    actor: Actor, room_id: int, limit_per_box: int = 5,
) -> dict[int, list[str]]:
    """Same as :func:`list_recent_photos_per_box` but limited to one
    room — used by /rooms/{id}/boxes."""
    if actor.tenant_id is None:
        return {}
    with db() as conn:
        rows = conn.execute(
            "SELECT i.box_id, i.photo FROM items i "
            "JOIN boxes b ON b.id = i.box_id "
            "WHERE b.room_id = ? AND b.tenant_id = ? "
            "  AND i.tenant_id = ? AND i.photo IS NOT NULL "
            "ORDER BY i.box_id, i.created_at DESC",
            (room_id, actor.tenant_id, actor.tenant_id),
        ).fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        lst = out.setdefault(r["box_id"], [])
        if len(lst) < limit_per_box:
            lst.append(r["photo"])
    return out


def list_tags_for_item(actor: Actor, item_id: int) -> list[dict]:
    """Tag rows attached to an item, including the (nullable) value."""
    if actor.tenant_id is None:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT t.id AS tag_id, t.name, it.value "
            "FROM item_tags it JOIN tags t ON t.id = it.tag_id "
            "WHERE it.item_id = ? AND it.tenant_id = ? "
            "ORDER BY t.name",
            (item_id, actor.tenant_id),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Mutations ───────────────────────────────────────────────────────


def create(
    actor: Actor,
    box_id: int,
    *,
    name: str,
    notes: str = "",
    photo: str | None = None,
    source_photo: str | None = None,
) -> int:
    """Create an item.  Maintainer only.  ``source_photo`` defaults to
    ``photo`` when not given so the recrop / revert flow has a stable
    "original" pointer."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"box {box_id}")
    if source_photo is None:
        source_photo = photo
    with db() as conn:
        # Confirm the parent box belongs to the actor's tenant — the
        # FK alone wouldn't catch a malicious box_id pointing at
        # another tenant's box.
        if conn.execute(
            "SELECT 1 FROM boxes WHERE id = ? AND tenant_id = ?",
            (box_id, actor.tenant_id),
        ).fetchone() is None:
            raise NotFoundError(f"box {box_id}")
        cur = conn.execute(
            "INSERT INTO items "
            "(box_id, name, notes, photo, source_photo, tenant_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (box_id, name.strip(), notes.strip(), photo, source_photo, actor.tenant_id),
        )
        new_id = cur.lastrowid
        obs.write_audit(
            conn, tenant_id=actor.tenant_id, actor_email=actor.email,
            action="item.create", target_kind="item", target_id=new_id,
            metadata={"box_id": box_id, "name": name.strip(),
                      "has_photo": bool(photo)},
        )
        conn.commit()
    _log.info("item.create id=%s box_id=%s name=%r",
              new_id, box_id, name.strip())
    return new_id


def replace_photo(actor: Actor, item_id: int, new_photo: str) -> dict:
    """Atomically swap an item's photo + source_photo to a new
    filename.  Returns ``{"box_id": ..., "old_photo": ..., "old_source": ...}``
    so the caller can decide which on-disk blobs are now orphans."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"item {item_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT box_id, photo, source_photo FROM items "
            "WHERE id = ? AND tenant_id = ?",
            (item_id, actor.tenant_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"item {item_id}")
        conn.execute(
            "UPDATE items SET photo = ?, source_photo = ? "
            "WHERE id = ? AND tenant_id = ?",
            (new_photo, new_photo, item_id, actor.tenant_id),
        )
        conn.commit()
    return {
        "box_id": row["box_id"],
        "old_photo": row["photo"],
        "old_source": row["source_photo"],
    }


def apply_recrop(
    actor: Actor,
    item_id: int,
    new_photo: str,
    source_photo: str,
) -> dict:
    """Update photo (cropped output) + source_photo for the item.
    Returns ``{"box_id": ..., "old_photo": ...}`` so the caller can
    orphan-clean the previous crop."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"item {item_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT box_id, photo FROM items WHERE id = ? AND tenant_id = ?",
            (item_id, actor.tenant_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"item {item_id}")
        conn.execute(
            "UPDATE items SET photo = ?, source_photo = ? "
            "WHERE id = ? AND tenant_id = ?",
            (new_photo, source_photo, item_id, actor.tenant_id),
        )
        conn.commit()
    return {"box_id": row["box_id"], "old_photo": row["photo"]}


def get_for_recrop(actor: Actor, item_id: int) -> dict:
    """The fields the recrop endpoint needs in one query: photo,
    source_photo, box_id, plus the item's tenant_id (always equal to
    the actor's, by construction, but we return it for the caller to
    pass into encryption helpers)."""
    if actor.tenant_id is None:
        raise NotFoundError(f"item {item_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT id, box_id, photo, source_photo, tenant_id "
            "FROM items WHERE id = ? AND tenant_id = ?",
            (item_id, actor.tenant_id),
        ).fetchone()
    if row is None:
        raise NotFoundError(f"item {item_id}")
    return dict(row)


def move_to_box(actor: Actor, item_id: int, target_box_id: int) -> dict:
    """Reassign an item to a different box.  Both rows must belong
    to the actor's tenant."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"item {item_id}")
    with db() as conn:
        item = conn.execute(
            "SELECT box_id FROM items WHERE id = ? AND tenant_id = ?",
            (item_id, actor.tenant_id),
        ).fetchone()
        if item is None:
            raise NotFoundError(f"item {item_id}")
        if conn.execute(
            "SELECT 1 FROM boxes WHERE id = ? AND tenant_id = ?",
            (target_box_id, actor.tenant_id),
        ).fetchone() is None:
            raise NotFoundError(f"box {target_box_id}")
        conn.execute(
            "UPDATE items SET box_id = ? WHERE id = ? AND tenant_id = ?",
            (target_box_id, item_id, actor.tenant_id),
        )
        obs.write_audit(
            conn, tenant_id=actor.tenant_id, actor_email=actor.email,
            action="item.move", target_kind="item", target_id=item_id,
            metadata={"old_box_id": item["box_id"],
                      "new_box_id": target_box_id},
        )
        conn.commit()
    _log.info("item.move id=%s %s -> %s",
              item_id, item["box_id"], target_box_id)
    return {"old_box_id": item["box_id"], "new_box_id": target_box_id}


def delete(actor: Actor, item_id: int) -> dict:
    """Delete an item.  Returns the row's photo + source_photo +
    box_id so the caller can orphan-clean the on-disk blobs."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"item {item_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT box_id, photo, source_photo FROM items "
            "WHERE id = ? AND tenant_id = ?",
            (item_id, actor.tenant_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"item {item_id}")
        obs.write_audit(
            conn, tenant_id=actor.tenant_id, actor_email=actor.email,
            action="item.delete", target_kind="item", target_id=item_id,
            metadata={"box_id": row["box_id"]},
        )
        conn.execute(
            "DELETE FROM items WHERE id = ? AND tenant_id = ?",
            (item_id, actor.tenant_id),
        )
        conn.commit()
    _log.info("item.delete id=%s box_id=%s", item_id, row["box_id"])
    return {
        "box_id": row["box_id"],
        "photo": row["photo"],
        "source_photo": row["source_photo"],
    }


def remove_tag(actor: Actor, item_id: int, tag_id: int) -> int:
    """Detach a tag from an item.  Returns the box_id so the caller
    can redirect back to the item's box page."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"item {item_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT box_id FROM items WHERE id = ? AND tenant_id = ?",
            (item_id, actor.tenant_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"item {item_id}")
        conn.execute(
            "DELETE FROM item_tags "
            "WHERE item_id = ? AND tag_id = ? AND tenant_id = ?",
            (item_id, tag_id, actor.tenant_id),
        )
        conn.commit()
    return row["box_id"]


def mark_missing(actor: Actor, item_id: int, missing: bool) -> None:
    """Flip is_missing for an item — used by the audit walkthrough."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"item {item_id}")
    with db() as conn:
        cur = conn.execute(
            "UPDATE items SET is_missing = ?, "
            "last_seen_at = CASE WHEN ? = 0 THEN CURRENT_TIMESTAMP ELSE last_seen_at END "
            "WHERE id = ? AND tenant_id = ?",
            (1 if missing else 0, 1 if missing else 0, item_id, actor.tenant_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"item {item_id}")
        conn.commit()
