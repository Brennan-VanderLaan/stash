"""Self-serve signup + free-tier capacity tunable.

Three coupled product surfaces tested here:

1. **/signup self-serve flow** — an authed user with no tenant
   creates one through the page, becomes the sole maintainer,
   lands on /home.  Refuses when the free pool is full,
   rate-limit-honest under abuse.
2. **Free-tier capacity** — operator-tunable pool size +
   per-tenant cap → slot count.  Active free tenants count
   against the pool; soft-deleted ones do not.
3. **Operator bump path** — POST /admin/free-tier-capacity
   changes the pool size, immediately reflected in subsequent
   reads of the capacity helper.
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


def _bootstrap_app(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("STASH_DB", str(tmp_path / "stash.db"))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.setenv(
        "STASH_KEK",
        base64.b64encode(secrets.token_bytes(32)).decode(),
    )
    monkeypatch.delenv("STASH_OPERATOR_EMAILS", raising=False)
    monkeypatch.setenv("STASH_REQUIRE_HTTPS_TOKENS", "false")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if "app" in sys.modules:
        del sys.modules["app"]
    import app as app_module
    importlib.reload(app_module)
    return app_module


# ── Capacity helper ─────────────────────────────────────────────────


def test_capacity_default_pool_yields_expected_slot_count(client):
    """Default pool size (10 GB) ÷ free per-tenant cap (100 MB)
    = 102 slots (integer floor of 10 240 / 100)."""
    cap = client.app_module.dao_quotas.free_tier_capacity()
    assert cap["per_slot_bytes"] == 100 * 1024 * 1024
    assert cap["total_bytes"] == 10 * 1024 * 1024 * 1024
    # 10 GB = 10,240 MB; ÷ 100 MB = 102.4 → 102 slots.
    assert cap["total_slots"] == 102
    # Conftest creates one 'pro' test tenant, no free tenants.
    assert cap["used_slots"] == 0
    assert cap["available_slots"] == 102
    assert cap["is_full"] is False


def test_capacity_only_counts_free_plan_tenants(client):
    """A pro tenant doesn't count against the free pool.  A
    soft-deleted free tenant doesn't count either (recoverable
    via undelete; the slot stays held only for active rows)."""
    with client.app_module.db() as conn:
        conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('Free A', 'free')"
        )
        conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('Free B', 'free')"
        )
        # Pro doesn't count.
        conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('Pro X', 'pro')"
        )
        # Soft-deleted free doesn't count.
        conn.execute(
            "INSERT INTO tenants (name, plan, deleted_at) "
            "VALUES ('Dead Free', 'free', '2026-01-01')"
        )
        conn.commit()
    cap = client.app_module.dao_quotas.free_tier_capacity()
    assert cap["used_slots"] == 2


def test_capacity_shrinks_when_pool_drops_below_used(client):
    """If the operator scales storage DOWN (or the pool gets
    bumped lower than current usage), available clamps to 0
    rather than going negative."""
    with client.app_module.db() as conn:
        for i in range(5):
            conn.execute(
                "INSERT INTO tenants (name, plan) VALUES (?, 'free')",
                (f"Free {i}",),
            )
        conn.commit()
    # Set pool size to 200 MB total — fits 2 slots; we already
    # have 5 used.
    from dao._base import Actor
    op = Actor(email="op@example.com", tenant_id=None, role=None,
               is_operator=True, memberships=(), shares=())
    client.app_module.dao_settings.set_value(
        op, "free_tier_bytes_total", str(200 * 1024 * 1024),
    )
    cap = client.app_module.dao_quotas.free_tier_capacity()
    assert cap["total_slots"] == 2
    assert cap["used_slots"] == 5
    assert cap["available_slots"] == 0
    assert cap["is_full"] is True


# ── /signup route ───────────────────────────────────────────────────


def test_signup_get_renders_form_for_authed_no_tenant_user(
    tmp_path, monkeypatch,
):
    """A signed-in email with no tenant gets the create-your-stash
    form on GET /signup."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with TestClient(
        app_mod.app,
        headers={"X-Forwarded-Email": "newcomer@example.com"},
    ) as c:
        r = c.get("/signup")
    assert r.status_code == 200
    assert "Create my stash" in r.text


