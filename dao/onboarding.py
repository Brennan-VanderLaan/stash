"""First-run onboarding state.

Surfaces three Boolean signals about how far a new user has
gotten through the magic flow:

* ``has_photo`` — uploaded at least one photo (pending_items OR
  resolved items with a photo).  Confirms they've experienced
  the "take a picture and the AI does the rest" entry point.
* ``has_box`` — at least one box exists.  Created manually OR
  via the queue's ``create-suggested-box`` flow.
* ``has_item_in_box`` — at least one item is filed into a box.
  The full magic-loop completion signal.

The /home "Get started" card walks the user through these in
order.  When all three are True the card collapses to a single
"you're set up" line so the dashboard stops nagging.

No new schema: all three flags are derived from existing tables.
A tenant reset (or a fresh tenant) automatically rolls them back
to False without any extra cleanup.
"""

from __future__ import annotations

from dao._base import Actor, db


def first_run_state(actor: Actor) -> dict:
    """Return the three Booleans plus a ``complete`` shortcut and a
    ``next_step`` string the template uses to pick the active CTA.

    Cheap — three single-row LIMIT 1 lookups, runs on every /home
    render but doesn't show up in profile traces.  If the queries
    ever do start mattering we can fold them into the existing
    boxes/thumbs queries on the same route."""
    if actor.tenant_id is None:
        return {
            "has_photo": False,
            "has_box": False,
            "has_item_in_box": False,
            "complete": False,
            "next_step": "photo",
        }
    tid = actor.tenant_id
    with db() as conn:
        # Photo uploaded — covers both still-pending (not yet
        # assigned to a box) and already-resolved items.  The OR
        # branch matters for legacy / imported items that bypassed
        # pending_items entirely (e.g. via an Encircle import).
        has_photo_row = conn.execute(
            "SELECT 1 FROM pending_items WHERE tenant_id = ? LIMIT 1",
            (tid,),
        ).fetchone()
        if has_photo_row is None:
            has_photo_row = conn.execute(
                "SELECT 1 FROM items "
                "WHERE tenant_id = ? AND photo IS NOT NULL "
                "LIMIT 1",
                (tid,),
            ).fetchone()
        has_photo = has_photo_row is not None

        has_box = conn.execute(
            "SELECT 1 FROM boxes WHERE tenant_id = ? LIMIT 1",
            (tid,),
        ).fetchone() is not None

        has_item_in_box = conn.execute(
            "SELECT 1 FROM items "
            "WHERE tenant_id = ? AND box_id IS NOT NULL "
            "LIMIT 1",
            (tid,),
        ).fetchone() is not None

    complete = has_photo and has_box and has_item_in_box
    # Pick the active CTA for the Get started card.  Order
    # matters: each step depends on the previous one being done.
    if not has_photo:
        next_step = "photo"
    elif not has_item_in_box:
        # has_photo is True; the active call is either "see the
        # queue" or "file something into a box" depending on
        # whether a box exists yet.  Either way the user's next
        # action is on /queue (the suggested-box flow gets them a
        # box AND files the item in one click), so the CTA points
        # there.
        next_step = "queue"
    else:
        next_step = "done"
    return {
        "has_photo": has_photo,
        "has_box": has_box,
        "has_item_in_box": has_item_in_box,
        "complete": complete,
        "next_step": next_step,
    }
