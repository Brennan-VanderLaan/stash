"""``/api/v1`` — bearer-auth JSON API for stash.

Phase 11 surface, scoped tight: enough for an MCP-style agent to
look up where things live and shuffle items between boxes.  The
heavy CRUD (create / delete) lands later when there's a concrete
agent workflow that needs it.

Routing structure: an :class:`fastapi.APIRouter` mounted at
``/api/v1`` from app.py.  Bearer auth runs in the global
``current_actor`` middleware, so by the time the router's handlers
fire ``request.state.actor`` is already populated with the
token-derived :class:`Actor`.

Auth contract:
* No bearer token → 401 from the middleware.
* Valid token → Actor with ``email = "api_token:<id>"``,
  ``tenant_id`` set, ``role = "maintainer"`` (the default for v1
  tokens; future scoped tokens can dial down).
* The route handlers call DAO methods as usual; tenant scoping +
  role gates fall through naturally.

Response shape: dicts (FastAPI auto-serialises).  No Pydantic
models for now — the DAO already shapes the rows for templates and
they're directly JSON-friendly.  When the API surface stabilises we
can lock the contract with response_models, but premature schema
stiffness on a v1 surface is the wrong tradeoff.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

import obs
from dao import (
    Actor,
    ForbiddenError,
    NotFoundError,
)
from dao import boxes as dao_boxes
from dao import items as dao_items
from dao import locations as dao_locations
from dao import rooms as dao_rooms
from dao import tags as dao_tags


router = APIRouter(prefix="/api/v1", tags=["api"])
_log = obs.get_logger("api")


# ── Identity ────────────────────────────────────────────────────────


@router.get("/me")
def whoami(request: Request) -> dict:
    """Echo back what the bearer token resolved to.  Useful for
    smoke-testing a fresh token + getting the active tenant_id
    before issuing tenant-scoped calls."""
    actor: Actor = request.state.actor
    return {
        "email": actor.email,
        "tenant_id": actor.tenant_id,
        "role": actor.role,
        "is_operator": actor.is_operator,
    }


# ── Boxes ───────────────────────────────────────────────────────────


@router.get("/boxes")
def list_boxes(request: Request) -> dict:
    """Every box in the actor's tenant with item counts + room +
    location.  Wrapped in ``{"boxes": [...]}`` so future top-level
    fields (paging cursors, totals) don't break clients."""
    actor: Actor = request.state.actor
    return {"boxes": dao_boxes.list_with_counts(actor)}


@router.get("/boxes/{box_id}")
def get_box(request: Request, box_id: int) -> dict:
    actor: Actor = request.state.actor
    try:
        box = dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(404, "Box not found")
    return box


@router.get("/boxes/{box_id}/items")
def list_box_items(request: Request, box_id: int) -> dict:
    """Items inside a single box.  The box-level tenant scope check
    happens up front so a missing box returns 404 rather than an
    empty list (which would be ambiguous between "empty" and
    "wrong tenant")."""
    actor: Actor = request.state.actor
    try:
        dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(404, "Box not found")
    items = dao_items.list_for_box(actor, box_id)
    return {"box_id": box_id, "items": items}


# ── Items ───────────────────────────────────────────────────────────


@router.get("/items")
def search_items(
    request: Request,
    q: str = "",
    box_id: int | None = None,
    tag: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Lightweight item search.  Free-text + tag + per-box filters
    are anded; result is paginated.  This is the endpoint an
    MCP-style agent uses to answer "where are my <thing>?" — the
    join surfaces the box name inline so the agent doesn't need a
    follow-up fetch per row."""
    actor: Actor = request.state.actor
    items = dao_items.search(
        actor, q=q, box_id=box_id, tag=tag,
        limit=limit, offset=offset,
    )
    return {
        "items": items,
        "limit": limit,
        "offset": offset,
        "filter": {"q": q, "box_id": box_id, "tag": tag},
    }


@router.get("/items/{item_id}")
def get_item(request: Request, item_id: int) -> dict:
    actor: Actor = request.state.actor
    try:
        item = dao_items.get_by_id(actor, item_id)
    except NotFoundError:
        raise HTTPException(404, "Item not found")
    item["tags"] = dao_items.list_tags_for_item(actor, item_id)
    return item


@router.post("/items/{item_id}/move")
def move_item(request: Request, item_id: int, body: dict) -> dict:
    """Reassign an item to another box.  Body: ``{"box_id": <int>}``.
    Distinguishes "item gone" (404) from "target box bad" (400)
    the same way the HTML route does, so an agent can react
    differently to each."""
    actor: Actor = request.state.actor
    target = body.get("box_id")
    if target is None:
        raise HTTPException(400, "Missing box_id in body")
    try:
        box_id = int(target)
    except (TypeError, ValueError):
        raise HTTPException(400, "box_id must be an integer")
    try:
        dao_items.get_by_id(actor, item_id)
    except NotFoundError:
        raise HTTPException(404, "Item not found")
    try:
        dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(400, "Unknown target box")
    try:
        result = dao_items.move_to_box(actor, item_id, box_id)
    except ForbiddenError:
        raise HTTPException(403, "Token lacks permission to move items")
    return {"ok": True, "item_id": item_id, **result}


# ── Locations / rooms / tags (read-only) ────────────────────────────


@router.get("/locations")
def list_locations(request: Request) -> dict:
    actor: Actor = request.state.actor
    return {"locations": dao_locations.list_with_room_counts(actor)}


@router.get("/rooms")
def list_rooms(request: Request) -> dict:
    actor: Actor = request.state.actor
    return {"rooms": dao_rooms.list_for_picker(actor)}


@router.get("/tags")
def list_tags(request: Request) -> dict:
    actor: Actor = request.state.actor
    return {"tags": dao_tags.list_with_counts(actor)}


# Release-day verification-loop promotion is driven via the MCP
# tools ``admin_list_feedback_awaiting_release`` +
# ``admin_mark_feedback_done_on_release`` — see mcp_server.py.
# Deliberately no REST endpoint: the operator-AI conversation is
# the manual control surface today, and an automated GHA path
# would lock in design choices we want to keep flexible until the
# loop's been exercised a few times in real releases.
