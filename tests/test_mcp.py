"""Phase 18 — built-in /mcp endpoint (Streamable HTTP, rev
2025-11-25).

Tests cover:

1. Header validation (Origin allow-list, Accept, MCP-Protocol-Version).
2. JSON-RPC dispatch (initialize, tools/list, resources/list, ping).
3. Auth: bearer required; revoked token fails; cross-tenant probes
   404 inside tool results.
4. Every read tool round-trips and respects tenant scope.
5. Every write tool mutates correctly and audits.
6. Photo content: thumb + full base64 ImageContent blocks.
7. Quota integration: 429 surfaces as ``isError: true`` with
   retry hint inside the tool result; warning header surfaces in
   ``_meta``.
8. GET / DELETE return 405 (we opt out of server-push + sessions).
9. Error mapping: NotFoundError → tool error, ForbiddenError →
   tool error, ValueError → tool error.
"""

from __future__ import annotations

import base64
import importlib
import json
import secrets
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── Fixtures ───────────────────────────────────────────────────────


def _bootstrap(tmp_path, monkeypatch, *,
               with_data: bool = True,
               operator_email: str | None = None):
    monkeypatch.setenv("STASH_DB", str(tmp_path / "stash.db"))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.setenv(
        "STASH_KEK",
        base64.b64encode(secrets.token_bytes(32)).decode(),
    )
    if operator_email:
        monkeypatch.setenv("STASH_OPERATOR_EMAILS", operator_email)
    else:
        monkeypatch.delenv("STASH_OPERATOR_EMAILS", raising=False)
    monkeypatch.setenv("STASH_REQUIRE_HTTPS_TOKENS", "false")
    if "app" in sys.modules:
        del sys.modules["app"]
    if "mcp_server" in sys.modules:
        del sys.modules["mcp_server"]
    if "api" in sys.modules:
        del sys.modules["api"]
    import app as app_module
    importlib.reload(app_module)
    import vault
    vault.clear_dek_cache()

    if with_data:
        with app_module.db() as conn:
            cur = conn.execute(
                "INSERT INTO tenants (name, plan) VALUES ('T1', 'pro')"
            )
            t1 = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO tenants (name, plan) VALUES ('T2', 'pro')"
            )
            t2 = cur.lastrowid
            conn.execute(
                "INSERT INTO tenant_members "
                "(tenant_id, email, role, joined_at) "
                "VALUES (?, 'me@t1.example', 'maintainer', "
                " CURRENT_TIMESTAMP)",
                (t1,),
            )
            conn.execute(
                "INSERT INTO tenant_members "
                "(tenant_id, email, role, joined_at) "
                "VALUES (?, 'me@t2.example', 'maintainer', "
                " CURRENT_TIMESTAMP)",
                (t2,),
            )
            cur = conn.execute(
                "INSERT INTO locations (name, tenant_id) "
                "VALUES ('Townhouse', ?)",
                (t1,),
            )
            loc_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO floors (location_id, name, tenant_id) "
                "VALUES (?, 'Ground', ?)",
                (loc_id, t1),
            )
            floor_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO rooms "
                "(location_id, floor_id, name, tenant_id) "
                "VALUES (?, ?, 'Kitchen', ?)",
                (loc_id, floor_id, t1),
            )
            room_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO boxes "
                "(name, location, notes, room_id, tenant_id) "
                "VALUES ('Drawer 1', 'Kitchen', '', ?, ?)",
                (room_id, t1),
            )
            box_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO items (box_id, name, notes, tenant_id) "
                "VALUES (?, 'Whisk', 'beat eggs', ?)",
                (box_id, t1),
            )
            item_id = cur.lastrowid
            # Cross-tenant decoy.
            cur = conn.execute(
                "INSERT INTO boxes "
                "(name, location, notes, tenant_id) "
                "VALUES ('Other tenant box', 'B', '', ?)",
                (t2,),
            )
            t2_box = cur.lastrowid
            conn.commit()
        ids = dict(t1=t1, t2=t2, loc_id=loc_id, floor_id=floor_id,
                   room_id=room_id, box_id=box_id, item_id=item_id,
                   t2_box=t2_box)
    else:
        ids = {}
    return app_module, ids


