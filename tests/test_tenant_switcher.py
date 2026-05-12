"""Tenant switcher — cookie-driven active-tenant selection.

The actor middleware honours a ``stash_active_tenant`` cookie when
its value names a tenant the user genuinely belongs to.  Tests
pin: the cookie persists across requests, an invalid value falls
back silently (no 500, no lockout), the switch route 404s on a
non-member tenant, and the dropdown renders for multi-membership
users only (single-membership users get no clutter).
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


def _bootstrap_two_tenant_app(tmp_path, monkeypatch):
    """Stand up an app with TWO tenants the test email is a member
    of, so the switcher has something to switch *between*."""
    monkeypatch.setenv("STASH_DB", str(tmp_path / "stash.db"))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.setenv(
        "STASH_KEK", base64.b64encode(secrets.token_bytes(32)).decode(),
    )
    monkeypatch.setenv("STASH_REQUIRE_HTTPS_TOKENS", "false")
    monkeypatch.delenv("STASH_OPERATOR_EMAILS", raising=False)
    if "app" in sys.modules:
        del sys.modules["app"]
    import app as app_module
    importlib.reload(app_module)
    import vault
    vault.clear_dek_cache()
    email = "user@example.com"
    with app_module.db() as conn:
        c1 = conn.execute("INSERT INTO tenants (name, plan) VALUES ('TenantA', 'pro')")
        tid_a = c1.lastrowid
        c2 = conn.execute("INSERT INTO tenants (name, plan) VALUES ('TenantB', 'pro')")
        tid_b = c2.lastrowid
        conn.execute(
            "INSERT INTO tenant_members (tenant_id, email, role, joined_at) "
            "VALUES (?, ?, 'maintainer', CURRENT_TIMESTAMP), "
            "       (?, ?, 'readonly',   CURRENT_TIMESTAMP)",
            (tid_a, email, tid_b, email),
        )
        conn.commit()
    return app_module, email, tid_a, tid_b


def test_switcher_dropdown_renders_for_multi_tenant_user(tmp_path, monkeypatch):
    """Two memberships → dropdown is present + lists both tenant
    names.  Single-tenant users (the default ``client`` fixture)
    don't get the switcher to avoid clutter — verified by the
    ``test_switcher_hidden_for_single_membership`` test below."""
    app_mod, email, tid_a, tid_b = _bootstrap_two_tenant_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": email}) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert 'id="tenant-switcher"' in r.text
        assert "TenantA" in r.text
        assert "TenantB" in r.text


def test_switcher_hidden_for_single_membership(client):
    """Default ``client`` fixture has exactly one tenant + no
    shares → switcher absent."""
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="tenant-switcher"' not in r.text


def test_switch_post_sets_cookie_and_redirects(tmp_path, monkeypatch):
    """POST /tenants/switch with a valid tenant_id → 303 with the
    Set-Cookie header carrying the chosen tenant; ``next`` is
    honoured for the Location."""
    app_mod, email, tid_a, tid_b = _bootstrap_two_tenant_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": email}) as c:
        r = c.post(
            "/tenants/switch",
            data={"tenant_id": str(tid_b), "next": "/usage"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/usage"
    set_cookie = r.headers.get("set-cookie", "")
    assert "stash_active_tenant=" in set_cookie
    assert str(tid_b) in set_cookie


def test_switch_post_rejects_non_member_tenant(tmp_path, monkeypatch):
    """A tenant the user doesn't belong to → 404 (matches the
    operator-opacity rule).  The cookie must NOT be set on this
    response so a tampered POST can't sneak through later."""
    app_mod, email, _, _ = _bootstrap_two_tenant_app(tmp_path, monkeypatch)
    with app_mod.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('OtherTenant', 'pro')",
        )
        other_tid = cur.lastrowid
        conn.commit()
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": email}) as c:
        r = c.post(
            "/tenants/switch",
            data={"tenant_id": str(other_tid)},
            follow_redirects=False,
        )
    assert r.status_code == 404
    assert "stash_active_tenant=" not in r.headers.get("set-cookie", "")


def test_switch_post_rejects_garbage_tenant_id(tmp_path, monkeypatch):
    app_mod, email, _, _ = _bootstrap_two_tenant_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": email}) as c:
        r = c.post(
            "/tenants/switch",
            data={"tenant_id": "not-an-int"},
            follow_redirects=False,
        )
    assert r.status_code == 400


def test_cookie_drives_active_tenant_resolution(tmp_path, monkeypatch):
    """When the cookie names a valid membership, the actor's
    ``tenant_id`` resolves to that tenant — not memberships[0].
    Easiest to assert via /usage's "Tenant" stat which renders
    the active tenant's name."""
    app_mod, email, tid_a, tid_b = _bootstrap_two_tenant_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": email}) as c:
        # No cookie → memberships[0] wins (TenantA, oldest joined).
        r = c.get("/usage")
        assert "TenantA" in r.text
        # Switch to TenantB.
        c.cookies.set("stash_active_tenant", str(tid_b))
        r = c.get("/usage")
        assert "TenantB" in r.text


def test_invalid_cookie_value_falls_back_to_default(tmp_path, monkeypatch):
    """A junk cookie (non-int, or a tenant_id the user isn't a
    member of) must NOT lock the user out — middleware silently
    falls back to memberships[0]."""
    app_mod, email, tid_a, _ = _bootstrap_two_tenant_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": email}) as c:
        c.cookies.set("stash_active_tenant", "999999")
        r = c.get("/usage")
        assert r.status_code == 200
        # Active tenant is TenantA (the default), not a 500.
        assert "TenantA" in r.text


def test_switch_post_open_redirect_guard(tmp_path, monkeypatch):
    """An attacker-controlled ``next`` must NOT redirect off-site —
    only relative paths beneath the app are honoured."""
    app_mod, email, tid_a, _ = _bootstrap_two_tenant_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": email}) as c:
        r = c.post(
            "/tenants/switch",
            data={"tenant_id": str(tid_a), "next": "//evil.example.com"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
