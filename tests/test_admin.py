"""Phase 12 — operator dashboard.

End-to-end coverage of the bootstrap-a-friend's-tenant flow:

1. An operator (email in ``STASH_OPERATOR_EMAILS``) opens ``/admin``,
   sees the deployment-wide tenant roster.
2. Operator fills the create-tenant form with ``name=Sister`` +
   ``invitee_email=sister@example.com``.
3. POST creates the tenant, mints an invite, and redirects back to
   ``/admin?invite_url=…``.  The page renders the link.
4. Operator copies the link, sends it out-of-band.
5. Sister signs in (oauth2-proxy lets her through), middleware
   bypass routes her to the redemption page, she accepts, and is
   the sole maintainer of the new tenant.

Hard rule (spec § "Operator surface"): non-operators must not see
``/admin`` exists at all.  We assert 404 (not 403) so a curious
maintainer can't probe for the operator URL space.
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


def _bootstrap_app(tmp_path, monkeypatch, *, operator_email: str | None = None):
    """Spin up an empty stash with no pre-existing tenants.  Returns
    the imported app module so the test can introspect / make
    TestClients with arbitrary X-Forwarded-Email headers."""
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
    if "app" in sys.modules:
        del sys.modules["app"]
    import app as app_module
    importlib.reload(app_module)
    import vault
    vault.clear_dek_cache()
    return app_module


# ── Operator gating ─────────────────────────────────────────────────


def test_admin_404s_for_non_operator(tmp_path, monkeypatch):
    """A maintainer of an existing tenant — but not an operator —
    must not see /admin even exists.  404, not 403, so the surface
    stays opaque."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    # Stand up a normal tenant + maintainer member.
    with app_mod.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('Normal', 'pro')",
        )
        tid = cur.lastrowid
        conn.execute(
            "INSERT INTO tenant_members "
            "(tenant_id, email, role, joined_at) "
            "VALUES (?, 'normal@example.com', 'maintainer', "
            " CURRENT_TIMESTAMP)",
            (tid,),
        )
        conn.commit()
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": "normal@example.com"}) as c:
        r = c.get("/admin", follow_redirects=False)
        assert r.status_code == 404


def test_admin_renders_for_operator(tmp_path, monkeypatch):
    """Operator with no membership still sees /admin and the empty
    tenant list."""
    app_mod = _bootstrap_app(
        tmp_path, monkeypatch, operator_email="op@example.com",
    )
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": "op@example.com"}) as c:
        r = c.get("/admin")
        assert r.status_code == 200
        assert "Operator dashboard" in r.text


# ── Tenant + invite bootstrap ───────────────────────────────────────


