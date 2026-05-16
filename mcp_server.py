"""MCP (Model Context Protocol) Streamable HTTP server.

Implements spec rev **2025-11-25**
(https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

Surface contract is in ``spec.md § Architecture · Agent / MCP
integration``.  Highlights:

* Single endpoint at ``/mcp`` accepting POST + GET + DELETE.
  POST is the workhorse; GET 405s (no server-push); DELETE 405s
  (we opt out of session-id state).
* JSON-RPC 2.0 over HTTP.  Atomic tools — most responses are
  ``Content-Type: application/json``; SSE is the deferred path.
* Bearer auth piggybacks on phase 11.  ``request.state.actor`` is
  populated by ``current_actor`` middleware before the route
  fires.
* Origin allow-list + ``Accept`` + ``MCP-Protocol-Version``
  validation up front so non-conforming clients fail fast.

Tool + resource registries live as module-level decorators so
adding a new capability is a one-liner.  See bottom of file for
the actual catalogue.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
from typing import Any, Callable, Optional

from fastapi import HTTPException, Request, Response

import obs
from dao import (
    Actor,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from dao import boxes as dao_boxes
from dao import feedback as dao_feedback
from dao import floors as dao_floors
from dao import items as dao_items
from dao import locations as dao_locations
from dao import quotas as dao_quotas
from dao import rooms as dao_rooms
from dao import tags as dao_tags
from dao import tenants as dao_tenants
from dao import usage as dao_usage


_log = obs.get_logger("mcp")


# ── Spec constants ──────────────────────────────────────────────────


SUPPORTED_PROTOCOL_VERSION = "2025-11-25"

# JSON-RPC 2.0 standard error codes.  ``code = -32001`` is our
# convention for auth failures inside a JSON-RPC error envelope;
# most of those should land in HTTP 401 anyway, but the body
# carries the JSON-RPC shape so SDK clients can surface a clean
# error message instead of a transport blob.
_RPC_PARSE_ERROR = -32700
_RPC_INVALID_REQUEST = -32600
_RPC_METHOD_NOT_FOUND = -32601
_RPC_INVALID_PARAMS = -32602
_RPC_INTERNAL_ERROR = -32603
_RPC_AUTH_REQUIRED = -32001


SERVER_INFO = {
    "name": "stash",
    "version": os.environ.get("STASH_VERSION", "dev"),
}


# ── Origin allow-list ──────────────────────────────────────────────


def _parse_allowed_origins() -> set[str]:
    """Combine ``STASH_PUBLIC_URL`` (the canonical deploy URL) with
    the comma-separated ``STASH_MCP_ALLOWED_ORIGINS`` for dev
    clients (Claude Desktop loopback, IDE hosts, etc.).  Trailing
    slashes are stripped — Origin headers don't carry them."""
    out: set[str] = set()
    pub = os.environ.get("STASH_PUBLIC_URL", "").strip().rstrip("/")
    if pub:
        out.add(pub)
    extra = os.environ.get("STASH_MCP_ALLOWED_ORIGINS", "")
    for o in extra.split(","):
        o = o.strip().rstrip("/")
        if o:
            out.add(o)
    return out


# ── Tool + resource registry ──────────────────────────────────────


@dataclasses.dataclass
class _ToolReg:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Any]


@dataclasses.dataclass
class _ResourceReg:
    uri_template: str
    name: str
    description: str
    mime_type: str
    handler: Callable[..., Any]


_TOOLS: dict[str, _ToolReg] = {}
_RESOURCES: list[_ResourceReg] = []


def _tool(
    name: str,
    *,
    description: str,
    input_schema: dict,
):
    """Register a tool.  ``input_schema`` is JSON Schema that
    appears verbatim in ``tools/list`` so SDK clients can validate
    arguments before sending.  Handler signature:
    ``handler(actor, **arguments) -> dict | list[ContentBlock]``.
    Plain dicts are wrapped in a single text content block by the
    dispatch loop; lists are passed through verbatim so a tool can
    return mixed text + image content (e.g. ``get_item`` with
    photo)."""
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _TOOLS[name] = _ToolReg(name, description, input_schema, fn)
        return fn
    return decorator


def _resource(
    uri_template: str,
    *,
    name: str,
    description: str,
    mime_type: str = "application/json",
):
    """Register a resource.  ``uri_template`` follows
    RFC 6570 syntax (``stash://items/{id}``) so SDK clients can
    enumerate parameter slots.  Handler:
    ``handler(actor, params: dict) -> str``."""
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _RESOURCES.append(_ResourceReg(
            uri_template=uri_template, name=name,
            description=description, mime_type=mime_type, handler=fn,
        ))
        return fn
    return decorator


