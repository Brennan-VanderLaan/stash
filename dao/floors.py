"""Floors hang off a location.  A location may have many floors, each
with its own floorplan image and rooms.
"""

from __future__ import annotations

from dao._base import Actor, NotFoundError, db, require_role


def list_for_location(actor: Actor, location_id: int) -> list[dict]:
    if actor.tenant_id is None:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM floors WHERE location_id = ? AND tenant_id = ? "
            "ORDER BY sort_order, id",
            (location_id, actor.tenant_id),
        ).fetchall()
    return [dict(r) for r in rows]


def get_by_id(actor: Actor, floor_id: int) -> dict:
    if actor.tenant_id is None:
        raise NotFoundError(f"floor {floor_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM floors WHERE id = ? AND tenant_id = ?",
            (floor_id, actor.tenant_id),
        ).fetchone()
    if row is None:
        raise NotFoundError(f"floor {floor_id}")
    return dict(row)


def create(actor: Actor, location_id: int, name: str) -> int:
    """Append a floor at the end of the location's sort_order."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"location {location_id}")
    with db() as conn:
        if conn.execute(
            "SELECT 1 FROM locations WHERE id = ? AND tenant_id = ?",
            (location_id, actor.tenant_id),
        ).fetchone() is None:
            raise NotFoundError(f"location {location_id}")
        next_sort = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM floors "
            "WHERE location_id = ?",
            (location_id,),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO floors (location_id, name, sort_order, tenant_id) "
            "VALUES (?, ?, ?, ?)",
            (location_id, name.strip(), next_sort, actor.tenant_id),
        )
        conn.commit()
    return cur.lastrowid


def rename(actor: Actor, floor_id: int, name: str) -> int:
    """Rename a floor.  Returns location_id so the caller can build
    a redirect."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"floor {floor_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT location_id FROM floors WHERE id = ? AND tenant_id = ?",
            (floor_id, actor.tenant_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"floor {floor_id}")
        conn.execute(
            "UPDATE floors SET name = ? WHERE id = ? AND tenant_id = ?",
            (name.strip(), floor_id, actor.tenant_id),
        )
        conn.commit()
    return row["location_id"]


def update_floorplan(actor: Actor, floor_id: int, new_filename: str) -> dict:
    """Swap floorplan filename to ``new_filename``.  Returns
    ``{"location_id": ..., "old_floorplan": ...}`` so the caller can
    orphan-clean the previous on-disk blob."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"floor {floor_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT location_id, floorplan FROM floors "
            "WHERE id = ? AND tenant_id = ?",
            (floor_id, actor.tenant_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"floor {floor_id}")
        conn.execute(
            "UPDATE floors SET floorplan = ? WHERE id = ? AND tenant_id = ?",
            (new_filename, floor_id, actor.tenant_id),
        )
        conn.commit()
    return {"location_id": row["location_id"], "old_floorplan": row["floorplan"]}


def delete(actor: Actor, floor_id: int) -> dict:
    """Delete a floor + cascade-remove its rooms.  Returns the
    floorplan filename + parent location_id."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"floor {floor_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT location_id, floorplan FROM floors "
            "WHERE id = ? AND tenant_id = ?",
            (floor_id, actor.tenant_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"floor {floor_id}")
        conn.execute("DELETE FROM floors WHERE id = ?", (floor_id,))
        conn.commit()
    return {"location_id": row["location_id"], "floorplan": row["floorplan"]}
