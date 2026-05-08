"""Locations (the top-level "where stuff lives" container).  See
spec § "Sharing model" for sharing semantics — for now everything is
tenant-scoped.
"""

from __future__ import annotations

from dao._base import Actor, NotFoundError, db, require_role


# ── Reads ───────────────────────────────────────────────────────────


def list_with_room_counts(actor: Actor) -> list[dict]:
    """Locations with per-location room + box counts and a
    representative floorplan filename.

    `floorplan` here is "what should we show on the locations index
    card", not necessarily ``locations.floorplan`` — multi-floor
    support moved the actual floorplan image onto floors.floorplan,
    leaving the legacy column populated only for pre-migration data
    (and stale even there once the user replaces the floor's image).
    Pick the first floor's floorplan when one exists, fall back to
    the legacy column otherwise."""
    if actor.tenant_id is None:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT l.id, l.name, l.created_at, "
            "       (SELECT COUNT(*) FROM rooms WHERE location_id = l.id) AS room_count, "
            "       (SELECT COUNT(*) FROM boxes b "
            "         JOIN rooms r ON r.id = b.room_id "
            "         WHERE r.location_id = l.id) AS box_count, "
            "       COALESCE("
            "         (SELECT f.floorplan FROM floors f "
            "           WHERE f.location_id = l.id AND f.floorplan IS NOT NULL "
            "           ORDER BY f.sort_order, f.id LIMIT 1), "
            "         l.floorplan"
            "       ) AS floorplan "
            "FROM locations l "
            "WHERE l.tenant_id = ? "
            "ORDER BY l.created_at",
            (actor.tenant_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_by_id(actor: Actor, location_id: int) -> dict:
    if actor.tenant_id is None:
        raise NotFoundError(f"location {location_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM locations WHERE id = ? AND tenant_id = ?",
            (location_id, actor.tenant_id),
        ).fetchone()
    if row is None:
        raise NotFoundError(f"location {location_id}")
    return dict(row)


# ── Mutations ───────────────────────────────────────────────────────


def create(actor: Actor, name: str) -> int:
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        from dao._base import ForbiddenError
        raise ForbiddenError(f"{actor.email} has no active tenant")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO locations (name, tenant_id) VALUES (?, ?)",
            (name.strip(), actor.tenant_id),
        )
        conn.commit()
    return cur.lastrowid


def rename(actor: Actor, location_id: int, name: str) -> None:
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"location {location_id}")
    with db() as conn:
        cur = conn.execute(
            "UPDATE locations SET name = ? WHERE id = ? AND tenant_id = ?",
            (name.strip(), location_id, actor.tenant_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"location {location_id}")
        conn.commit()


def delete(actor: Actor, location_id: int, expected_name: str) -> dict:
    """Delete a location.  ``expected_name`` is the type-the-name
    confirmation from the form — must exactly match the row's
    current name or a NotFoundError-like guard fires (route → 400).

    Returns ``{"floorplan": ...}`` so the caller can orphan-clean
    the legacy locations.floorplan blob if any."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"location {location_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT name, floorplan FROM locations WHERE id = ? AND tenant_id = ?",
            (location_id, actor.tenant_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"location {location_id}")
        if expected_name.strip() != row["name"]:
            from dao._base import ForbiddenError
            raise ForbiddenError("type the location name to confirm deletion")
        conn.execute("DELETE FROM locations WHERE id = ?", (location_id,))
        conn.commit()
    return {"floorplan": row["floorplan"]}