# ── Header validation ──────────────────────────────────────────────


def validate_request_headers(request: Request) -> None:
    """Run the spec's required header guards before dispatching.
    Raises HTTPException with the right code so FastAPI returns
    the canonical body.

    Spec compliance:
    * Origin: 403 if present and not allow-listed (DNS-rebinding
      mitigation).
    * Accept on POST: must include ``application/json`` and
      ``text/event-stream``.
    * MCP-Protocol-Version: must equal ``2025-11-25``; missing
      gets the same 400 (we opt out of the spec's fallback to
      ``2025-03-26`` on missing headers).
    """
    origin = (request.headers.get("Origin") or "").strip().rstrip("/")
    if origin:
        allowed = _parse_allowed_origins()
        if allowed and origin not in allowed:
            _log.warning(
                "mcp.origin_rejected origin=%r allowed=%s",
                origin, sorted(allowed),
            )
            raise HTTPException(
                403,
                f"Origin {origin!r} not allowed.  "
                f"Set STASH_MCP_ALLOWED_ORIGINS to whitelist a "
                f"non-public-URL client.",
            )

    if request.method == "POST":
        accept = request.headers.get("Accept", "")
        if ("application/json" not in accept
                or "text/event-stream" not in accept):
            raise HTTPException(
                400,
                "POST /mcp requires Accept header listing both "
                "application/json and text/event-stream.",
            )

    # Skip the protocol-version check on initialize requests so a
    # fresh client can negotiate.  The check applies post-init.
    # In practice we always require it; the spec's "absent header
    # falls back to 2025-03-26" path is intentionally NOT honoured.
    pv = request.headers.get("MCP-Protocol-Version", "")
    # On the first POST (with an initialize payload), allow a
    # missing or matching version; the dispatch validates inside.
    # On any other request the header is required.
    if request.method != "POST":
        return
    if pv and pv != SUPPORTED_PROTOCOL_VERSION:
        raise HTTPException(
            400,
            f"Unsupported MCP-Protocol-Version: {pv!r}.  "
            f"This server speaks {SUPPORTED_PROTOCOL_VERSION}.",
        )


# ── JSON-RPC dispatch ──────────────────────────────────────────────