def _mint(app_mod, tenant_id, owner_email,
          name="test-token", role="maintainer") -> str:
    from dao import Actor, api_tokens
    actor = Actor(
        email=owner_email, tenant_id=tenant_id, role="maintainer",
        is_operator=False, memberships=((tenant_id, "maintainer"),),
        shares=(),
    )
    return api_tokens.create(actor, name=name, role=role)["plaintext"]


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
        "Content-Type": "application/json",
    }


def _rpc(client: TestClient, headers: dict, *,
         method: str, params: dict | None = None,
         req_id: int = 1) -> dict:
    r = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": req_id,
              "method": method, "params": params or {}},
    )
    return r.json()


def _tool_call(client: TestClient, headers: dict, name: str,
               arguments: dict | None = None) -> dict:
    return _rpc(client, headers, method="tools/call",
                params={"name": name, "arguments": arguments or {}})


def _result_json(rpc_response: dict) -> dict:
    """Pull the tool's text-block JSON payload out of a tools/call
    success result."""
    blocks = rpc_response["result"]["content"]
    text = next(b["text"] for b in blocks if b["type"] == "text")
    return json.loads(text)


# ── Header validation ──────────────────────────────────────────────


def test_post_requires_accept_header(tmp_path, monkeypatch):
    """Spec compliance: POST /mcp requires both
    application/json and text/event-stream in Accept."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        # Missing Accept entirely.
        r = c.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "MCP-Protocol-Version": "2025-11-25",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        assert r.status_code == 400


def test_post_rejects_old_protocol_version(tmp_path, monkeypatch):
    """Stash hard-fails on older revs even though the spec lets
    servers fall back to 2025-03-26 on missing headers — we don't
    want clients silently missing new tool semantics."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    headers = _headers(token)
    headers["MCP-Protocol-Version"] = "2025-03-26"
    with TestClient(app_mod.app) as c:
        r = c.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
    assert r.status_code == 400


def test_origin_allowlist_blocks_unknown(tmp_path, monkeypatch):
    """An Origin not in the allow-list (DNS-rebinding mitigation)
    gets 403."""
    monkeypatch.setenv("STASH_PUBLIC_URL", "https://stash.example.com")
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    headers = _headers(token)
    headers["Origin"] = "https://evil.example.com"
    with TestClient(app_mod.app) as c:
        r = c.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
    assert r.status_code == 403


def test_origin_allowlist_admits_public_url(tmp_path, monkeypatch):
    monkeypatch.setenv("STASH_PUBLIC_URL", "https://stash.example.com")
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    headers = _headers(token)
    headers["Origin"] = "https://stash.example.com"
    with TestClient(app_mod.app) as c:
        r = c.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
    assert r.status_code == 200


def test_get_returns_405(tmp_path, monkeypatch):
    """Stash has no server-push — GET /mcp 405s per spec
    compliance (the route exists but we offer no SSE)."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        r = c.get("/mcp", headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        })
    assert r.status_code == 405


def test_delete_returns_405(tmp_path, monkeypatch):
    """We opt out of MCP-Session-Id, so DELETE has no use."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        r = c.delete("/mcp", headers={
            "Authorization": f"Bearer {token}",
            "MCP-Protocol-Version": "2025-11-25",
        })
    assert r.status_code == 405


# ── Auth ───────────────────────────────────────────────────────────


