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


def _bootstrap_two_tenants(tmp_path, monkeypatch, *,
                           keep_operators: bool = False):
    """Two tenants with one maintainer each, plus a box + item in
    each tenant so a tenant-scope leak shows up as a wrong-tenant
    row appearing in a list.

    ``keep_operators`` lets a caller preserve a STASH_OPERATOR_EMAILS
    they set BEFORE invoking the bootstrap (the env var is read at
    module import time into a frozenset, so changes after reload
    don't take effect)."""
    monkeypatch.setenv("STASH_DB", str(tmp_path / "stash.db"))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.setenv(
        "STASH_KEK",
        base64.b64encode(secrets.token_bytes(32)).decode(),
    )
    if not keep_operators:
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
    """The mint POST renders the usage page inline with the
    plaintext block — *not* a redirect — so the plaintext never
    lands in a URL where the leak scanner would (correctly) catch
    and auto-revoke it.  The plain GET /usage that follows shows
    no token bytes."""
    r = client.post(
        "/usage/api-tokens",
        data={"name": "MCP server", "role": "maintainer"},
        follow_redirects=False,
    )
    # Direct render, not a redirect.
    assert r.status_code == 200, r.text
    body = r.text
    # The plaintext appears exactly once, in the response body.
    import re
    matches = re.findall(r"stash_[A-Za-z0-9_\-]{40,50}", body)
    assert len(matches) >= 1, "plaintext token missing from response body"

    # Plain GET /usage (no query string) shouldn't render any
    # token bytes — the table only shows names + last_used_at.
    page = client.get("/usage").text
    assert "stash_" not in page  # no plaintext leaks across requests
    assert "MCP server" in page  # but the listing has the name