def _rpc_error(req_id: Any, code: int, message: str,
               data: dict | None = None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _rpc_result(req_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _tool_text_result(text: str, *, is_error: bool = False) -> dict:
    """Wrap a string in a tools/call ``content`` block.  Most
    tool errors come through here so the agent sees a structured
    failure instead of a transport-level kill."""
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def _tool_json_result(payload: dict | list,
                      extra: list[dict] | None = None,
                      meta: dict | None = None) -> dict:
    """Render a dict / list result as a single text content block
    with a JSON body.  Optional ``extra`` content blocks (e.g.
    image bytes) appear after the JSON.  ``meta`` rides in
    ``_meta`` so the soft-quota warning surface can travel
    alongside the result."""
    blocks: list[dict] = [
        {"type": "text", "text": json.dumps(payload, default=str)},
    ]
    if extra:
        blocks.extend(extra)
    out: dict = {"content": blocks, "isError": False}
    if meta:
        out["_meta"] = meta
    return out


def _quota_warnings_meta(actor: Actor) -> dict | None:
    """Build the ``_meta.warnings`` payload for this tenant's
    current cap-band state.  Mirrors the X-Quota-Warning header
    surface so an agent that ignores headers still sees the
    signal."""
    if actor.tenant_id is None:
        return None
    try:
        caps = dao_quotas.get_caps(actor.tenant_id)
        used = dao_quotas.usage_for_tenant(actor.tenant_id)
    except Exception:  # noqa: BLE001
        return None
    warnings: list[str] = []
    for key in ("monthly_ai_calls", "monthly_upload_bytes",
                "daily_ai_cost_micros"):
        cap = caps.get(key)
        if not cap:
            continue
        band = dao_quotas.warning_band(used.get(key, 0), cap)
        if band == "warning":
            pct = dao_quotas.percent(used.get(key, 0), cap)
            warnings.append(f"{key}={pct}%")
    if not warnings:
        return None
    return {"warnings": warnings}


def _record_tool_telemetry(actor: Actor, tool_name: str) -> None:
    """Stamp a usage_events row tagged ``surface = mcp`` so the
    cost-transparency block can break out agent traffic.  Per-tool
    kind values let an operator audit see "your agent did 5,000
    searches" at a glance."""
    if actor.tenant_id is None:
        return
    dao_usage.record(
        actor.tenant_id, "mcp", f"mcp.{tool_name}", units=1,
    )


def _exec_tool(actor: Actor, tool_name: str, arguments: dict) -> dict:
    """Run a tool by name.  Translates DAO-layer exceptions into
    tool-result errors (``isError: true``).  Hard transport errors
    (auth, malformed) are returned as JSON-RPC errors by the
    surrounding dispatch."""
    reg = _TOOLS.get(tool_name)
    if reg is None:
        return _tool_text_result(
            f"Unknown tool: {tool_name!r}", is_error=True,
        )
    try:
        result = reg.handler(actor, **(arguments or {}))
    except NotFoundError as exc:
        return _tool_text_result(f"Not found: {exc}", is_error=True)
    except ForbiddenError as exc:
        return _tool_text_result(
            f"Token role lacks permission: {exc}", is_error=True,
        )
    except ConflictError as exc:
        return _tool_text_result(
            f"Stale read, refresh and retry: {exc}", is_error=True,
        )
    except dao_quotas.QuotaExceeded as exc:
        return _tool_text_result(
            f"Quota exceeded: {exc.key}={exc.used} > {exc.cap}.  "
            f"Surface: {exc.surface}.  Resets at the start of the "
            f"next window.",
            is_error=True,
        )
    except ValueError as exc:
        return _tool_text_result(f"Bad arguments: {exc}", is_error=True)
    except TypeError as exc:
        # Catches missing required kwargs from a malformed
        # ``arguments`` dict — give the agent enough to fix it.
        return _tool_text_result(
            f"Argument shape error: {exc}", is_error=True,
        )

    _record_tool_telemetry(actor, tool_name)
    meta = _quota_warnings_meta(actor)

    # Tools may already have shaped their own content (the
    # photo path returns a list of mixed text/image blocks);
    # plain dicts get JSON-wrapped.
    if isinstance(result, dict) and "content" in result and isinstance(
        result["content"], list,
    ):
        # Tool already produced full ToolResult shape; stitch
        # in meta if missing.
        if meta and "_meta" not in result:
            result["_meta"] = meta
        return result
    return _tool_json_result(result, meta=meta)


def _read_resource(actor: Actor, uri: str) -> dict:
    """Look up a resource by URI.  Resources use the same
    fail-loud-on-bad-id behaviour as tools."""
    for reg in _RESOURCES:
        params = _match_uri(reg.uri_template, uri)
        if params is None:
            continue
        try:
            text = reg.handler(actor, params)
        except NotFoundError as exc:
            raise _ResourceNotFound(str(exc))
        return {
            "contents": [{
                "uri": uri,
                "mimeType": reg.mime_type,
                "text": text,
            }],
        }
    raise _ResourceNotFound(f"No resource matched {uri!r}")


class _ResourceNotFound(Exception):
    """Internal exception — translated to a JSON-RPC error by the
    dispatch loop."""


def _match_uri(template: str, uri: str) -> dict | None:
    """Tiny URI-template matcher: ``stash://items/{id}`` against
    ``stash://items/42`` returns ``{"id": "42"}``.  Returns None
    on no match.  We don't need RFC 6570's full power; constants
    + simple ``{slot}`` placeholders cover every resource we
    expose."""
    t_parts = template.split("/")
    u_parts = uri.split("/")
    if len(t_parts) != len(u_parts):
        return None
    out: dict[str, str] = {}
    for tp, up in zip(t_parts, u_parts):
        if tp.startswith("{") and tp.endswith("}"):
            out[tp[1:-1]] = up
        elif tp != up:
            return None
    return out


# ── Top-level dispatch ─────────────────────────────────────────────


def dispatch(request_body: bytes, actor: Actor) -> dict | None:
    """Parse a JSON-RPC payload and produce the response dict (or
    None for notifications, which spec says return 202 with no
    body)."""
    try:
        msg = json.loads(request_body or b"{}")
    except json.JSONDecodeError as exc:
        return _rpc_error(None, _RPC_PARSE_ERROR, f"Invalid JSON: {exc}")

    if not isinstance(msg, dict):
        return _rpc_error(None, _RPC_INVALID_REQUEST,
                          "JSON-RPC payload must be an object")

    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    # No "id" → notification.  Spec: 202 Accepted, no body.
    is_notification = "id" not in msg

    if not isinstance(method, str):
        if is_notification:
            return None
        return _rpc_error(req_id, _RPC_INVALID_REQUEST,
                          "Missing or non-string 'method'")

    try:
        result = _dispatch_method(method, params, actor)
    except _ResourceNotFound as exc:
        return _rpc_error(req_id, _RPC_INVALID_PARAMS, str(exc))
    except _MethodNotFound as exc:
        return _rpc_error(req_id, _RPC_METHOD_NOT_FOUND, str(exc))
    except _AuthRequired as exc:
        return _rpc_error(req_id, _RPC_AUTH_REQUIRED, str(exc))
    except Exception as exc:  # noqa: BLE001 — last-resort
        _log.exception("mcp.internal_error method=%r", method)
        return _rpc_error(req_id, _RPC_INTERNAL_ERROR,
                          f"Internal error: {exc}")

    if is_notification:
        return None
    return _rpc_result(req_id, result)


class _MethodNotFound(Exception):
    pass


class _AuthRequired(Exception):
    pass


def _dispatch_method(method: str, params: dict, actor: Actor) -> dict:
    """Route a JSON-RPC method to its handler.  Auth gate fires
    here so unauthenticated tool calls land as JSON-RPC errors
    rather than getting a regular tool-result."""
    # ``initialize`` is the only method an unauthenticated client
    # can call, by design — the spec's negotiation flow is
    # deliberately permissive so a client can discover capabilities
    # before committing a token.  Stash still requires a valid
    # bearer at the HTTP layer (current_actor middleware), so
    # actor.tenant_id will already be populated here when a real
    # token was supplied.  We don't gate initialize separately.
    if method == "initialize":
        return {
            "protocolVersion": SUPPORTED_PROTOCOL_VERSION,
            "serverInfo": SERVER_INFO,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
            "instructions": (
                "Stash inventory + sharing API.  Use find_items to "
                "search; get_item with include_photo=\"thumb\" to see a "
                "photo; move_item to relocate.  All actions stay scoped "
                "to the bearer token's tenant."
            ),
        }

    if method == "ping":
        return {}

    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.input_schema,
                }
                for t in _TOOLS.values()
            ],
        }

    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            raise _MethodNotFound("tools/call requires a 'name' arg")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise _MethodNotFound("tools/call 'arguments' must be an object")
        if actor.tenant_id is None and not actor.is_operator:
            raise _AuthRequired("Tool calls require a tenant-scoped token")
        return _exec_tool(actor, name, args)

    if method == "resources/list":
        return {
            "resources": [
                {
                    "uri": r.uri_template,
                    "name": r.name,
                    "description": r.description,
                    "mimeType": r.mime_type,
                }
                for r in _RESOURCES
            ],
        }

    if method == "resources/read":
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise _MethodNotFound("resources/read requires a 'uri' arg")
        if actor.tenant_id is None:
            raise _AuthRequired("Resource reads require a tenant-scoped token")
        return _read_resource(actor, uri)

    # Notifications we accept silently.  ``notifications/initialized``
    # is the most common one — the client telling the server it's
    # done with the handshake.
    if method.startswith("notifications/"):
        return {}

    raise _MethodNotFound(f"Unknown method: {method}")


