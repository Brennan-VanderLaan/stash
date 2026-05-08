"""Phase 11 — bearer-auth /api/v1 surface.

Two halves:

1. DAO + bearer round-trip: mint a token via the DAO, hit
   /api/v1/me with it, confirm tenant_id resolves and last_used_at
   bumps.  Revocation cuts access immediately.
2. /api/v1 surface coverage: list/get boxes, list/get items,
   search, move.  Tenant scoping holds (a token from T1 can't see
   T2's boxes).
"""

from __future__ import annotations

import base64
import importlib
import secrets
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _bootstrap_two_tenants(tmp_path, monkeypatch):
    """Two tenants with one maintainer each, plus a box + item in
    each tenant so a tenant-scope leak shows up as a wrong-tenant
    row appearing in a list."""
    monkeypatch.setenv("STASH_DB", str(tmp_path / "stash.db"))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.setenv(
        "STASH_KEK",
        base64.b64encode(secrets.token_bytes(32)).decode(),
    )
    monkeypatch.delenv("STASH_OPERATOR_EMAILS", raising=False)
    if "app" in sys.modules:
        del sys.modules["app"]
    if "api" in sys.modules:
        del sys.modules["api"]
    import app as app_module
    importlib.reload(app_module)
    import vault
    vault.clear_dek_cache()

    with app_module.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('T1', 'pro')"
        )
        t1 = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('T2', 'pro')"
        )
        t2 = cur.lastrowid
        for tid, owner in ((t1, "owner@t1.example"),
                           (t2, "owner@t2.example")):
            conn.execute(
                "INSERT INTO tenant_members "
                "(tenant_id, email, role, joined_at) "
                "VALUES (?, ?, 'maintainer', CURRENT_TIMESTAMP)",
                (tid, owner),
            )
        conn.execute(
            "INSERT INTO boxes (id, name, location, notes, tenant_id) "
            "VALUES (1, 'T1 Kitchen', 'A', '', ?)",
            (t1,),
        )
        conn.execute(
            "INSERT INTO items (id, box_id, name, notes, tenant_id) "
            "VALUES (10, 1, 'Whisk', 'beat eggs', ?)",
            (t1,),
        )
        conn.execute(
            "INSERT INTO boxes (id, name, location, notes, tenant_id) "
            "VALUES (2, 'T2 Garage', 'B', '', ?)",
            (t2,),
        )
        conn.execute(
            "INSERT INTO items (id, box_id, name, notes, tenant_id) "
            "VALUES (20, 2, 'Wrench', 'metric', ?)",
            (t2,),
        )
        conn.commit()
    return app_module, dict(t1=t1, t2=t2)


def _mint_token(app_mod, tenant_id, owner_email,
                *, role="maintainer") -> str:
    from dao import Actor, api_tokens as dao_api_tokens
    actor = Actor(
        email=owner_email, tenant_id=tenant_id, role="maintainer",
        is_operator=False, memberships=((tenant_id, "maintainer"),),
        shares=(),
    )
    return dao_api_tokens.create(actor, name="test-token",
                                 role=role)["plaintext"]


# ── DAO surface ─────────────────────────────────────────────────────