def test_signup_post_creates_tenant_and_membership(
    tmp_path, monkeypatch,
):
    """Submitting the form creates a tenant + makes the user its
    sole maintainer + redirects to /home."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with TestClient(
        app_mod.app,
        headers={"X-Forwarded-Email": "newcomer@example.com"},
    ) as c:
        r = c.post(
            "/signup",
            data={"tenant_name": "Brand New Stash"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/home"
    with app_mod.db() as conn:
        tenant = conn.execute(
            "SELECT id, name, plan FROM tenants WHERE name = 'Brand New Stash'"
        ).fetchone()
        member = conn.execute(
            "SELECT email, role FROM tenant_members WHERE tenant_id = ?",
            (tenant["id"],),
        ).fetchone()
    assert tenant["plan"] == "free"
    assert member["email"] == "newcomer@example.com"
    assert member["role"] == "maintainer"


def test_signup_refuses_when_pool_full(tmp_path, monkeypatch):
    """When the free-tier pool has no slots left, signup renders
    the "free tier full" copy with a 409 + does NOT create a
    tenant."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    # Drain the pool by setting it to zero.
    from dao._base import Actor
    op = Actor(email="op@example.com", tenant_id=None, role=None,
               is_operator=True, memberships=(), shares=())
    app_mod.dao_settings.set_value(
        op, "free_tier_bytes_total", "0",
    )
    with TestClient(
        app_mod.app,
        headers={"X-Forwarded-Email": "newcomer@example.com"},
    ) as c:
        r = c.post(
            "/signup",
            data={"tenant_name": "Won't happen"},
            follow_redirects=False,
        )
    assert r.status_code == 409
    assert "Free tier" in r.text
    # No tenant created.
    with app_mod.db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM tenants WHERE name = 'Won''t happen'"
        ).fetchone()["n"]
    assert n == 0


def test_signup_empty_name_redirects_back_with_error(
    tmp_path, monkeypatch,
):
    """Whitespace-only tenant_name doesn't create — bounces back
    to the form with an error hint."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with TestClient(
        app_mod.app,
        headers={"X-Forwarded-Email": "newcomer@example.com"},
    ) as c:
        r = c.post(
            "/signup",
            data={"tenant_name": "   "},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert "name_error" in r.headers["location"]


def test_no_tenant_user_redirected_to_signup_from_other_paths(
    tmp_path, monkeypatch,
):
    """An authed user with no tenant hitting / or /home gets a
    303 to /signup — the friendly self-serve loop closes."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with TestClient(
        app_mod.app,
        headers={
            "X-Forwarded-Email": "newcomer@example.com",
            "Accept": "text/html",
        },
    ) as c:
        r = c.get("/home", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/signup"


# ── Operator capacity bump ──────────────────────────────────────────


def test_operator_can_bump_free_tier_pool(tmp_path, monkeypatch):
    """POST /admin/free-tier-capacity updates the pool;
    capacity helper reads the new value immediately."""
    app_mod = _bootstrap_app(
        tmp_path, monkeypatch,
        STASH_OPERATOR_EMAILS="op@example.com",
    )
    with TestClient(
        app_mod.app,
        headers={"X-Forwarded-Email": "op@example.com"},
    ) as c:
        # Bump to 50 GB.
        r = c.post(
            "/admin/free-tier-capacity",
            data={"total_bytes": str(50 * 1024 * 1024 * 1024)},
            follow_redirects=False,
        )
    assert r.status_code == 303
    cap = app_mod.dao_quotas.free_tier_capacity()
    assert cap["total_bytes"] == 50 * 1024 * 1024 * 1024
    # 50 GB / 100 MB = 512 slots.
    assert cap["total_slots"] == 512


def test_non_operator_cannot_bump_pool(tmp_path, monkeypatch):
    """The capacity bump endpoint is operator-gated with the
    standard 404 opacity rule."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with app_mod.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('T', 'pro')"
        )
        tid = cur.lastrowid
        conn.execute(
            "INSERT INTO tenant_members "
            "(tenant_id, email, role, joined_at) "
            "VALUES (?, 'sneak@example.com', 'maintainer', CURRENT_TIMESTAMP)",
            (tid,),
        )
        conn.commit()
    with TestClient(
        app_mod.app,
        headers={"X-Forwarded-Email": "sneak@example.com"},
    ) as c:
        r = c.post(
            "/admin/free-tier-capacity",
            data={"total_bytes": "99999999999"},
            follow_redirects=False,
        )
    assert r.status_code == 404


def test_capacity_bump_refuses_tiny_value(tmp_path, monkeypatch):
    """A bump below 1 MB gets a 400 — the floor catches an
    operator who typed MB but submitted bytes."""
    app_mod = _bootstrap_app(
        tmp_path, monkeypatch,
        STASH_OPERATOR_EMAILS="op@example.com",
    )
    with TestClient(
        app_mod.app,
        headers={"X-Forwarded-Email": "op@example.com"},
    ) as c:
        r = c.post(
            "/admin/free-tier-capacity",
            data={"total_bytes": "100"},  # 100 bytes — obvious typo
            follow_redirects=False,
        )
    assert r.status_code == 400


# ── Landing page banner ─────────────────────────────────────────────


def test_landing_page_shows_free_slot_count(tmp_path, monkeypatch):
    """The public landing renders the live free-slot count so
    prospective signups see the size of the free tier without
    having to click through to /signup first."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        r = c.get("/")
    assert r.status_code == 200
    # 102 free slots available with the default 10 GB pool.
    assert "102" in r.text
    assert "free spots open" in r.text