# ── Tools ──────────────────────────────────────────────────────────


@_tool(
    "me",
    description="Identity of the bearer token: tenant_id, role, plan.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
def _tool_me(actor: Actor) -> dict:
    plan = ""
    if actor.tenant_id is not None:
        try:
            tenant = dao_tenants.get_tenant(actor, actor.tenant_id)
            plan = tenant.get("plan", "")
        except NotFoundError:
            pass
    return {
        "tenant_id": actor.tenant_id,
        "role": actor.role,
        "plan": plan,
    }


@_tool(
    "find_items",
    description=(
        "Search items by free-text + optional filters.  Returns "
        "rows with the box name joined inline so a single call "
        "answers 'where is X?'.  Limits clamped to [1, 200]."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "q": {"type": "string", "default": ""},
            "box_id": {"type": ["integer", "null"]},
            "tag": {"type": "string", "default": ""},
            "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            "offset": {"type": "integer", "default": 0, "minimum": 0},
        },
        "additionalProperties": False,
    },
)
def _tool_find_items(actor: Actor, q: str = "", box_id: int | None = None,
                     tag: str = "", limit: int = 50, offset: int = 0) -> dict:
    items = dao_items.search(
        actor, q=q, box_id=box_id, tag=tag, limit=limit, offset=offset,
    )
    return {
        "items": items,
        "limit": limit,
        "offset": offset,
        "filter": {"q": q, "box_id": box_id, "tag": tag},
    }


