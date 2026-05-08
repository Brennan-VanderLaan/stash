"""Pending-item rows that flow through the sort queue (vision-detected
items waiting for the user to assign / reject / edit).
"""

from __future__ import annotations

from dao._base import Actor, NotFoundError, db, require_role


def list_for_queue(actor: Actor) -> list[dict]:
    """The sort-queue listing — pending items joined with their AI-
    suggested box (when present), oldest first so the user works
    through the backlog in arrival order."""
    if actor.tenant_id is None:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT p.*, b.name AS suggested_box_name "
            "FROM pending_items p "
            "LEFT JOIN boxes b ON b.id = p.suggested_box_id "
            "WHERE p.tenant_id = ? "
            "ORDER BY p.created_at ASC",
            (actor.tenant_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_by_id(actor: Actor, pending_id: int) -> dict:
    if actor.tenant_id is None:
        raise NotFoundError(f"pending item {pending_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM pending_items WHERE id = ? AND tenant_id = ?",
            (pending_id, actor.tenant_id),
        ).fetchone()
    if row is None:
        raise NotFoundError(f"pending item {pending_id}")
    return dict(row)


def get_for_assign(actor: Actor, pending_id: int) -> dict:
    """Fields the assign endpoint needs — photo + bbox + tenant_id."""
    if actor.tenant_id is None:
        raise NotFoundError(f"pending item {pending_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT photo, bbox_y_min, bbox_x_min, bbox_y_max, bbox_x_max, tenant_id "
            "FROM pending_items WHERE id = ? AND tenant_id = ?",
            (pending_id, actor.tenant_id),
        ).fetchone()
    if row is None:
        raise NotFoundError(f"pending item {pending_id}")
    return dict(row)


def update_suggestion(
    actor: Actor,
    pending_id: int,
    *,
    suggested_box_id: int | None,
    suggested_new_box_name: str | None,
    suggested_new_box_location: str | None,
    suggestion_reason: str,
) -> None:
    """Persist the AI's "where should this go" suggestion onto the
    row.  Maintainer only (it's a write)."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"pending item {pending_id}")
    with db() as conn:
        cur = conn.execute(
            "UPDATE pending_items SET suggested_box_id = ?, "
            "suggested_new_box_name = ?, suggested_new_box_location = ?, "
            "suggestion_reason = ? "
            "WHERE id = ? AND tenant_id = ?",
            (suggested_box_id, suggested_new_box_name,
             suggested_new_box_location, suggestion_reason,
             pending_id, actor.tenant_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"pending item {pending_id}")
        conn.commit()


def delete(actor: Actor, pending_id: int) -> dict:
    """Reject a pending item.  Returns the photo filename so the
    caller can orphan-clean the on-disk blob."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError(f"pending item {pending_id}")
    with db() as conn:
        row = conn.execute(
            "SELECT photo FROM pending_items WHERE id = ? AND tenant_id = ?",
            (pending_id, actor.tenant_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"pending item {pending_id}")
        conn.execute(
            "DELETE FROM pending_items WHERE id = ? AND tenant_id = ?",
            (pending_id, actor.tenant_id),
        )
        conn.commit()
    return {"photo": row["photo"]}


def fingerprint(actor: Actor) -> str:
    """SHA-1 of the queue + boxes state — the queue page polls this
    and refreshes the cards fragment when it changes."""
    import hashlib
    if actor.tenant_id is None:
        return ""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, description, suggested_box_id, "
            "suggested_new_box_name, suggestion_reason "
            "FROM pending_items WHERE tenant_id = ? ORDER BY id",
            (actor.tenant_id,),
        ).fetchall()
        boxes = conn.execute(
            "SELECT id, name FROM boxes WHERE tenant_id = ? ORDER BY id",
            (actor.tenant_id,),
        ).fetchall()
    payload = "|".join(
        f"{r['id']}:{r['name']}:{r['description']}:{r['suggested_box_id']}:"
        f"{r['suggested_new_box_name']}:{r['suggestion_reason']}"
        for r in rows
    ) + "||" + "|".join(f"{b['id']}:{b['name']}" for b in boxes)
    return hashlib.sha1(payload.encode()).hexdigest()
