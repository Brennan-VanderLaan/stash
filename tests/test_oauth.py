"""Phase 19 — OAuth 2.1 authorization server.

Spec rev 2025-11-25 §"Authorization".  Stash plays both roles:
the resource server (``/mcp`` + ``/api/v1``) and the
authorization server (``/oauth/authorize`` + ``/oauth/token``).

Tests cover:

1. Discovery — ``/.well-known/oauth-protected-resource`` and
   ``/.well-known/oauth-authorization-server`` return the right
   shapes per RFC 9728 / RFC 8414.
2. Dynamic Client Registration — POST /oauth/register issues a
   client_id + redirect_uri storage.
3. Full authorization-code flow with PKCE: register → authorize
   GET → consent POST → /token exchange.
4. PKCE verification — wrong code_verifier → 400.
5. Code reuse — single-use enforced.
6. Refresh token rotation — old refresh fails after rotation.
7. Audience binding — token issued for /mcp must not authenticate
   against a different audience.
8. WWW-Authenticate header on /mcp 401 carries
   ``resource_metadata=`` so a discovery-aware client can find
   the AS without manual config.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import secrets
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── Fixtures ───────────────────────────────────────────────────────


def _bootstrap(tmp_path, monkeypatch):
    monkeypatch.setenv("STASH_DB", str(tmp_path / "stash.db"))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.setenv(
        "STASH_KEK",
        base64.b64encode(secrets.token_bytes(32)).decode(),
    )
    monkeypatch.setenv("STASH_PUBLIC_URL", "https://stash.example.com")
    monkeypatch.delenv("STASH_OPERATOR_EMAILS", raising=False)
    monkeypatch.setenv("STASH_REQUIRE_HTTPS_TOKENS", "false")
    for mod in ("app", "api", "mcp_server"):
        if mod in sys.modules:
            del sys.modules[mod]
    import app as app_module
    importlib.reload(app_module)
    import vault
    vault.clear_dek_cache()

    with app_module.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('Mine', 'pro')"
        )
        t1 = cur.lastrowid
        conn.execute(
            "INSERT INTO tenant_members "
            "(tenant_id, email, role, joined_at) "
            "VALUES (?, 'me@example.com', 'maintainer', "
            " CURRENT_TIMESTAMP)",
            (t1,),
        )
        conn.commit()
    return app_module, dict(t1=t1)


def _pkce_pair() -> tuple[str, str]:
    """Generate (verifier, S256-challenge) for an OAuth flow."""
    verifier = secrets.token_urlsafe(48)[:64]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


# ── Discovery ──────────────────────────────────────────────────────


def test_protected_resource_metadata_shape(tmp_path, monkeypatch):
    app_mod, _ = _bootstrap(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        r = c.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    body = r.json()
    assert body["resource"] == "https://stash.example.com/mcp"
    assert "https://stash.example.com" in body["authorization_servers"]
    assert "mcp" in body["scopes_supported"]
    assert "header" in body["bearer_methods_supported"]


def test_authorization_server_metadata_shape(tmp_path, monkeypatch):
    app_mod, _ = _bootstrap(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        r = c.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    body = r.json()
    assert body["issuer"] == "https://stash.example.com"
    assert body["authorization_endpoint"].endswith("/oauth/authorize")
    assert body["token_endpoint"].endswith("/oauth/token")
    assert body["registration_endpoint"].endswith("/oauth/register")
    # PKCE S256 mandatory per spec §"Authorization Code Protection".
    assert "S256" in body["code_challenge_methods_supported"]
    assert "authorization_code" in body["grant_types_supported"]
    assert "refresh_token" in body["grant_types_supported"]


# ── Dynamic Client Registration ────────────────────────────────────


def test_dcr_registers_public_client(tmp_path, monkeypatch):
    """A public client (no token_endpoint_auth_method or 'none')
    gets a client_id + no client_secret.  PKCE is the auth."""
    app_mod, _ = _bootstrap(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        r = c.post(
            "/oauth/register",
            json={
                "client_name": "Test MCP Client",
                "redirect_uris": [
                    "http://localhost:3000/callback",
                ],
                "token_endpoint_auth_method": "none",
            },
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["client_id"].startswith("client_")
    assert "client_secret" not in body
    assert body["token_endpoint_auth_method"] == "none"


def test_dcr_rejects_non_https_redirect(tmp_path, monkeypatch):
    """Spec §Communication Security: redirect URIs MUST be HTTPS
    or localhost.  An HTTP non-localhost redirect is refused."""
    app_mod, _ = _bootstrap(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        r = c.post(
            "/oauth/register",
            json={
                "client_name": "Bad",
                "redirect_uris": ["http://evil.example.com/callback"],
            },
        )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_client_metadata"


def test_dcr_disabled_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STASH_OAUTH_DCR_ENABLED", "false")
    app_mod, _ = _bootstrap(tmp_path, monkeypatch)
    monkeypatch.setenv("STASH_OAUTH_DCR_ENABLED", "false")
    with TestClient(app_mod.app) as c:
        r = c.post(
            "/oauth/register",
            json={
                "client_name": "Test",
                "redirect_uris": ["https://example.com/cb"],
            },
        )
    assert r.status_code == 403


# ── Authorization-code flow with PKCE ─────────────────────────────


def _register_client(client: TestClient,
                     redirect_uri: str = "https://app.test/cb") -> str:
    r = client.post(
        "/oauth/register",
        json={
            "client_name": "Test Client",
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def test_authorize_get_renders_consent_page(tmp_path, monkeypatch):
    """The user lands on /oauth/authorize via the client's
    redirect; oauth2-proxy has authenticated them already
    (X-Forwarded-Email).  Consent page shows the client name +
    membership picker."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    headers = {"X-Forwarded-Email": "me@example.com"}
    redirect_uri = "https://app.test/cb"
    with TestClient(app_mod.app, headers=headers) as c:
        client_id = _register_client(c, redirect_uri)
        verifier, challenge = _pkce_pair()
        r = c.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": "mcp",
                "state": "xyz",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": "https://stash.example.com/mcp",
            },
        )
    assert r.status_code == 200
    page = r.text
    assert "Test Client" in page
    assert "Mine" in page  # tenant name in dropdown
    assert "approve" in page.lower()