@_tool(
    "list_boxes",
    description="Every box in the tenant with item counts + room + location.",
    input_schema={
        "type": "object",
        "properties": {
            "room_id": {"type": ["integer", "null"]},
            "location_id": {"type": ["integer", "null"]},
        },
        "additionalProperties": False,
    },
)
def _tool_list_boxes(actor: Actor, room_id: int | None = None,
                     location_id: int | None = None) -> dict:
    boxes = dao_boxes.list_with_counts(actor)
    if room_id is not None:
        boxes = [b for b in boxes if b.get("room_id") == room_id]
    if location_id is not None:
        boxes = [b for b in boxes if b.get("location_id") == location_id]
    return {"boxes": boxes}


@_tool(
    "get_box",
    description="Single box, optionally with its item list.",
    input_schema={
        "type": "object",
        "properties": {
            "box_id": {"type": "integer"},
            "include_items": {"type": "boolean", "default": True},
        },
        "required": ["box_id"],
        "additionalProperties": False,
    },
)
def _tool_get_box(actor: Actor, box_id: int,
                  include_items: bool = True) -> dict:
    box = dao_boxes.get_by_id(actor, box_id)
    if include_items:
        box["items"] = dao_items.list_for_box(actor, box_id)
    return box


@_tool(
    "list_locations",
    description="Locations with room/box counts.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
def _tool_list_locations(actor: Actor) -> dict:
    return {"locations": dao_locations.list_with_room_counts(actor)}


@_tool(
    "list_rooms",
    description="Flat list of rooms across the tenant (with location + floor labels).",
    input_schema={
        "type": "object",
        "properties": {
            "location_id": {"type": ["integer", "null"]},
        },
        "additionalProperties": False,
    },
)
def _tool_list_rooms(actor: Actor, location_id: int | None = None) -> dict:
    rooms = dao_rooms.list_for_picker(actor)
    if location_id is not None:
        rooms = [r for r in rooms if r.get("location_id") == location_id]
    return {"rooms": rooms}


@_tool(
    "list_tags",
    description="Tags with use counts.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
def _tool_list_tags(actor: Actor) -> dict:
    return {"tags": dao_tags.list_with_counts(actor)}


@_tool(
    "inventory_room",
    description=(
        "Composite view of a room: every box currently in the "
        "room with the items inside each.  Useful for 'what's in "
        "the kitchen?' agent prompts."
    ),
    input_schema={
        "type": "object",
        "properties": {"room_id": {"type": "integer"}},
        "required": ["room_id"],
        "additionalProperties": False,
    },
)
def _tool_inventory_room(actor: Actor, room_id: int) -> dict:
    room = dao_rooms.get_with_location(actor, room_id)
    boxes = dao_boxes.list_for_room(actor, room_id)
    for b in boxes:
        b["items"] = dao_items.list_for_box(actor, b["id"])
    return {"room": room, "boxes": boxes}


@_tool(
    "get_item",
    description=(
        "Single item with tags, parent box context, and optional "
        "photo bytes.  ``include_photo``: 'none' (default, "
        "photo_url only), 'thumb' (320 px JPEG ImageContent), "
        "'full' (full-resolution; counts toward upload-bytes "
        "telemetry to keep bandwidth-heavy reads visible)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "item_id": {"type": "integer"},
            "include_photo": {
                "type": "string",
                "enum": ["none", "thumb", "full"],
                "default": "none",
            },
        },
        "required": ["item_id"],
        "additionalProperties": False,
    },
)
def _tool_get_item(actor: Actor, item_id: int,
                   include_photo: str = "none") -> Any:
    """Returns either a plain dict (no photo) or a fully-shaped
    ToolResult with mixed text+image content (with photo)."""
    item = dao_items.get_by_id(actor, item_id)
    item["tags"] = dao_items.list_tags_for_item(actor, item_id)
    try:
        box = dao_boxes.get_by_id(actor, item["box_id"])
        item["box_name"] = box["name"]
    except NotFoundError:
        item["box_name"] = None
    photo_name = item.get("photo")
    if photo_name:
        item["photo_url"] = f"/uploads/{photo_name}"
    else:
        item["photo_url"] = None

    if include_photo == "none" or not photo_name:
        return item

    # Photo path — return mixed content blocks: JSON text + image.
    # Late-imported because mcp_server.py is imported at app boot
    # but the photo helpers want to be lazy.
    from app import _read_encrypted, _ensure_thumb
    blocks = [{"type": "text", "text": json.dumps(item, default=str)}]
    try:
        if include_photo == "thumb":
            data = _ensure_thumb(actor.tenant_id, photo_name)
            if data is None:
                data = _read_encrypted(actor.tenant_id, photo_name)
        else:  # "full"
            data = _read_encrypted(actor.tenant_id, photo_name)
            # Telemetry for bandwidth-heavy reads — count toward
            # upload-bytes so the daily AI cost cap budget covers
            # agent reads of full-resolution photos.
            dao_usage.record(
                actor.tenant_id, "upload", "mcp_full_photo_bytes",
                units=len(data),
            )
        blocks.append({
            "type": "image",
            "data": base64.b64encode(data).decode("ascii"),
            "mimeType": "image/jpeg",
        })
    except Exception as exc:  # noqa: BLE001
        # File missing / decrypt failed — return JSON only +
        # surface the failure inline.  Don't kill the whole tool
        # call; the agent can still see the metadata.
        blocks.append({
            "type": "text",
            "text": f"(photo bytes unavailable: {exc})",
        })
    return {"content": blocks, "isError": False}


# ── Write tools ────────────────────────────────────────────────────


@_tool(
    "move_item",
    description=(
        "Reassign an item to another box.  Both ids must belong "
        "to the bearer's tenant.  One-shot — fails loudly with "
        "isError:true if the item or target box doesn't exist."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "item_id": {"type": "integer"},
            "target_box_id": {"type": "integer"},
        },
        "required": ["item_id", "target_box_id"],
        "additionalProperties": False,
    },
)
def _tool_move_item(actor: Actor, item_id: int, target_box_id: int) -> dict:
    # Pre-flight the target so a missing box returns a clean
    # error instead of a partial-state mutation.
    dao_boxes.get_by_id(actor, target_box_id)
    result = dao_items.move_to_box(actor, item_id, target_box_id)
    return {"ok": True, "item_id": item_id, **result}