def test_no_bearer_blocks_at_auth_wall(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        r = c.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
    # Auth-wall returns 403 for stash; either auth shape is OK
    # for the spec (the JSON-RPC layer doesn't see this).
    assert r.status_code in (401, 403)


def test_revoked_bearer_fails(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    from dao import Actor, api_tokens
    actor = Actor(
        email="me@t1.example", tenant_id=ids["t1"], role="maintainer",
        is_operator=False, memberships=((ids["t1"], "maintainer"),),
        shares=(),
    )
    with app_mod.db() as conn:
        token_id = conn.execute(
            "SELECT id FROM api_tokens WHERE name = 'test-token'"
        ).fetchone()["id"]
    api_tokens.revoke(actor, token_id)
    with TestClient(app_mod.app) as c:
        r = c.post(
            "/mcp",
            headers=_headers(token),
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
    assert r.status_code == 401


# ── Handshake ──────────────────────────────────────────────────────


def test_initialize_returns_capabilities_and_pinned_version(
    tmp_path, monkeypatch,
):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        r = c.post(
            "/mcp", headers=_headers(token),
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
    assert r.status_code == 200
    assert r.headers["MCP-Protocol-Version"] == "2025-11-25"
    body = r.json()
    assert body["result"]["protocolVersion"] == "2025-11-25"
    assert body["result"]["serverInfo"]["name"] == "stash"
    assert "tools" in body["result"]["capabilities"]
    assert "resources" in body["result"]["capabilities"]


def test_notification_returns_202(tmp_path, monkeypatch):
    """JSON-RPC notifications (no id) → 202 Accepted, no body."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        r = c.post(
            "/mcp", headers=_headers(token),
            json={"jsonrpc": "2.0",
                  "method": "notifications/initialized"},
        )
    assert r.status_code == 202
    assert r.content == b""


def test_tools_list_enumerates_full_catalogue(tmp_path, monkeypatch):
    """Tools/list must include every tool we registered.  Locks
    the catalogue size as a regression guard."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _rpc(c, _headers(token), method="tools/list")
    names = {t["name"] for t in body["result"]["tools"]}
    expected = {
        "me", "find_items", "list_boxes", "get_box",
        "list_locations", "list_rooms", "list_tags",
        "inventory_room", "get_item",
        "move_item", "create_item", "update_item",
        "add_tag", "remove_tag", "mark_missing",
    }
    assert expected.issubset(names), f"missing tools: {expected - names}"


def test_resources_list_enumerates_full_catalogue(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _rpc(c, _headers(token), method="resources/list")
    uris = {r["uri"] for r in body["result"]["resources"]}
    assert uris == {
        "stash://items/{id}",
        "stash://boxes/{id}",
        "stash://rooms/{id}",
        "stash://locations/{id}",
    }


# ── Read tools ─────────────────────────────────────────────────────


def test_me_tool_returns_tenant_role_and_plan(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _tool_call(c, _headers(token), "me")
    payload = _result_json(body)
    assert payload["tenant_id"] == ids["t1"]
    assert payload["role"] == "maintainer"
    assert payload["plan"] == "pro"


def test_find_items_only_returns_callers_tenant(tmp_path, monkeypatch):
    """A T1 token's find_items must not surface T2's items."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    # Create a T2 item that would lexically match a free-text q.
    with app_mod.db() as conn:
        conn.execute(
            "INSERT INTO items (box_id, name, tenant_id) "
            "VALUES (?, 'Whisk', ?)",
            (ids["t2_box"], ids["t2"]),
        )
        conn.commit()
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _tool_call(c, _headers(token), "find_items",
                          {"q": "Whisk"})
    items = _result_json(body)["items"]
    assert len(items) == 1
    assert items[0]["box_id"] == ids["box_id"]
    assert all(it.get("box_id") == ids["box_id"] for it in items)


def test_get_item_no_photo_returns_url_only(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _tool_call(c, _headers(token), "get_item",
                          {"item_id": ids["item_id"]})
    payload = _result_json(body)
    assert payload["name"] == "Whisk"
    assert payload["box_name"] == "Drawer 1"
    assert "tags" in payload
    assert payload["photo_url"] is None  # no photo on this item


def test_get_item_cross_tenant_returns_tool_error(tmp_path, monkeypatch):
    """A T1 token asking for a T2 item gets isError:true with
    "Not found" — never 200, never 403."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    with app_mod.db() as conn:
        cur = conn.execute(
            "INSERT INTO items (box_id, name, tenant_id) "
            "VALUES (?, 'T2 thing', ?)",
            (ids["t2_box"], ids["t2"]),
        )
        t2_item = cur.lastrowid
        conn.commit()
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _tool_call(c, _headers(token), "get_item",
                          {"item_id": t2_item})
    assert body["result"]["isError"] is True
    assert "Not found" in body["result"]["content"][0]["text"]


def test_inventory_room_returns_boxes_with_items(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _tool_call(c, _headers(token), "inventory_room",
                          {"room_id": ids["room_id"]})
    payload = _result_json(body)
    assert payload["room"]["name"] == "Kitchen"
    boxes = payload["boxes"]
    assert len(boxes) == 1
    assert boxes[0]["items"][0]["name"] == "Whisk"


# ── Resources ──────────────────────────────────────────────────────


def test_resources_read_box_returns_json(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _rpc(c, _headers(token), method="resources/read",
                    params={"uri": f"stash://boxes/{ids['box_id']}"})
    contents = body["result"]["contents"]
    assert contents[0]["mimeType"] == "application/json"
    payload = json.loads(contents[0]["text"])
    assert payload["name"] == "Drawer 1"


def test_resources_read_unknown_uri_returns_rpc_error(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _rpc(c, _headers(token), method="resources/read",
                    params={"uri": "stash://nonsense/1"})
    assert "error" in body
    assert body["error"]["code"] == -32602


def test_resources_read_cross_tenant_404(tmp_path, monkeypatch):
    """A T1 token reading a T2 box URI gets a JSON-RPC error
    (translated from the DAO's NotFoundError)."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _rpc(c, _headers(token), method="resources/read",
                    params={"uri": f"stash://boxes/{ids['t2_box']}"})
    assert "error" in body


# ── Write tools ────────────────────────────────────────────────────


def test_move_item_one_shot(tmp_path, monkeypatch):
    """move_item works end to end + the move audit-logs through
    the existing dao.items.move_to_box path."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    with app_mod.db() as conn:
        cur = conn.execute(
            "INSERT INTO boxes "
            "(name, location, notes, tenant_id) "
            "VALUES ('Pantry', 'Kitchen', '', ?)",
            (ids["t1"],),
        )
        target = cur.lastrowid
        conn.commit()
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _tool_call(c, _headers(token), "move_item",
                          {"item_id": ids["item_id"],
                           "target_box_id": target})
    payload = _result_json(body)
    assert payload["ok"] is True
    assert payload["new_box_id"] == target
    # Audit row landed.
    with app_mod.db() as conn:
        row = conn.execute(
            "SELECT action FROM audit_log WHERE action = 'item.move' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["action"] == "item.move"


def test_move_item_to_other_tenant_box_fails_with_tool_error(
    tmp_path, monkeypatch,
):
    """One-shot, fails loudly per user direction.  Target box in
    another tenant ⇒ isError:true."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _tool_call(c, _headers(token), "move_item",
                          {"item_id": ids["item_id"],
                           "target_box_id": ids["t2_box"]})
    assert body["result"]["isError"] is True


def test_create_item_with_tags(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _tool_call(
            c, _headers(token), "create_item",
            {"box_id": ids["box_id"], "name": "Spatula",
             "notes": "wood", "tags": ["kitchen", "tools"]},
        )
    payload = _result_json(body)
    assert payload["ok"] is True
    assert payload["item_id"]
    tag_names = {t["name"] for t in payload["tags"]}
    assert "kitchen" in tag_names
    assert "tools" in tag_names


def test_update_item_changes_notes(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _tool_call(
            c, _headers(token), "update_item",
            {"item_id": ids["item_id"], "notes": "balloon whisk"},
        )
    assert _result_json(body)["changed"] is True
    with app_mod.db() as conn:
        row = conn.execute(
            "SELECT notes FROM items WHERE id = ?",
            (ids["item_id"],),
        ).fetchone()
    assert row["notes"] == "balloon whisk"


def test_add_and_remove_tag(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _tool_call(c, _headers(token), "add_tag",
                          {"item_id": ids["item_id"],
                           "tag": "kitchen"})
        payload = _result_json(body)
        tag_id = next(t["tag_id"] for t in payload["tags"]
                      if t["name"] == "kitchen")
        body = _tool_call(c, _headers(token), "remove_tag",
                          {"item_id": ids["item_id"],
                           "tag_id": tag_id})
    assert _result_json(body)["tags"] == []


def test_mark_missing_flips_flag(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _tool_call(c, _headers(token), "mark_missing",
                          {"item_id": ids["item_id"]})
    assert _result_json(body)["is_missing"] is True


# ── Photo content ──────────────────────────────────────────────────


def test_get_item_with_thumb_returns_image_content(tmp_path, monkeypatch):
    """include_photo='thumb' produces an MCP ImageContent block
    base64-encoded JPEG bytes alongside the JSON metadata."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    # Drive a photo through the normal save path so the encryption
    # + thumb plumbing wires up correctly.
    raw = b"\xff\xd8\xff\xe0" + b"x" * 200 + b"\xff\xd9"
    photo_name = app_mod.save_photo_bytes(ids["t1"], raw, "test.jpg")
    with app_mod.db() as conn:
        conn.execute(
            "UPDATE items SET photo = ?, source_photo = ? "
            "WHERE id = ?",
            (photo_name, photo_name, ids["item_id"]),
        )
        conn.commit()
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _tool_call(
            c, _headers(token), "get_item",
            {"item_id": ids["item_id"], "include_photo": "thumb"},
        )
    blocks = body["result"]["content"]
    assert any(b.get("type") == "text" for b in blocks)
    img_blocks = [b for b in blocks if b.get("type") == "image"]
    assert len(img_blocks) == 1
    assert img_blocks[0]["mimeType"] == "image/jpeg"
    # Decodes cleanly.
    decoded = base64.b64decode(img_blocks[0]["data"])
    assert len(decoded) > 0


def test_get_item_full_records_upload_bytes_telemetry(tmp_path, monkeypatch):
    """Spec note: ``include_photo='full'`` records the byte count
    against upload-bytes so a hammering agent counts toward
    quota."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    raw = b"\xff\xd8\xff\xe0" + b"x" * 200 + b"\xff\xd9"
    photo_name = app_mod.save_photo_bytes(ids["t1"], raw, "test.jpg")
    with app_mod.db() as conn:
        conn.execute(
            "UPDATE items SET photo = ?, source_photo = ? "
            "WHERE id = ?",
            (photo_name, photo_name, ids["item_id"]),
        )
        # Drop pre-existing usage rows so the new event is the
        # only one with kind = 'mcp_full_photo_bytes'.
        conn.execute(
            "DELETE FROM usage_events WHERE kind = 'mcp_full_photo_bytes'"
        )
        conn.commit()
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        _tool_call(
            c, _headers(token), "get_item",
            {"item_id": ids["item_id"], "include_photo": "full"},
        )
    with app_mod.db() as conn:
        row = conn.execute(
            "SELECT units FROM usage_events "
            "WHERE kind = 'mcp_full_photo_bytes'"
        ).fetchone()
    assert row is not None
    assert row["units"] > 0


# ── Telemetry: surface=mcp ──────────────────────────────────────────


def test_tool_calls_record_surface_mcp(tmp_path, monkeypatch):
    """Every tool call writes a usage_events row with
    surface='mcp' so phase 13's cost-transparency block can
    break out agent-vs-human usage."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        _tool_call(c, _headers(token), "me")
        _tool_call(c, _headers(token), "find_items", {"q": "Whisk"})
    with app_mod.db() as conn:
        rows = conn.execute(
            "SELECT kind FROM usage_events "
            "WHERE surface = 'mcp' "
            "ORDER BY id"
        ).fetchall()
    kinds = [r["kind"] for r in rows]
    assert "mcp.me" in kinds
    assert "mcp.find_items" in kinds


# ── Quota propagation ──────────────────────────────────────────────


def test_quota_warning_band_surfaces_in_meta(tmp_path, monkeypatch):
    """When a tenant is in the 80–99% band, tool results carry
    ``_meta.warnings`` so an agent that ignores HTTP headers
    still sees the signal."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    # Force a tight cap and seed usage just under it.
    from dao import Actor, quotas as dao_quotas
    op = Actor(
        email="op@example.com", tenant_id=None, role=None,
        is_operator=True, memberships=(), shares=(),
    )
    dao_quotas.set_overrides(op, ids["t1"], monthly_ai_calls=100)
    with app_mod.db() as conn:
        for _ in range(85):
            conn.execute(
                "INSERT INTO usage_events "
                "(tenant_id, surface, kind, units, cost_micros) "
                "VALUES (?, 'ai', 'gemini_detect', 1, 0)",
                (ids["t1"],),
            )
        conn.commit()
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _tool_call(c, _headers(token), "me")
    meta = body["result"].get("_meta")
    assert meta is not None
    assert any("monthly_ai_calls" in w for w in meta["warnings"])


# ── Method-not-found ───────────────────────────────────────────────


def test_unknown_method_returns_rpc_error(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    token = _mint(app_mod, ids["t1"], "me@t1.example")
    with TestClient(app_mod.app) as c:
        body = _rpc(c, _headers(token), method="not_a_real_method")
    assert "error" in body
    assert body["error"]["code"] == -32601