def test_dao_mint_authenticate_revoke_roundtrip(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    token = _mint_token(app_mod, ids["t1"], "owner@t1.example")
    assert token.startswith("stash_")
    from dao import api_tokens as dao_api_tokens
    row = dao_api_tokens.authenticate(token)
    assert row is not None
    assert row["tenant_id"] == ids["t1"]
    assert row["role"] == "maintainer"

    # Plaintext is never stored; the DB only has the hash.
    with app_mod.db() as conn:
        plaintext_in_db = conn.execute(
            "SELECT 1 FROM api_tokens WHERE token_hash = ?",
            (token,),  # passing the plaintext as-is — should miss.
        ).fetchone()
    assert plaintext_in_db is None

    # Revoke + re-authenticate fails.
    from dao import Actor
    owner = Actor(
        email="owner@t1.example", tenant_id=ids["t1"], role="maintainer",
        is_operator=False, memberships=((ids["t1"], "maintainer"),),
        shares=(),
    )
    dao_api_tokens.revoke(owner, row["id"])
    assert dao_api_tokens.authenticate(token) is None


def test_dao_authenticate_rejects_garbage(tmp_path, monkeypatch):
    """Unknown / malformed tokens return None, not an exception."""
    app_mod, _ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    from dao import api_tokens as dao_api_tokens
    assert dao_api_tokens.authenticate("") is None
    assert dao_api_tokens.authenticate("plaintext-no-prefix") is None
    assert dao_api_tokens.authenticate("stash_unknownXYZ") is None


def test_dao_authenticate_bumps_last_used_at(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    token = _mint_token(app_mod, ids["t1"], "owner@t1.example")
    from dao import api_tokens as dao_api_tokens
    dao_api_tokens.authenticate(token)
    with app_mod.db() as conn:
        row = conn.execute(
            "SELECT last_used_at FROM api_tokens WHERE name = 'test-token'"
        ).fetchone()
    assert row["last_used_at"] is not None


# ── Bearer middleware ──────────────────────────────────────────────


def test_no_bearer_returns_401(tmp_path, monkeypatch):
    """A request to /api/v1 with no Authorization header and no
    X-Forwarded-Email is unauthenticated — the global 403 (auth
    deny) wall lands.  Bearer tokens are the *only* path through
    /api/v1 in v1; oauth2-proxy headers stay browser-side."""
    app_mod, _ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        r = c.get("/api/v1/me")
    assert r.status_code in (401, 403)


def test_invalid_bearer_returns_401(tmp_path, monkeypatch):
    app_mod, _ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        r = c.get(
            "/api/v1/me",
            headers={"Authorization": "Bearer stash_does_not_exist"},
        )
    assert r.status_code == 401


def test_bearer_resolves_actor_and_me_works(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    token = _mint_token(app_mod, ids["t1"], "owner@t1.example")
    with TestClient(app_mod.app) as c:
        r = c.get("/api/v1/me",
                  headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == ids["t1"]
    assert body["role"] == "maintainer"
    assert body["email"].startswith("api_token:")


# ── Surface scoping ────────────────────────────────────────────────


def test_boxes_endpoint_scopes_to_token_tenant(tmp_path, monkeypatch):
    """A T1 token must not surface T2's box in /api/v1/boxes."""
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    token = _mint_token(app_mod, ids["t1"], "owner@t1.example")
    with TestClient(app_mod.app) as c:
        r = c.get("/api/v1/boxes",
                  headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    names = [b["name"] for b in r.json()["boxes"]]
    assert "T1 Kitchen" in names
    assert "T2 Garage" not in names


def test_items_search_scopes_to_token_tenant(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    token = _mint_token(app_mod, ids["t1"], "owner@t1.example")
    with TestClient(app_mod.app) as c:
        r = c.get("/api/v1/items?q=Wrench",
                  headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    # T2's "Wrench" is invisible to a T1 token even on a free-text
    # search that matches it.
    assert r.json()["items"] == []


def test_get_other_tenant_box_404s(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    token = _mint_token(app_mod, ids["t1"], "owner@t1.example")
    # Box id 2 belongs to T2; the T1 token must see 404, not the row.
    with TestClient(app_mod.app) as c:
        r = c.get("/api/v1/boxes/2",
                  headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404


def test_move_item_via_api(tmp_path, monkeypatch):
    """The single write-side endpoint shipped in v1.  Move T1's
    Whisk into a fresh T1 box; the response carries old + new
    box_id and the DB reflects the move."""
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    # Add a second T1 box to move into.
    with app_mod.db() as conn:
        conn.execute(
            "INSERT INTO boxes (id, name, location, notes, tenant_id) "
            "VALUES (3, 'T1 Pantry', 'A', '', ?)",
            (ids["t1"],),
        )
        conn.commit()
    token = _mint_token(app_mod, ids["t1"], "owner@t1.example")
    with TestClient(app_mod.app) as c:
        r = c.post(
            "/api/v1/items/10/move",
            json={"box_id": 3},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["item_id"] == 10
    assert body["old_box_id"] == 1
    assert body["new_box_id"] == 3


def test_move_item_to_other_tenant_box_400(tmp_path, monkeypatch):
    """A T1 token trying to move a T1 item into T2's box is a 400
    on target — not a 200 with cross-tenant write, not a 404 that
    leaks "T2 box exists"."""
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    token = _mint_token(app_mod, ids["t1"], "owner@t1.example")
    with TestClient(app_mod.app) as c:
        r = c.post(
            "/api/v1/items/10/move",
            json={"box_id": 2},  # T2's box
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 400


# ── /usage UI surface ──────────────────────────────────────────────


def test_usage_mints_token_and_reveals_plaintext_once(client):
    """The mint POST round-trips ``?api_token_plaintext=…`` so the
    page can render the one-time copy box.  After the redirect,
    the plaintext is gone — refreshing /usage shouldn't reveal it
    again."""
    r = client.post(
        "/usage/api-tokens",
        data={"name": "MCP server", "role": "maintainer"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "api_token_plaintext=" in loc
    # Plain ``GET /usage`` (no query string) shouldn't render any
    # token bytes — the table only shows names + last_used_at.
    page = client.get("/usage").text
    assert "stash_" not in page  # no plaintext leaks once redirect is gone
    assert "MCP server" in page  # but the listing has the name


def test_usage_revoke_cuts_token_immediately(client):
    """After revoke, an in-flight token returns 401 on the next
    /api/v1 request."""
    from dao import Actor, api_tokens as dao_api_tokens
    owner = Actor(
        email=client.test_email, tenant_id=client.test_tenant_id,
        role="maintainer", is_operator=False,
        memberships=((client.test_tenant_id, "maintainer"),),
        shares=(),
    )
    minted = dao_api_tokens.create(owner, name="ephemeral")
    token = minted["plaintext"]
    # Token works.
    r = client.get("/api/v1/me",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    # Revoke through the route.
    rev = client.post(f"/usage/api-tokens/{minted['id']}/revoke",
                      follow_redirects=False)
    assert rev.status_code == 303
    # Next request fails.
    r2 = client.get("/api/v1/me",
                    headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 401