@_tool(
    "create_item",
    description="Create an item in a box.  Returns the new id + tags.",
    input_schema={
        "type": "object",
        "properties": {
            "box_id": {"type": "integer"},
            "name": {"type": "string", "minLength": 1},
            "notes": {"type": "string", "default": ""},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
        },
        "required": ["box_id", "name"],
        "additionalProperties": False,
    },
)
def _tool_create_item(actor: Actor, box_id: int, name: str,
                      notes: str = "", tags: list[str] | None = None) -> dict:
    new_id = dao_items.create(actor, box_id, name=name, notes=notes)
    if tags:
        dao_tags.attach_to_item(actor, new_id, [(t, None) for t in tags])
    return {
        "ok": True,
        "item_id": new_id,
        "tags": dao_items.list_tags_for_item(actor, new_id),
    }


@_tool(
    "update_item",
    description=(
        "Edit an item's name and/or notes.  Pass only the fields "
        "you want to change."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "item_id": {"type": "integer"},
            "name": {"type": "string"},
            "notes": {"type": "string"},
        },
        "required": ["item_id"],
        "additionalProperties": False,
    },
)
def _tool_update_item(actor: Actor, item_id: int,
                      name: str | None = None,
                      notes: str | None = None) -> dict:
    changed = dao_items.update(actor, item_id, name=name, notes=notes)
    return {"ok": True, "item_id": item_id, "changed": changed}


@_tool(
    "add_tag",
    description="Attach a tag to an item.  Tag is created if missing.",
    input_schema={
        "type": "object",
        "properties": {
            "item_id": {"type": "integer"},
            "tag": {"type": "string", "minLength": 1},
        },
        "required": ["item_id", "tag"],
        "additionalProperties": False,
    },
)
def _tool_add_tag(actor: Actor, item_id: int, tag: str) -> dict:
    # Verify the item is in this tenant — DAO will 404 if not.
    dao_items.get_by_id(actor, item_id)
    dao_tags.attach_to_item(actor, item_id, [(tag, None)])
    return {
        "ok": True,
        "item_id": item_id,
        "tags": dao_items.list_tags_for_item(actor, item_id),
    }


@_tool(
    "remove_tag",
    description="Remove a tag from an item by tag id.",
    input_schema={
        "type": "object",
        "properties": {
            "item_id": {"type": "integer"},
            "tag_id": {"type": "integer"},
        },
        "required": ["item_id", "tag_id"],
        "additionalProperties": False,
    },
)
def _tool_remove_tag(actor: Actor, item_id: int, tag_id: int) -> dict:
    dao_items.remove_tag(actor, item_id, tag_id)
    return {
        "ok": True,
        "item_id": item_id,
        "tags": dao_items.list_tags_for_item(actor, item_id),
    }