def test_authorize_get_rejects_unknown_client(tmp_path, monkeypatch):
    app_mod, _ = _bootstrap(tmp_path, monkeypatch)
    headers = {"X-Forwarded-Email": "me@example.com"}
    with TestClient(app_mod.app, headers=headers) as c:
        verifier, challenge = _pkce_pair()
        r = c.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "client_does_not_exist",
                "redirect_uri": "https://x/cb",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
    assert r.status_code == 400


def test_authorize_get_rejects_bad_redirect_uri(tmp_path, monkeypatch):
    """Open-redirect mitigation: a redirect_uri not in the
    client's registered list is refused even with otherwise-
    correct params."""
    app_mod, _ = _bootstrap(tmp_path, monkeypatch)
    headers = {"X-Forwarded-Email": "me@example.com"}
    with TestClient(app_mod.app, headers=headers) as c:
        client_id = _register_client(c, "https://app.test/cb")
        verifier, challenge = _pkce_pair()
        r = c.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "https://evil.example.com/cb",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
    assert r.status_code == 400


def _full_flow(c: TestClient, ids, *, with_refresh: bool = False) -> dict:
    """Execute the full register → authorize → consent → token
    flow.  Returns the /token response body."""
    redirect_uri = "https://app.test/cb"
    client_id = _register_client(c, redirect_uri)
    verifier, challenge = _pkce_pair()
    state = "ZYX"
    # Consent POST.  Skip the GET render — we know it works
    # (covered by test_authorize_get_renders_consent_page).
    r = c.post(
        "/oauth/authorize",
        data={
            "decision": "approve",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "mcp",
            "resource": "https://stash.example.com/mcp",
            "tenant_id": str(ids["t1"]),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    # Extract the code from the redirect (we don't actually
    # follow the bounce in tests).
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(loc).query)
    code = qs["code"][0]
    assert qs["state"][0] == state

    # Exchange code for token pair.
    r = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    body["_client_id"] = client_id
    return body


def test_full_flow_issues_access_and_refresh_token(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    headers = {"X-Forwarded-Email": "me@example.com"}
    with TestClient(app_mod.app, headers=headers) as c:
        body = _full_flow(c, ids)
    assert body["token_type"] == "Bearer"
    assert body["access_token"].startswith("stash_")
    assert body["refresh_token"].startswith("stashr_")
    assert body["expires_in"] == 3600
    assert body["scope"] == "mcp"


def test_access_token_authenticates_against_mcp(tmp_path, monkeypatch):
    """End-to-end smoke: the OAuth-issued access token should let
    a client successfully call /mcp's initialize handshake."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    headers = {"X-Forwarded-Email": "me@example.com"}
    with TestClient(app_mod.app, headers=headers) as c:
        body = _full_flow(c, ids)
        access = body["access_token"]
    with TestClient(app_mod.app) as c:
        r = c.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {access}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"


def test_pkce_wrong_verifier_fails(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    headers = {"X-Forwarded-Email": "me@example.com"}
    with TestClient(app_mod.app, headers=headers) as c:
        client_id = _register_client(c, "https://app.test/cb")
        _, challenge = _pkce_pair()
        # Mint a code with one challenge, then try to redeem it
        # with a *different* verifier (wrong PKCE).
        r = c.post(
            "/oauth/authorize",
            data={
                "decision": "approve",
                "client_id": client_id,
                "redirect_uri": "https://app.test/cb",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "mcp",
                "resource": "https://stash.example.com/mcp",
                "tenant_id": str(ids["t1"]),
            },
            follow_redirects=False,
        )
        from urllib.parse import urlparse, parse_qs
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        r = c.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "redirect_uri": "https://app.test/cb",
                "code_verifier": "totally-the-wrong-verifier",
            },
        )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_authorization_code_single_use(tmp_path, monkeypatch):
    """Replay attack: a code that's been redeemed must not work
    a second time.  Spec §4.1.3."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    headers = {"X-Forwarded-Email": "me@example.com"}
    with TestClient(app_mod.app, headers=headers) as c:
        redirect_uri = "https://app.test/cb"
        client_id = _register_client(c, redirect_uri)
        verifier, challenge = _pkce_pair()
        r = c.post(
            "/oauth/authorize",
            data={
                "decision": "approve", "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "mcp",
                "resource": "https://stash.example.com/mcp",
                "tenant_id": str(ids["t1"]),
            },
            follow_redirects=False,
        )
        from urllib.parse import urlparse, parse_qs
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        body_data = {
            "grant_type": "authorization_code",
            "code": code, "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
        r1 = c.post("/oauth/token", data=body_data)
        assert r1.status_code == 200
        # Replay.
        r2 = c.post("/oauth/token", data=body_data)
    assert r2.status_code == 400
    assert r2.json()["error"] == "invalid_grant"


def test_refresh_token_rotates(tmp_path, monkeypatch):
    """OAuth 2.1 §4.3.1: public clients MUST rotate refresh
    tokens.  Old refresh fails after a successful rotation."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    headers = {"X-Forwarded-Email": "me@example.com"}
    with TestClient(app_mod.app, headers=headers) as c:
        body = _full_flow(c, ids)
    refresh1 = body["refresh_token"]
    client_id = body["_client_id"]
    with TestClient(app_mod.app) as c:
        r = c.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh1,
                "client_id": client_id,
            },
        )
        assert r.status_code == 200, r.text
        new_body = r.json()
        # New refresh token issued.
        assert new_body["refresh_token"] != refresh1
        # Old refresh token now fails.
        r2 = c.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh1,
                "client_id": client_id,
            },
        )
    assert r2.status_code == 400
    assert r2.json()["error"] == "invalid_grant"


def test_audience_mismatch_rejects_token(tmp_path, monkeypatch):
    """A token issued for ``audience=https://stash.example.com/mcp``
    should not authenticate against a different resource even if
    the bearer is otherwise valid.  Stash only has /mcp as a
    distinct audience today, so we drive this by rewriting the
    audience column directly."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    headers = {"X-Forwarded-Email": "me@example.com"}
    with TestClient(app_mod.app, headers=headers) as c:
        body = _full_flow(c, ids)
        access = body["access_token"]
    # Mutate the audience to something unexpected.
    with app_mod.db() as conn:
        conn.execute(
            "UPDATE api_tokens SET audience = ? "
            "WHERE oauth_client_id IS NOT NULL",
            ("https://elsewhere.example.com/mcp",),
        )
        conn.commit()
    with TestClient(app_mod.app) as c:
        r = c.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {access}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
    assert r.status_code == 401


def test_mcp_401_carries_www_authenticate_resource_metadata(
    tmp_path, monkeypatch,
):
    """Spec §"Authorization Server Discovery" path #1: 401
    responses on /mcp MUST carry ``WWW-Authenticate: Bearer
    resource_metadata="..."`` so a discovery-aware client (like
    claude.ai) can find the AS without manual config."""
    app_mod, _ = _bootstrap(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        # Send a bogus bearer that fails authentication; the
        # 401 response should carry the discovery hint.
        r = c.post(
            "/mcp",
            headers={
                "Authorization": "Bearer stash_invalid_token_value_long_enough_to_be_shaped_right_42",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
    assert r.status_code == 401
    www = r.headers.get("WWW-Authenticate", "")
    assert "Bearer" in www
    assert "resource_metadata=" in www
    assert "/.well-known/oauth-protected-resource" in www
    assert "mcp" in www


def test_authorize_post_deny_redirects_with_error(tmp_path, monkeypatch):
    """User denying consent → redirect to the client's
    redirect_uri with ``error=access_denied`` per spec §4.1.2.1."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    headers = {"X-Forwarded-Email": "me@example.com"}
    with TestClient(app_mod.app, headers=headers) as c:
        client_id = _register_client(c, "https://app.test/cb")
        _, challenge = _pkce_pair()
        r = c.post(
            "/oauth/authorize",
            data={
                "decision": "deny",
                "client_id": client_id,
                "redirect_uri": "https://app.test/cb",
                "state": "ABC",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "mcp",
                "resource": "https://stash.example.com/mcp",
                "tenant_id": str(ids["t1"]),
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "error=access_denied" in loc
    assert "state=ABC" in loc