def test_admin_create_tenant_and_invite_end_to_end(tmp_path, monkeypatch):
    """The friend-onboarding walk-through, in test form:
    operator creates Sister tenant + invite, copies the link,
    sister signs in, accepts, becomes the sole maintainer."""
    app_mod = _bootstrap_app(
        tmp_path, monkeypatch, operator_email="op@example.com",
    )
    op_headers = {"X-Forwarded-Email": "op@example.com"}
    sis_headers = {"X-Forwarded-Email": "sister@example.com"}

    with TestClient(app_mod.app, headers=op_headers) as op_client:
        # 1. Create tenant + mint invite in one shot.
        r = op_client.post(
            "/admin/tenants",
            data={
                "name": "Sister",
                "invitee_email": "sister@example.com",
                "role": "maintainer",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "/admin" in r.headers["location"]
        assert "invite_url=" in r.headers["location"]

        # 2. Tenant exists with zero members + one outstanding invite.
        page = op_client.get("/admin").text
        assert "Sister" in page
        with app_mod.db() as conn:
            row = conn.execute(
                "SELECT id FROM tenants WHERE name = 'Sister'"
            ).fetchone()
            tid = row["id"]
            members = conn.execute(
                "SELECT COUNT(*) FROM tenant_members WHERE tenant_id = ?",
                (tid,),
            ).fetchone()[0]
            invites = conn.execute(
                "SELECT token FROM tenant_invites WHERE tenant_id = ?",
                (tid,),
            ).fetchone()
        assert members == 0
        token = invites["token"]

    # 3. Sister, with no membership, can land on the invite page
    #    via the middleware bypass.
    with TestClient(app_mod.app, headers=sis_headers) as sis:
        r = sis.get(f"/invite/{token}")
        assert r.status_code == 200
        assert "Sister" in r.text

        # 4. Accept → membership granted → next request works.
        r = sis.post(f"/invite/{token}/accept", follow_redirects=False)
        assert r.status_code == 303
        r = sis.get("/")
        assert r.status_code == 200

    # 5. Operator's view confirms the membership landed.
    with app_mod.db() as conn:
        row = conn.execute(
            "SELECT email, role FROM tenant_members "
            "WHERE tenant_id = ?",
            (tid,),
        ).fetchone()
    assert row["email"] == "sister@example.com"
    assert row["role"] == "maintainer"


def test_admin_post_404s_for_non_operator(tmp_path, monkeypatch):
    """The POST surface is also operator-gated — not just the GET.
    Otherwise a curious maintainer who guessed the URL could mint
    cross-tenant invites."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with app_mod.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('Normal', 'pro')",
        )
        tid = cur.lastrowid
        conn.execute(
            "INSERT INTO tenant_members "
            "(tenant_id, email, role, joined_at) "
            "VALUES (?, 'sneak@example.com', 'maintainer', "
            " CURRENT_TIMESTAMP)",
            (tid,),
        )
        conn.commit()
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": "sneak@example.com"}) as c:
        r = c.post(
            "/admin/tenants",
            data={"name": "Steal", "invitee_email": "x@example.com"},
            follow_redirects=False,
        )
        assert r.status_code == 404


# ── DAO surface direct ──────────────────────────────────────────────


def test_dao_list_all_requires_operator(tmp_path, monkeypatch):
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    from dao import Actor, ForbiddenError, tenants as dao_tenants
    plain = Actor(
        email="plain@example.com", tenant_id=1, role="maintainer",
        is_operator=False, memberships=((1, "maintainer"),),
    )
    with pytest.raises(ForbiddenError):
        dao_tenants.list_all(plain)


def test_dao_create_tenant_audits(tmp_path, monkeypatch):
    """Operator-driven tenant creation writes an audit_log entry —
    the only cross-tenant trail we have today of operator activity."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    from dao import Actor, tenants as dao_tenants
    op = Actor(
        email="op@example.com", tenant_id=None, role=None,
        is_operator=True, memberships=(),
    )
    tid = dao_tenants.create_tenant(op, "Bootstrap", plan="free")
    with app_mod.db() as conn:
        row = conn.execute(
            "SELECT actor_email, action, target_id FROM audit_log "
            "WHERE action = 'tenant.create'"
        ).fetchone()
    assert row["actor_email"] == "op@example.com"
    assert row["target_id"] == tid


def test_list_all_includes_last_activity(tmp_path, monkeypatch):
    """``last_activity_at`` reflects the most recent audit_log
    write for that tenant — that's what powers the new "Last
    activity" column on /admin so an operator can spot a tenant
    that's gone quiet."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    from dao import Actor, tenants as dao_tenants
    with app_mod.db() as conn:
        cur = conn.execute("INSERT INTO tenants (name, plan) VALUES ('T1', 'pro')")
        t1 = cur.lastrowid
        cur = conn.execute("INSERT INTO tenants (name, plan) VALUES ('Quiet', 'pro')")
        t2 = cur.lastrowid
        # Quiet tenant gets a dated audit row; T1 gets one a tick later.
        conn.execute(
            "INSERT INTO audit_log (tenant_id, actor_email, action, "
            " target_kind, target_id, created_at) "
            "VALUES (?, 'a@example.com', 'box.create', 'box', 1, "
            "        '2025-01-01T00:00:00Z')",
            (t2,),
        )
        conn.execute(
            "INSERT INTO audit_log (tenant_id, actor_email, action, "
            " target_kind, target_id, created_at) "
            "VALUES (?, 'a@example.com', 'box.create', 'box', 2, "
            "        '2026-05-01T12:00:00Z')",
            (t1,),
        )
        conn.commit()
    op = Actor(
        email="op@example.com", tenant_id=None, role=None,
        is_operator=True, memberships=(),
    )
    rows = {r["id"]: r for r in dao_tenants.list_all(op)}
    assert rows[t1]["last_activity_at"] == "2026-05-01T12:00:00Z"
    assert rows[t2]["last_activity_at"] == "2025-01-01T00:00:00Z"


def test_list_members_includes_last_active(tmp_path, monkeypatch):
    """Per-member ``last_active_at`` joins on ``actor_email`` so the
    same email's activity across any tenant counts toward "active"
    — the spec's identity is email-keyed, not membership-keyed."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    from dao import Actor, tenants as dao_tenants
    with app_mod.db() as conn:
        cur = conn.execute("INSERT INTO tenants (name, plan) VALUES ('T1', 'pro')")
        tid = cur.lastrowid
        conn.execute(
            "INSERT INTO tenant_members "
            "(tenant_id, email, role, joined_at) "
            "VALUES (?, 'busy@example.com', 'maintainer', CURRENT_TIMESTAMP), "
            "       (?, 'idle@example.com', 'readonly',   CURRENT_TIMESTAMP)",
            (tid, tid),
        )
        conn.execute(
            "INSERT INTO audit_log (tenant_id, actor_email, action, "
            " target_kind, target_id, created_at) "
            "VALUES (?, 'busy@example.com', 'box.update', 'box', 1, "
            "        '2026-04-15T10:00:00Z')",
            (tid,),
        )
        conn.commit()
    op = Actor(
        email="op@example.com", tenant_id=None, role=None,
        is_operator=True, memberships=(),
    )
    rows = {r["email"]: r for r in dao_tenants.list_members(op, tid)}
    assert rows["busy@example.com"]["last_active_at"] == "2026-04-15T10:00:00Z"
    assert rows["idle@example.com"]["last_active_at"] is None


def test_audit_recent_for_operator_requires_operator(tmp_path, monkeypatch):
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    from dao import Actor, ForbiddenError, audit as dao_audit
    plain = Actor(
        email="plain@example.com", tenant_id=1, role="maintainer",
        is_operator=False, memberships=((1, "maintainer"),),
    )
    with pytest.raises(ForbiddenError):
        dao_audit.list_recent_for_operator(plain)


def test_audit_recent_for_operator_returns_joined_tenant_name(
    tmp_path, monkeypatch,
):
    """Operator gets actor_email + tenant_name + action in one shot —
    the recent-activity feed renders without a follow-up DB hit per
    row."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    from dao import Actor, audit as dao_audit
    with app_mod.db() as conn:
        cur = conn.execute("INSERT INTO tenants (name, plan) VALUES ('Acme', 'pro')")
        tid = cur.lastrowid
        conn.execute(
            "INSERT INTO audit_log (tenant_id, actor_email, action, "
            " target_kind, target_id, created_at) "
            "VALUES (?, 'a@example.com', 'box.create', 'box', 7, "
            "        '2026-05-01T00:00:00Z')",
            (tid,),
        )
        # Cross-tenant operator action with NULL tenant_id still
        # surfaces — we rely on this to spot oauth.client.register
        # bursts.
        conn.execute(
            "INSERT INTO audit_log (tenant_id, actor_email, action, "
            " target_kind, target_id, created_at) "
            "VALUES (NULL, 'op@example.com', 'oauth.client.register', "
            "        'oauth_client', 'abc', '2026-05-02T00:00:00Z')",
        )
        conn.commit()
    op = Actor(
        email="op@example.com", tenant_id=None, role=None,
        is_operator=True, memberships=(),
    )
    rows = dao_audit.list_recent_for_operator(op)
    actions = [r["action"] for r in rows]
    assert "oauth.client.register" in actions
    box_row = next(r for r in rows if r["action"] == "box.create")
    assert box_row["tenant_name"] == "Acme"
    cross_row = next(r for r in rows if r["action"] == "oauth.client.register")
    assert cross_row["tenant_name"] is None


def test_admin_renders_filter_ui_and_recent_activity(tmp_path, monkeypatch):
    """The admin page surfaces the API-token filter UI markup and
    the recent-activity card — both are entirely client-rendered
    after this so the filter JS has DOM to bind to.  Filter UI is
    inside ``{% if api_tokens %}`` so we seed one row before
    checking the markup."""
    app_mod = _bootstrap_app(
        tmp_path, monkeypatch, operator_email="op@example.com",
    )
    with app_mod.db() as conn:
        cur = conn.execute("INSERT INTO tenants (name, plan) VALUES ('Acme', 'pro')")
        tid = cur.lastrowid
        conn.execute(
            "INSERT INTO api_tokens (tenant_id, token_hash, name, role, "
            " created_by_email) "
            "VALUES (?, 'abc123', 'ci-bot', 'maintainer', 'op@example.com')",
            (tid,),
        )
        conn.commit()
    with TestClient(
        app_mod.app, headers={"X-Forwarded-Email": "op@example.com"},
    ) as c:
        r = c.get("/admin")
        assert r.status_code == 200
        # Filter bar is keyed by data-filter-target so the JS can
        # find its tbody — the assertion guards against renames
        # that would silently disable filtering.
        assert 'data-filter-target="#api-tokens-tbody"' in r.text
        assert 'data-filter-key="tenant"' in r.text
        assert 'data-filter-key="state"' in r.text
        assert 'data-filter-key="role"' in r.text
        assert 'data-filter-key="name"' in r.text
        assert "Recent activity" in r.text
        assert "Last activity" in r.text


def test_dao_invite_create_operator_bypass(tmp_path, monkeypatch):
    """An operator may mint into a tenant they don't belong to;
    a maintainer of *another* tenant cannot."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    from dao import Actor, ForbiddenError, invites as dao_invites
    # Set up two tenants; the actor is a maintainer of T1 only.
    with app_mod.db() as conn:
        c1 = conn.execute("INSERT INTO tenants (name, plan) VALUES ('T1', 'pro')")
        t1 = c1.lastrowid
        c2 = conn.execute("INSERT INTO tenants (name, plan) VALUES ('T2', 'pro')")
        t2 = c2.lastrowid
        conn.execute(
            "INSERT INTO tenant_members "
            "(tenant_id, email, role, joined_at) "
            "VALUES (?, 'm@example.com', 'maintainer', CURRENT_TIMESTAMP)",
            (t1,),
        )
        conn.commit()
    op = Actor(
        email="op@example.com", tenant_id=None, role=None,
        is_operator=True, memberships=(),
    )
    maint = Actor(
        email="m@example.com", tenant_id=t1, role="maintainer",
        is_operator=False, memberships=((t1, "maintainer"),),
    )
    # Operator bypass: works.
    invite = dao_invites.create(
        op, email="x@example.com", role="maintainer", tenant_id=t2,
    )
    assert invite["tenant_id"] == t2
    # Maintainer of T1 trying to mint into T2: forbidden.
    with pytest.raises(ForbiddenError):
        dao_invites.create(
            maint, email="x@example.com", role="maintainer", tenant_id=t2,
        )