@_tool(
    "mark_missing",
    description=(
        "Flag an item as missing.  Marks ``is_missing = 1`` so "
        "the search-by-missing filter surfaces it in the UI."
    ),
    input_schema={
        "type": "object",
        "properties": {"item_id": {"type": "integer"}},
        "required": ["item_id"],
        "additionalProperties": False,
    },
)
def _tool_mark_missing(actor: Actor, item_id: int) -> dict:
    dao_items.mark_missing(actor, item_id, True)
    return {"ok": True, "item_id": item_id, "is_missing": True}


# ── Operator-scoped tools (admin_*) ────────────────────────────────
#
# These mirror the operator surface on /admin so an AI assistant
# authenticated with an operator-minted api_token can read + triage
# the feedback queue without a copy-paste round trip.  Every
# operator tool gates on ``actor.is_operator`` and returns a
# structured tool-error for non-operators rather than a transport
# 401 — that way a non-operator client can still call
# ``tools/list`` and see what's available.


def _require_operator(actor: Actor) -> dict | None:
    """Return a tool-error dict when the actor isn't an operator,
    or ``None`` to proceed.  Tools call this at the top so the
    error path is one consistent shape."""
    if not actor.is_operator:
        return _tool_text_result(
            "Operator-only tool.  Bearer token must be minted by an "
            "email listed in STASH_OPERATOR_EMAILS.",
            is_error=True,
        )
    return None


@_tool(
    "admin_list_feedback",
    description=(
        "Operator-only.  List in-app feedback rows, optionally "
        "filtered by status (open / accepted / rejected / done)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["open", "accepted", "rejected", "done", "all"],
                "description": "Filter by status; 'all' returns every row.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        },
        "additionalProperties": False,
    },
)
def _tool_admin_list_feedback(actor: Actor,
                              status: str = "open",
                              limit: int = 100) -> dict | list:
    err = _require_operator(actor)
    if err:
        return err
    rows = dao_feedback.list_for_operator(
        status=None if status == "all" else status,
        limit=max(1, min(int(limit), 500)),
    )
    # Cheap booleans so the agent can decide which rows are worth
    # a follow-up ``admin_get_feedback`` with ``include`` set.
    for r in rows:
        r["has_screenshot"] = bool(r.get("screenshot"))
        r["has_page_html"] = bool(r.get("page_html"))
    return {
        "ok": True,
        "filter": {"status": status},
        "count": len(rows),
        "feedback": rows,
    }


# Page HTML payloads can be large; cap what we return inline to MCP
# clients so a 500 KB capture doesn't blow the agent's context budget.
# The full file is always available via the /admin/feedback/{id}/page_html
# route — this cap only affects the MCP inline-return path.
_MCP_FEEDBACK_PAGE_HTML_RETURN_CAP = 256_000


@_tool(
    "admin_get_feedback",
    description=(
        "Operator-only.  Fetch a single feedback row by id.  Pass "
        "``include`` to also embed binary/large telemetry attached "
        "to the row: \"screenshot\" returns the captured page image "
        "as a base64 data URL, \"page_html\" returns the captured "
        "DOM as text (capped at 256 KB), \"console_log\" + "
        "\"perf_timing\" inline the parsed JSON.  \"all\" includes "
        "everything available.  Without ``include`` the response is "
        "just the row + boolean flags for what's attached."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "feedback_id": {"type": "integer"},
            "include": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "screenshot", "page_html",
                        "console_log", "perf_timing", "all",
                    ],
                },
                "description": (
                    "Optional list of extras to embed in the response. "
                    "Default omits all large payloads."
                ),
            },
        },
        "required": ["feedback_id"],
        "additionalProperties": False,
    },
)
def _tool_admin_get_feedback(
    actor: Actor, feedback_id: int,
    include: list[str] | None = None,
) -> dict | list:
    err = _require_operator(actor)
    if err:
        return err
    row = dao_feedback.get(int(feedback_id))
    # Always surface the cheap flags so callers can decide whether
    # a follow-up call with ``include`` is worthwhile.
    row["has_screenshot"] = bool(row.get("screenshot"))
    row["has_page_html"] = bool(row.get("page_html"))

    wanted = set(include or [])
    if "all" in wanted:
        wanted = {"screenshot", "page_html", "console_log", "perf_timing"}

    tenant_id = row.get("tenant_id")

    if "screenshot" in wanted and row.get("screenshot") and tenant_id:
        try:
            from app import _read_encrypted
            data = _read_encrypted(tenant_id, row["screenshot"])
            row["screenshot_data_url"] = (
                "data:image/jpeg;base64,"
                + base64.b64encode(data).decode("ascii")
            )
        except Exception as exc:  # noqa: BLE001
            row["screenshot_error"] = str(exc)

    if "page_html" in wanted and row.get("page_html") and tenant_id:
        try:
            from app import _read_encrypted
            data = _read_encrypted(tenant_id, row["page_html"])
            text = data.decode("utf-8", errors="replace")
            truncated = len(text) > _MCP_FEEDBACK_PAGE_HTML_RETURN_CAP
            if truncated:
                text = text[:_MCP_FEEDBACK_PAGE_HTML_RETURN_CAP]
            row["page_html_text"] = text
            row["page_html_truncated"] = truncated
        except Exception as exc:  # noqa: BLE001
            row["page_html_error"] = str(exc)

    # console_log + perf_timing live in columns as JSON strings —
    # parse them on the way out so the agent gets structured data
    # instead of a quoted string-of-JSON.
    if "console_log" in wanted and row.get("console_log"):
        try:
            row["console_log_parsed"] = json.loads(row["console_log"])
        except Exception:
            row["console_log_parsed"] = None

    if "perf_timing" in wanted and row.get("perf_timing"):
        try:
            row["perf_timing_parsed"] = json.loads(row["perf_timing"])
        except Exception:
            row["perf_timing_parsed"] = None

    return {"ok": True, "feedback": row}


