"""Comprehensive auth-coverage suite.

User data safety + tenant isolation are paramount.  This file
drives every protected route under five distinct identity shapes
and asserts the access matrix matches expectations.

Identity shapes covered:

* **Unauthenticated** — no ``X-Forwarded-Email``, no bearer token.
  Must 401/403 for every protected route; only ``/healthz`` passes.
* **Wrong tenant** — actor of T2 hitting T1's resources.  Must 404
  (never 403, since 403 leaks "this row exists in another tenant").
* **Readonly** — a readonly-role member can hit GET routes but
  every mutation route is 403.
* **Non-operator** — a tenant maintainer who is *not* in
  ``STASH_OPERATOR_EMAILS`` hitting ``/admin``.  Must 404 (opacity
  rule: the surface stays invisible).
* **Revoked bearer** — an API token whose ``revoked_at`` is set
  must not authenticate.
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


# ── Helpers ─────────────────────────────────────────────────────────


def _bootstrap(tmp_path, monkeypatch):
    """Two tenants, three actors per side: T1 maintainer + T1
    readonly + T2 maintainer.  T1 has a box + item so the
    cross-tenant tests have something to attempt access against."""
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
        cur = conn.execute("INSERT INTO tenants (name, plan) VALUES ('T1', 'pro')")
        t1 = cur.lastrowid
        cur = conn.execute("INSERT INTO tenants (name, plan) VALUES ('T2', 'pro')")
        t2 = cur.lastrowid
        # T1 maintainer + T1 readonly + T2 maintainer.
        for tid, email, role in (
            (t1, "main@t1.example", "maintainer"),
            (t1, "ro@t1.example", "readonly"),
            (t2, "main@t2.example", "maintainer"),
        ):
            conn.execute(
                "INSERT INTO tenant_members "
                "(tenant_id, email, role, joined_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (tid, email, role),
            )
        # T1 owns box id 1 with item id 10.
        conn.execute(
            "INSERT INTO boxes (id, name, location, notes, tenant_id) "
            "VALUES (1, 'T1 Kitchen', 'A', '', ?)",
            (t1,),
        )
        conn.execute(
            "INSERT INTO items (id, box_id, name, notes, tenant_id) "
            "VALUES (10, 1, 'Whisk', '', ?)",
            (t1,),
        )
        # T1 location + room + floor for cross-tenant checks.
        conn.execute(
            "INSERT INTO locations (id, name, tenant_id) "
            "VALUES (1, 'T1 Townhouse', ?)",
            (t1,),
        )
        conn.execute(
            "INSERT INTO floors (id, location_id, name, tenant_id) "
            "VALUES (1, 1, 'Ground', ?)",
            (t1,),
        )
        conn.execute(
            "INSERT INTO rooms (id, location_id, floor_id, name, tenant_id) "
            "VALUES (1, 1, 1, 'Main', ?)",
            (t1,),
        )
        conn.commit()
    return app_module, dict(t1=t1, t2=t2)


# ── Unauthenticated wall ────────────────────────────────────────────


# Routes that should *all* refuse an unauthenticated request.  The
# list is curated rather than auto-derived so we can include the path
# params + verify the actual auth wall fires before the route logic.
# Format: (method, path, body-or-None).
_PROTECTED_ROUTES: list[tuple[str, str, dict | None]] = [
    # ``/`` is the public marketing landing — NOT in this list.
    # ``/home`` is the authenticated dashboard; pin THAT instead.
    ("GET", "/home", None),
    ("POST", "/boxes", {"name": "x"}),
    ("GET", "/boxes/1", None),
    ("POST", "/boxes/1/edit", {"name": "x"}),
    ("POST", "/boxes/1/delete", {"confirm": "x"}),
    ("POST", "/items/10/move", {"box_id": "1"}),
    ("POST", "/items/10/delete", None),
    ("GET", "/locations", None),
    ("POST", "/locations", {"name": "x"}),
    ("GET", "/locations/1", None),
    ("POST", "/locations/1/delete", {"confirm": "T1 Townhouse"}),
    ("GET", "/queue", None),
    ("GET", "/ingest", None),
    ("GET", "/search", None),
    ("GET", "/tags", None),
    ("GET", "/labels", None),
    ("GET", "/usage", None),
    ("GET", "/usage/backup", None),
    ("POST", "/usage/api-tokens", {"name": "x"}),
    ("POST", "/usage/invites", {"email": "x@example.com"}),
    ("GET", "/maintenance", None),
    ("POST", "/maintenance/cleanup", None),
    ("GET", "/maintenance/export", None),
    ("GET", "/admin", None),
    ("POST", "/admin/tenants", {"name": "x", "invitee_email": "x@example.com"}),
    ("GET", "/api/v1/me", None),
    ("GET", "/api/v1/boxes", None),
    ("GET", "/api/v1/items", None),
    ("POST", "/api/v1/items/10/move", {"box_id": 1}),
    ("GET", "/shared", None),
    ("GET", "/shared/box/1", None),
    ("GET", "/uploads/anything.jpg", None),
    ("GET", "/thumbs/anything.jpg", None),
]


@pytest.mark.parametrize("method,path,body", _PROTECTED_ROUTES,
                         ids=lambda v: v if isinstance(v, str) else "")
def test_unauthenticated_request_is_rejected(
    tmp_path, monkeypatch, method, path, body,
):
    """Every protected route refuses an unauthenticated request.
    Acceptable shapes: 401 (the bearer-rejection path) or 403 (the
    auth-wall response).  Anything 200/3xx is a security bug."""
    app_mod, _ids = _bootstrap(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        if method == "GET":
            r = c.get(path, follow_redirects=False)
        elif method == "POST":
            if path.startswith("/api/v1/"):
                r = c.post(path, json=body, follow_redirects=False)
            else:
                r = c.post(path, data=body, follow_redirects=False)
        else:
            raise AssertionError(method)
    assert r.status_code in (401, 403), (
        f"{method} {path} returned {r.status_code} unauthenticated — "
        "expected 401/403"
    )


def test_auth_bypass_paths_pinned(tmp_path, monkeypatch):
    """The auth-bypass set is intentionally tiny — anything new
    here needs a security justification.  This test pins the set
    so a future addition shows up as a code-review red flag."""
    app_mod, _ = _bootstrap(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        # /healthz: liveness probe, no tenant data.
        r = c.get("/healthz")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # /.well-known/oauth-* — public discovery surfaces (RFC
        # 8414 + RFC 9728) so MCP clients can auto-discover.
        r = c.get("/.well-known/oauth-protected-resource")
        assert r.status_code == 200
        assert r.json()["resource"].endswith("/mcp")
        r = c.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        # Static files bypass the auth wall: the public /about
        # pages need to load /static/style.css to render styled
        # for unauthenticated viewers (Stripe's KYC crawler being
        # the load-bearing example).  Static carries no tenant
        # data — only stash's own CSS/JS/vendor bundles — so the
        # bypass is safe.  User-uploaded photos live at /uploads/
        # which stays gated.
        r = c.get("/static/style.css", follow_redirects=False)
        assert r.status_code == 200
    # Pin the exact bypass set — split into exact + prefix forms
    # to support RFC 9728 path-suffixed protected-resource
    # metadata (``/.well-known/oauth-protected-resource/mcp``).
    # ``/`` joined the list when the public marketing landing was
    # split off from the authenticated dashboard (now at /home);
    # unauthenticated visitors need a page to land on.
    assert app_mod._AUTH_BYPASS_EXACT == frozenset((
        "/healthz",
        "/.well-known/oauth-authorization-server",
        "/oauth/token",
        "/oauth/register",
        "/",
        "/robots.txt",
        "/__stash_robots_txt",
    ))
    # /about/ added: Stripe + similar KYC partners require the
    # business name, refund policy, sub-processor list, etc. to be
    # publicly reachable without a Google sign-in.  Both the
    # trailing-slash + bare forms are listed so /about itself and
    # every /about/* page bypass.  /static/ added so the public
    # pages can fetch CSS/JS without bouncing through Google
    # login — these are stash's own assets, no tenant data.
    assert app_mod._AUTH_BYPASS_PREFIXES == (
        "/.well-known/oauth-protected-resource",
        "/about/",
        "/about",
        "/static/",
    )


# ── Cross-tenant isolation ──────────────────────────────────────────


_T2_HEADERS = {"X-Forwarded-Email": "main@t2.example"}


# Pairs of (path, expected_status) when a T2 actor probes T1's
# resources.  404 is the right answer everywhere — a 200 means
# the row leaked, a 403 leaks the existence-distinction.
_T2_PROBES: list[tuple[str, str, int]] = [
    ("GET", "/boxes/1", 404),
    ("GET", "/boxes/1/audit", 404),
    ("GET", "/boxes/1/preview", 404),
    ("POST", "/boxes/1/edit", 404),
    ("POST", "/boxes/1/delete", 404),
    ("POST", "/boxes/1/items", 404),
    ("POST", "/boxes/1/share", 404),
    ("GET", "/items/10/preview", 404),
    ("POST", "/items/10/delete", 404),
    ("POST", "/items/10/share", 404),
    ("POST", "/items/10/move", 404),
    ("GET", "/locations/1", 404),
    ("POST", "/locations/1/delete", 404),
    ("GET", "/rooms/1/boxes", 404),
    ("POST", "/rooms/1/delete", 404),
    ("POST", "/floors/1/delete", 404),
    ("GET", "/api/v1/boxes/1", 404),
    ("GET", "/api/v1/boxes/1/items", 404),
    ("GET", "/api/v1/items/10", 404),
]


@pytest.mark.parametrize("method,path,expected", _T2_PROBES)
def test_cross_tenant_probes_404(tmp_path, monkeypatch,
                                 method, path, expected):
    """A T2 actor probing T1's resources gets 404 — never 403,
    never 200.  Tenant scoping in the DAO is the load-bearing
    surface; this test fails loudly if a route is added that
    forgets to consult the actor."""
    app_mod, _ = _bootstrap(tmp_path, monkeypatch)
    body = None
    if method == "POST":
        body = {"name": "x", "confirm": "T1 Kitchen", "box_id": "1",
                "recipient_email": "x@example.com"}
    with TestClient(app_mod.app, headers=_T2_HEADERS) as c:
        # Phase-11 API endpoints need a T2 bearer instead of the
        # X-Forwarded-Email path; auth still 404s the cross-tenant
        # probe via the DAO's tenant filter.
        if path.startswith("/api/v1/"):
            from dao import Actor, api_tokens as dao_api_tokens
            t2_actor = Actor(
                email="main@t2.example", tenant_id=2, role="maintainer",
                is_operator=False, memberships=((2, "maintainer"),),
                shares=(),
            )
            t2_token = dao_api_tokens.create(
                t2_actor, name="t2-probe",
            )["plaintext"]
            headers = {"Authorization": f"Bearer {t2_token}"}
            if method == "GET":
                r = c.get(path, headers=headers, follow_redirects=False)
            else:
                r = c.post(path, json=body, headers=headers,
                           follow_redirects=False)
        elif method == "GET":
            r = c.get(path, follow_redirects=False)
        else:
            r = c.post(path, data=body, follow_redirects=False)
    assert r.status_code == expected, (
        f"{method} {path}: expected {expected}, got {r.status_code} "
        f"(body: {r.text[:200]!r})"
    )


# ── Readonly role guard ─────────────────────────────────────────────


_RO_HEADERS = {"X-Forwarded-Email": "ro@t1.example"}

# Mutation routes that a readonly member must not be able to hit.
# 403 (ForbiddenError → HTTPException) is the right answer — the
# row exists and is in the right tenant, the actor just lacks the
# role.
_READONLY_MUTATIONS: list[tuple[str, str, dict | None]] = [
    ("POST", "/boxes", {"name": "x"}),
    ("POST", "/boxes/1/edit", {"name": "renamed"}),
    ("POST", "/boxes/1/items", {"name": "x"}),
    ("POST", "/items/10/move", {"box_id": "1"}),
    ("POST", "/items/10/delete", None),
    ("POST", "/items/10/share", {"recipient_email": "x@example.com"}),
    ("POST", "/boxes/1/share", {"recipient_email": "x@example.com"}),
    ("POST", "/locations", {"name": "x"}),
    ("POST", "/usage/api-tokens", {"name": "x"}),
    ("POST", "/usage/invites", {"email": "x@example.com"}),
    ("GET", "/usage/backup", None),
]


@pytest.mark.parametrize("method,path,body", _READONLY_MUTATIONS)
def test_readonly_actor_cannot_mutate(tmp_path, monkeypatch,
                                      method, path, body):
    """A readonly member of a tenant can browse but never write.
    The DAO's ``require_role(actor, "maintainer")`` gate fires;
    the route translates to 403."""
    app_mod, _ = _bootstrap(tmp_path, monkeypatch)
    with TestClient(app_mod.app, headers=_RO_HEADERS) as c:
        if method == "GET":
            r = c.get(path, follow_redirects=False)
        else:
            r = c.post(path, data=body, follow_redirects=False)
    assert r.status_code == 403, (
        f"{method} {path} as readonly returned {r.status_code} — "
        "expected 403"
    )


# ── Operator opacity ────────────────────────────────────────────────


_NON_OPERATOR_HEADERS = {"X-Forwarded-Email": "main@t1.example"}


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/admin", None),
    ("POST", "/admin/tenants", {"name": "x", "invitee_email": "y@example.com"}),
    ("POST", "/admin/tenants/1/backup", None),
])
def test_non_operator_admin_routes_404(tmp_path, monkeypatch,
                                       method, path, body):
    """A regular tenant maintainer (not in STASH_OPERATOR_EMAILS)
    hitting any /admin route gets 404 — opacity rule.  The surface
    is invisible to non-operators."""
    app_mod, _ = _bootstrap(tmp_path, monkeypatch)
    with TestClient(app_mod.app, headers=_NON_OPERATOR_HEADERS) as c:
        if method == "GET":
            r = c.get(path, follow_redirects=False)
        else:
            r = c.post(path, data=body, follow_redirects=False)
    assert r.status_code == 404, (
        f"{method} {path} as non-operator returned {r.status_code} "
        "— expected 404 (opacity rule)"
    )


# ── Bearer token revocation ─────────────────────────────────────────


def test_revoked_bearer_token_fails_auth(tmp_path, monkeypatch):
    """A token whose ``revoked_at`` is set must not authenticate.
    The revoke route (and the upcoming auto-revoke-on-HTTP path)
    rely on this."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    from dao import Actor, api_tokens as dao_api_tokens
    actor = Actor(
        email="main@t1.example", tenant_id=ids["t1"], role="maintainer",
        is_operator=False, memberships=((ids["t1"], "maintainer"),),
        shares=(),
    )
    token = dao_api_tokens.create(actor, name="revoke-me")
    plaintext = token["plaintext"]

    # Token works first.
    with TestClient(app_mod.app) as c:
        r = c.get("/api/v1/me",
                  headers={"Authorization": f"Bearer {plaintext}"})
        assert r.status_code == 200
    # Revoke at the DAO level + retry.
    dao_api_tokens.revoke(actor, token["id"])
    with TestClient(app_mod.app) as c:
        r = c.get("/api/v1/me",
                  headers={"Authorization": f"Bearer {plaintext}"})
        assert r.status_code == 401