def test_freshly_minted_token_actually_works(client):
    """End-to-end regression for the bug where the post-mint
    redirect put the plaintext in the URL, the leak scanner
    spotted it on the very next request, and auto-revoked the
    freshly-minted token before the user could use it.

    The fix is "render inline, never redirect with the plaintext"
    — this test catches a re-introduction by extracting the
    plaintext from the rendered page and confirming it actually
    authenticates against /api/v1/me."""
    r = client.post(
        "/usage/api-tokens",
        data={"name": "regression", "role": "maintainer"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    import re
    m = re.search(r"stash_[A-Za-z0-9_\-]{40,50}", r.text)
    assert m, "plaintext token missing from response body"
    plaintext = m.group(0)

    r2 = client.get(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    # If the leak scanner ate the token during the mint flow,
    # this request lands on a revoked row and 401s.  A clean
    # mint produces a working token.
    assert r2.status_code == 200, (
        f"freshly-minted token failed auth (status {r2.status_code}, "
        f"body {r2.text!r}) — the post-mint flow re-introduced a "
        "URL round-trip that the leak scanner caught."
    )


def test_bearer_over_http_auto_revokes(tmp_path, monkeypatch):
    """Spec § "API tokens · token-leak guards": a bearer that
    travels over plaintext HTTP must be auto-revoked with reason
    ``seen_over_http`` on the first request that arrives without
    X-Forwarded-Proto: https."""
    monkeypatch.setenv("STASH_REQUIRE_HTTPS_TOKENS", "true")
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    token = _mint_token(app_mod, ids["t1"], "owner@t1.example")
    with TestClient(app_mod.app) as c:
        r = c.get("/api/v1/me",
                  headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    # Token row should be revoked with the right reason.
    with app_mod.db() as conn:
        row = conn.execute(
            "SELECT revoked_at, revoked_reason FROM api_tokens "
            "WHERE name = 'test-token'"
        ).fetchone()
    assert row["revoked_at"] is not None
    assert row["revoked_reason"] == "seen_over_http"


def test_bearer_with_x_forwarded_proto_https_works(tmp_path, monkeypatch):
    """When Caddy proxies a real HTTPS request, ``X-Forwarded-Proto:
    https`` is set and the guard lets the bearer through."""
    monkeypatch.setenv("STASH_REQUIRE_HTTPS_TOKENS", "true")
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    token = _mint_token(app_mod, ids["t1"], "owner@t1.example")
    with TestClient(app_mod.app) as c:
        r = c.get(
            "/api/v1/me",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Forwarded-Proto": "https",
            },
        )
    assert r.status_code == 200


def test_token_in_url_query_auto_revokes(tmp_path, monkeypatch):
    """A stash_-shaped token in the URL query string is treated as
    a leak and auto-revoked, even if the same token isn't in the
    Authorization header.  Defensive scan covers the
    'pasted-curl-with-?token=' mistake."""
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    token = _mint_token(app_mod, ids["t1"], "owner@t1.example")
    with TestClient(app_mod.app) as c:
        r = c.get(f"/?token={token}")
    assert r.status_code == 401
    assert "revoked" in r.text.lower()
    with app_mod.db() as conn:
        row = conn.execute(
            "SELECT revoked_reason FROM api_tokens "
            "WHERE name = 'test-token'"
        ).fetchone()
    assert row["revoked_reason"] == "leaked_in_url"


def test_token_in_other_header_auto_revokes(tmp_path, monkeypatch):
    """A stash_-shaped token in a non-Authorization header (e.g. a
    custom X-Token header from a misconfigured client) is also
    treated as a leak."""
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    token = _mint_token(app_mod, ids["t1"], "owner@t1.example")
    with TestClient(app_mod.app) as c:
        r = c.get("/", headers={"X-Custom-Token": token})
    assert r.status_code == 401
    with app_mod.db() as conn:
        row = conn.execute(
            "SELECT revoked_reason FROM api_tokens "
            "WHERE name = 'test-token'"
        ).fetchone()
    assert row["revoked_reason"] == "leaked_in_header"


def test_operator_can_revoke_any_tenants_token(tmp_path, monkeypatch):
    """/admin token panel lets an operator kill any tenant's
    token.  Reason on the row is 'operator_revoke' so the
    owning tenant can see the kill came from above."""
    # STASH_OPERATOR_EMAILS must be set *before* the bootstrap reloads
    # the app module — it's read at module import time into a frozenset.
    monkeypatch.setenv("STASH_OPERATOR_EMAILS", "op@example.com")
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch,
                                          keep_operators=True)
    token = _mint_token(app_mod, ids["t1"], "owner@t1.example")
    with app_mod.db() as conn:
        token_id = conn.execute(
            "SELECT id FROM api_tokens WHERE name = 'test-token'"
        ).fetchone()["id"]
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": "op@example.com"}) as c:
        r = c.post(f"/admin/api-tokens/{token_id}/revoke",
                   follow_redirects=False)
    assert r.status_code == 303
    # Now the bearer fails.
    with TestClient(app_mod.app) as c:
        r = c.get("/api/v1/me",
                  headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    with app_mod.db() as conn:
        row = conn.execute(
            "SELECT revoked_reason FROM api_tokens WHERE id = ?",
            (token_id,),
        ).fetchone()
    assert row["revoked_reason"] == "operator_revoke"


def test_operator_revoke_client_tokens_kills_active_only(
    tmp_path, monkeypatch,
):
    """A bulk revoke for one OAuth client + tenant flips every
    active token's revoked_at without touching tokens from other
    clients or other tenants.  Tokens already revoked stay revoked
    and don't double-count."""
    monkeypatch.setenv("STASH_OPERATOR_EMAILS", "op@example.com")
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch,
                                          keep_operators=True)
    # Seed an OAuth client + 3 access tokens for t1, plus 1 for t2,
    # plus an already-revoked one (should be a no-op).
    with app_mod.db() as conn:
        conn.execute(
            "INSERT INTO oauth_clients "
            "(client_id, name, redirect_uris) "
            "VALUES ('claude-ai-mcp', 'claude.ai MCP', "
            "        '[\"https://claude.ai/cb\"]')"
        )
        for i in range(3):
            conn.execute(
                "INSERT INTO api_tokens "
                "(tenant_id, token_hash, name, role, "
                " created_by_email, oauth_client_id) "
                "VALUES (?, ?, ?, 'maintainer', "
                "        'owner@t1.example', 'claude-ai-mcp')",
                (ids["t1"], f"hash-t1-{i}", f"mcp-token-{i}"),
            )
        # Different tenant, same OAuth client — must NOT be touched.
        conn.execute(
            "INSERT INTO api_tokens "
            "(tenant_id, token_hash, name, role, "
            " created_by_email, oauth_client_id) "
            "VALUES (?, 'hash-t2', 'mcp-t2', 'maintainer', "
            "        'owner@t2.example', 'claude-ai-mcp')",
            (ids["t2"],),
        )
        # Already-revoked t1 token (same client) — should stay revoked.
        conn.execute(
            "INSERT INTO api_tokens "
            "(tenant_id, token_hash, name, role, "
            " created_by_email, oauth_client_id, revoked_at) "
            "VALUES (?, 'hash-t1-old', 'mcp-t1-old', 'maintainer', "
            "        'owner@t1.example', 'claude-ai-mcp', "
            "        '2020-01-01T00:00:00')",
            (ids["t1"],),
        )
        conn.commit()
    with TestClient(
        app_mod.app, headers={"X-Forwarded-Email": "op@example.com"},
    ) as c:
        r = c.post(
            "/admin/oauth-clients/revoke-tokens",
            data={
                "oauth_client_id": "claude-ai-mcp",
                "tenant_id": str(ids["t1"]),
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    with app_mod.db() as conn:
        # 3 active t1 rows flipped to revoked with the dedicated reason.
        rows = conn.execute(
            "SELECT name, revoked_reason FROM api_tokens "
            "WHERE oauth_client_id = 'claude-ai-mcp' AND tenant_id = ? "
            "  AND revoked_reason = 'operator_revoke_client'",
            (ids["t1"],),
        ).fetchall()
        assert len(rows) == 3
        # The previously-revoked row keeps its original (NULL) reason
        # — bulk revoke is "active only" and won't re-stamp.
        old = conn.execute(
            "SELECT revoked_reason FROM api_tokens "
            "WHERE name = 'mcp-t1-old'"
        ).fetchone()
        assert old["revoked_reason"] != "operator_revoke_client"
        # t2 token must still be active.
        t2_row = conn.execute(
            "SELECT revoked_at FROM api_tokens WHERE name = 'mcp-t2'"
        ).fetchone()
        assert t2_row["revoked_at"] is None


def test_operator_revoke_client_tokens_non_operator_404(
    tmp_path, monkeypatch,
):
    """A non-operator hitting the bulk endpoint gets a 404 — same
    opacity convention as the rest of /admin (404 not 403 so the
    operator surface stays unenumerable)."""
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    # Authenticated as a regular tenant member, NOT an operator.
    with TestClient(
        app_mod.app, headers={"X-Forwarded-Email": "owner@t1.example"},
    ) as c:
        r = c.post(
            "/admin/oauth-clients/revoke-tokens",
            data={"oauth_client_id": "x", "tenant_id": "1"},
            follow_redirects=False,
        )
    assert r.status_code == 404


def test_admin_groups_oauth_tokens_by_client(tmp_path, monkeypatch):
    """The /admin token panel renders an "Grouped by OAuth client"
    summary card per (tenant, oauth_client) so an operator can see
    a flood of access tokens as one origin instead of N rows."""
    monkeypatch.setenv("STASH_OPERATOR_EMAILS", "op@example.com")
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch,
                                          keep_operators=True)
    with app_mod.db() as conn:
        conn.execute(
            "INSERT INTO oauth_clients "
            "(client_id, name, redirect_uris) "
            "VALUES ('claude-ai-mcp', 'claude.ai MCP', "
            "        '[\"https://claude.ai/cb\"]')"
        )
        for i in range(4):
            conn.execute(
                "INSERT INTO api_tokens "
                "(tenant_id, token_hash, name, role, "
                " created_by_email, oauth_client_id) "
                "VALUES (?, ?, ?, 'maintainer', "
                "        'owner@t1.example', 'claude-ai-mcp')",
                (ids["t1"], f"hash-{i}", f"tok-{i}"),
            )
        conn.commit()
    with TestClient(
        app_mod.app, headers={"X-Forwarded-Email": "op@example.com"},
    ) as c:
        page = c.get("/admin").text
    assert "Grouped by OAuth client" in page
    assert "claude.ai MCP" in page
    assert "4 active" in page
    assert "Revoke all active" in page


def test_operator_can_suspend_and_resume(tmp_path, monkeypatch):
    """Suspend pauses a token; resume reactivates it.  Auth fails
    while suspended, succeeds once resumed."""
    # STASH_OPERATOR_EMAILS must be set *before* the bootstrap reloads
    # the app module — it's read at module import time into a frozenset.
    monkeypatch.setenv("STASH_OPERATOR_EMAILS", "op@example.com")
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch,
                                          keep_operators=True)
    token = _mint_token(app_mod, ids["t1"], "owner@t1.example")
    with app_mod.db() as conn:
        token_id = conn.execute(
            "SELECT id FROM api_tokens WHERE name = 'test-token'"
        ).fetchone()["id"]
    op_h = {"X-Forwarded-Email": "op@example.com"}
    with TestClient(app_mod.app, headers=op_h) as c:
        # Suspend.
        c.post(f"/admin/api-tokens/{token_id}/suspend",
               follow_redirects=False)
    # Bearer fails while suspended.
    with TestClient(app_mod.app) as c:
        r = c.get("/api/v1/me",
                  headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401
    # Resume.
    with TestClient(app_mod.app, headers=op_h) as c:
        c.post(f"/admin/api-tokens/{token_id}/resume",
               follow_redirects=False)
    # Bearer works again.
    with TestClient(app_mod.app) as c:
        r = c.get("/api/v1/me",
                  headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200


def test_token_name_too_long_rejected(client):
    """100-char cap on token names — reject longer names with 400
    so a runaway-input client can't bloat the DB."""
    long_name = "x" * 200
    r = client.post(
        "/usage/api-tokens",
        data={"name": long_name, "role": "maintainer"},
        follow_redirects=False,
    )
    assert r.status_code == 400


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


# ── CI redeploy webhook (/api/v1/admin/redeploy) ───────────────────


def _mint_operator_token(app_mod, tenant_id, op_email):
    """Mint a bearer token whose creator is in STASH_OPERATOR_EMAILS.
    The actor-middleware reads the creator email back at request time
    and sets is_operator=True, so the resulting token authenticates
    as an operator without needing to plumb is_operator through the
    DAO's mint path."""
    from dao import Actor, api_tokens as dao_api_tokens
    op_actor = Actor(
        email=op_email, tenant_id=tenant_id, role="maintainer",
        is_operator=True, memberships=((tenant_id, "maintainer"),),
        shares=(),
    )
    return dao_api_tokens.create(op_actor, name="ci-redeploy")["plaintext"]


def test_redeploy_unauthenticated_rejected(tmp_path, monkeypatch):
    """No bearer → 401 from the global wall.  This endpoint is in
    the bearer-only /api/v1 surface."""
    app_mod, _ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        r = c.post("/api/v1/admin/redeploy")
    assert r.status_code in (401, 403)


def test_redeploy_non_operator_bearer_is_forbidden(tmp_path, monkeypatch):
    """A regular tenant maintainer bearer is rejected with 403.
    Opacity (404) is HTML-only; the API surface uses 403 uniformly."""
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    token = _mint_token(app_mod, ids["t1"], "owner@t1.example")
    with TestClient(app_mod.app) as c:
        r = c.post("/api/v1/admin/redeploy",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_redeploy_503_when_watchtower_not_configured(tmp_path, monkeypatch):
    """If WATCHTOWER_URL is empty on this deploy, the route 503s
    rather than silently no-opping.  GHA hitting a misconfigured
    box should fail loudly so the operator can fix the env."""
    monkeypatch.setenv("STASH_OPERATOR_EMAILS", "op@example.com")
    monkeypatch.delenv("WATCHTOWER_URL", raising=False)
    app_mod, ids = _bootstrap_two_tenants(
        tmp_path, monkeypatch, keep_operators=True,
    )
    # Add op as a tenant member of T1 so token mint succeeds.
    with app_mod.db() as conn:
        conn.execute(
            "INSERT INTO tenant_members "
            "(tenant_id, email, role, joined_at) "
            "VALUES (?, 'op@example.com', 'maintainer', CURRENT_TIMESTAMP)",
            (ids["t1"],),
        )
        conn.commit()
    token = _mint_operator_token(app_mod, ids["t1"], "op@example.com")
    with TestClient(app_mod.app) as c:
        r = c.post("/api/v1/admin/redeploy",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 503
    assert "WATCHTOWER_URL" in r.json()["detail"]


def test_redeploy_happy_path_triggers_watchtower(tmp_path, monkeypatch):
    """Operator bearer + WATCHTOWER_URL set → 200, and the
    watchtower-trigger background task runs.  We stub the
    trigger function so the test doesn't make real network calls."""
    monkeypatch.setenv("STASH_OPERATOR_EMAILS", "op@example.com")
    monkeypatch.setenv("WATCHTOWER_URL", "http://watchtower:8080")
    monkeypatch.setenv("WATCHTOWER_TOKEN", "test-watchtower-token")
    app_mod, ids = _bootstrap_two_tenants(
        tmp_path, monkeypatch, keep_operators=True,
    )
    with app_mod.db() as conn:
        conn.execute(
            "INSERT INTO tenant_members "
            "(tenant_id, email, role, joined_at) "
            "VALUES (?, 'op@example.com', 'maintainer', CURRENT_TIMESTAMP)",
            (ids["t1"],),
        )
        conn.commit()
    token = _mint_operator_token(app_mod, ids["t1"], "op@example.com")

    calls: list[bool] = []
    def fake_trigger():
        calls.append(True)
    monkeypatch.setattr(app_mod, "_trigger_watchtower_update", fake_trigger)

    with TestClient(app_mod.app) as c:
        r = c.post("/api/v1/admin/redeploy",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "triggered": "watchtower"}
    # BackgroundTasks fire after the response is sent — by the time
    # TestClient returns control, the task has run.
    assert calls == [True], "watchtower trigger should have been called once"