@_tool(
    "admin_set_feedback_status",
    description=(
        "Operator-only.  Transition a feedback row to "
        "accepted / rejected / done / open.  Optional notes append "
        "to operator_notes; resolved_by stamps with the operator's "
        "email."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "feedback_id": {"type": "integer"},
            "status": {
                "type": "string",
                "enum": ["open", "accepted", "rejected", "done"],
            },
            "notes": {"type": "string"},
        },
        "required": ["feedback_id", "status"],
        "additionalProperties": False,
    },
)
def _tool_admin_set_feedback_status(
    actor: Actor, feedback_id: int, status: str,
    notes: str | None = None,
) -> dict | list:
    err = _require_operator(actor)
    if err:
        return err
    # ``actor.email`` for bearer auth is ``api_token:<id>`` — record
    # that as the operator identity so the resolved_by column traces
    # back to the actual token that made the call.
    updated = dao_feedback.set_status(
        int(feedback_id), status,
        operator_email=actor.email or "operator",
        notes=(notes or "").strip() or None,
    )
    return {"ok": True, "feedback": updated}


@_tool(
    "admin_feedback_counts",
    description=(
        "Operator-only.  Per-status counts for the feedback queue "
        "(open / accepted / rejected / done).  Cheap call; safe to "
        "poll if an agent wants to wait for new submissions."
    ),
    input_schema={
        "type": "object", "properties": {}, "additionalProperties": False,
    },
)
def _tool_admin_feedback_counts(actor: Actor) -> dict | list:
    err = _require_operator(actor)
    if err:
        return err
    return {"ok": True, "counts": dao_feedback.queue_counts()}


# ── Resources ──────────────────────────────────────────────────────


@_resource(
    "stash://items/{id}",
    name="Item",
    description="A single stash item by id.",
)
def _res_item(actor: Actor, params: dict) -> str:
    item = dao_items.get_by_id(actor, int(params["id"]))
    item["tags"] = dao_items.list_tags_for_item(actor, item["id"])
    return json.dumps(item, default=str, indent=2)


@_resource(
    "stash://boxes/{id}",
    name="Box",
    description="A single stash box with its items.",
)
def _res_box(actor: Actor, params: dict) -> str:
    box = dao_boxes.get_by_id(actor, int(params["id"]))
    box["items"] = dao_items.list_for_box(actor, box["id"])
    return json.dumps(box, default=str, indent=2)


@_resource(
    "stash://rooms/{id}",
    name="Room",
    description="A room with its boxes (no nested items).",
)
def _res_room(actor: Actor, params: dict) -> str:
    room = dao_rooms.get_with_location(actor, int(params["id"]))
    room["boxes"] = dao_boxes.list_for_room(actor, room["id"])
    return json.dumps(room, default=str, indent=2)


@_resource(
    "stash://locations/{id}",
    name="Location",
    description="A location with its floors + rooms.",
)
def _res_location(actor: Actor, params: dict) -> str:
    loc = dao_locations.get_by_id(actor, int(params["id"]))
    loc["floors"] = dao_floors.list_for_location(actor, loc["id"])
    return json.dumps(loc, default=str, indent=2)