# ── Bearer routing through bypass paths ─────────────────────────────


def test_invite_bypass_does_not_widen_to_other_paths(tmp_path, monkeypatch):
    """The invite bypass should match exactly /invite/<token> and
    /invite/<token>/accept — nothing else.  A future route at
    /invite/<token>/<extra> must NOT slip through the auth wall.
    We confirm with a contrived path the matcher should reject."""
    app_mod, _ = _bootstrap(tmp_path, monkeypatch)
    # Mint a real token so the bypass would fire if the matcher
    # were permissive.
    with app_mod.db() as conn:
        conn.execute(
            "INSERT INTO tenant_invites "
            "(token, tenant_id, email, role, created_by_email) "
            "VALUES ('realtoken123', 1, 'x@example.com', 'maintainer', 'admin@example.com')"
        )
        conn.commit()
    headers = {"X-Forwarded-Email": "x@example.com"}
    with TestClient(app_mod.app, headers=headers) as c:
        # Exact form: bypass fires, page renders.
        r = c.get("/invite/realtoken123")
        assert r.status_code == 200
        # Off-pattern: bypass must not fire — a path traversal
        # via a deeper segment does NOT widen access.
        r = c.get("/invite/realtoken123/something/else", follow_redirects=False)
        assert r.status_code in (403, 404), (
            f"/invite/<token>/extra leaked through auth wall "
            f"(status {r.status_code})"
        )
