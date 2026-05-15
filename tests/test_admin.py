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
        r = sis.get("/home")
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


# ── Lifecycle controls (phase 12 deferred) ───────────────────────


def test_soft_delete_sets_deleted_at_and_grace(tmp_path, monkeypatch):
    """``soft_delete`` stamps ``deleted_at`` + a
    ``hard_delete_after`` 30 days out, and writes an audit row.
    Idempotent on a re-call (bumps the grace window forward)."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    from dao import Actor, tenants as dao_tenants
    with app_mod.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('Doomed', 'free')",
        )
        tid = cur.lastrowid
        conn.commit()
    op = Actor(
        email="op@example.com", tenant_id=None, role=None,
        is_operator=True, memberships=(),
    )
    dao_tenants.soft_delete(op, tid)
    with app_mod.db() as conn:
        row = conn.execute(
            "SELECT deleted_at, hard_delete_after FROM tenants WHERE id = ?",
            (tid,),
        ).fetchone()
        audit = conn.execute(
            "SELECT action, actor_email FROM audit_log "
            "WHERE action = 'tenant.soft_delete'"
        ).fetchone()
    assert row["deleted_at"] is not None
    assert row["hard_delete_after"] is not None
    assert audit["actor_email"] == "op@example.com"


def test_reactivate_clears_soft_delete(tmp_path, monkeypatch):
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    from dao import Actor, tenants as dao_tenants
    with app_mod.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants "
            "(name, plan, deleted_at, hard_delete_after) "
            "VALUES ('Frozen', 'free', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        )
        tid = cur.lastrowid
        conn.commit()
    op = Actor(
        email="op@example.com", tenant_id=None, role=None,
        is_operator=True, memberships=(),
    )
    result = dao_tenants.reactivate(op, tid)
    assert result["already_active"] is False
    with app_mod.db() as conn:
        row = conn.execute(
            "SELECT deleted_at, hard_delete_after FROM tenants WHERE id = ?",
            (tid,),
        ).fetchone()
    assert row["deleted_at"] is None
    assert row["hard_delete_after"] is None


def test_reactivate_on_active_is_noop(tmp_path, monkeypatch):
    """No audit row, no exception — operator clicked reactivate
    on an already-active tenant."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    from dao import Actor, tenants as dao_tenants
    with app_mod.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('Already', 'free')",
        )
        tid = cur.lastrowid
        conn.commit()
    op = Actor(
        email="op@example.com", tenant_id=None, role=None,
        is_operator=True, memberships=(),
    )
    result = dao_tenants.reactivate(op, tid)
    assert result["already_active"] is True
    with app_mod.db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE action = 'tenant.reactivate'"
        ).fetchone()[0]
    assert n == 0


def test_hard_delete_cascades_and_audits(tmp_path, monkeypatch):
    """Hard-delete must drop the tenant row + every cascade-
    referencing row.  Audit row lives at ``tenant_id=NULL`` so
    the permanent record survives the cascade."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    from dao import Actor, tenants as dao_tenants
    with app_mod.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('NukeMe', 'free')",
        )
        tid = cur.lastrowid
        conn.execute(
            "INSERT INTO boxes (name, tenant_id) VALUES ('B', ?)", (tid,),
        )
        conn.commit()
    op = Actor(
        email="op@example.com", tenant_id=None, role=None,
        is_operator=True, memberships=(),
    )
    dao_tenants.hard_delete(op, tid)
    with app_mod.db() as conn:
        row = conn.execute(
            "SELECT 1 FROM tenants WHERE id = ?", (tid,),
        ).fetchone()
        boxes = conn.execute(
            "SELECT COUNT(*) FROM boxes WHERE tenant_id = ?", (tid,),
        ).fetchone()[0]
        audit = conn.execute(
            "SELECT tenant_id, target_id FROM audit_log "
            "WHERE action = 'tenant.hard_delete'"
        ).fetchone()
    assert row is None
    assert boxes == 0
    # Cross-tenant audit row keeps tenant_id=NULL but target_id
    # carries the deleted tenant's id for forensics.
    assert audit["tenant_id"] is None
    assert audit["target_id"] == tid


def test_hard_delete_refuses_self_tenant(tmp_path, monkeypatch):
    """Operator who's also a member of the target tenant can't
    nuke their own — would lock them out of /admin."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    from dao import Actor, tenants as dao_tenants
    with app_mod.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('Self', 'free')",
        )
        tid = cur.lastrowid
        conn.commit()
    op = Actor(
        email="op@example.com", tenant_id=tid, role="maintainer",
        is_operator=True, memberships=((tid, "maintainer"),),
    )
    with pytest.raises(ValueError):
        dao_tenants.hard_delete(op, tid)


def test_admin_route_hard_delete_requires_confirm(tmp_path, monkeypatch):
    """POST /admin/tenants/{id}/hard-delete without the matching
    ``confirm=<name>`` form field returns 400, and the tenant
    survives."""
    app_mod = _bootstrap_app(
        tmp_path, monkeypatch, operator_email="op@example.com",
    )
    with app_mod.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('Careful', 'free')",
        )
        tid = cur.lastrowid
        conn.commit()
    with TestClient(
        app_mod.app, headers={"X-Forwarded-Email": "op@example.com"},
    ) as c:
        r = c.post(
            f"/admin/tenants/{tid}/hard-delete",
            data={"confirm": "wrong"},
            follow_redirects=False,
        )
        assert r.status_code == 400
        # Tenant still alive.
        r2 = c.get("/admin")
        assert "Careful" in r2.text
        # Correct confirm → 303 + tenant gone.
        r3 = c.post(
            f"/admin/tenants/{tid}/hard-delete",
            data={"confirm": "Careful"},
            follow_redirects=False,
        )
        assert r3.status_code == 303
        r4 = c.get("/admin")
        assert "Careful" not in r4.text


# ── Vendor cost summary ──────────────────────────────────────────


def test_operator_cost_summary_aggregates_across_tenants(
    tmp_path, monkeypatch,
):
    """``operator_cost_summary`` sums AI costs across every
    tenant + groups per-kind + lists per-tenant rollup.  This is
    the data backing the vendor-cost panel on /admin."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    from dao import Actor, usage as dao_usage
    with app_mod.db() as conn:
        c1 = conn.execute("INSERT INTO tenants (name, plan) VALUES ('A', 'pro')")
        a = c1.lastrowid
        c2 = conn.execute("INSERT INTO tenants (name, plan) VALUES ('B', 'pro')")
        b = c2.lastrowid
        conn.commit()
    dao_usage.record(a, "ai", "gemini_detect")
    dao_usage.record(a, "ai", "gemini_detect")
    dao_usage.record(b, "ai", "gemini_art")
    op = Actor(
        email="op@example.com", tenant_id=None, role=None,
        is_operator=True, memberships=(),
    )
    out = dao_usage.operator_cost_summary(op)
    assert out["total_cost_micros"] > 0
    assert out["by_kind"]["gemini_detect"]["units"] == 2
    assert out["by_kind"]["gemini_art"]["units"] == 1
    by_name = {t["name"]: t for t in out["by_tenant"]}
    assert by_name["A"]["ai_calls"] == 2
    assert by_name["B"]["ai_calls"] == 1


def test_operator_cost_summary_requires_operator(tmp_path, monkeypatch):
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    from dao import Actor, ForbiddenError, usage as dao_usage
    plain = Actor(
        email="plain@example.com", tenant_id=1, role="maintainer",
        is_operator=False, memberships=((1, "maintainer"),),
    )
    with pytest.raises(ForbiddenError):
        dao_usage.operator_cost_summary(plain)


# ── OAuth client panel ───────────────────────────────────────────


def test_admin_renders_oauth_clients_and_revoke_works(
    tmp_path, monkeypatch,
):
    """OAuth client panel renders registered clients; the revoke
    route flips the row's ``revoked_at`` and the next render
    shows it as revoked."""
    app_mod = _bootstrap_app(
        tmp_path, monkeypatch, operator_email="op@example.com",
    )
    with app_mod.db() as conn:
        conn.execute(
            "INSERT INTO oauth_clients "
            "(client_id, name, redirect_uris, is_public, "
            " registered_by_email) "
            "VALUES ('abc', 'TestClient', '[\"https://x\"]', 1, "
            "        'someone@example.com')",
        )
        conn.commit()
    with TestClient(
        app_mod.app, headers={"X-Forwarded-Email": "op@example.com"},
    ) as c:
        r = c.get("/admin")
        assert "TestClient" in r.text
        assert "abc" in r.text
        r2 = c.post(
            "/admin/oauth-clients/abc/revoke",
            follow_redirects=False,
        )
        assert r2.status_code == 303
        r3 = c.get("/admin")
        # After revoke, status flips to "revoked".
        assert ">revoked<" in r3.text or "revoked" in r3.text


def test_admin_renders_vendor_cost_panel(tmp_path, monkeypatch):
    """Smoke: the vendor-cost card is in the rendered page so a
    future template refactor that drops it fails this test."""
    app_mod = _bootstrap_app(
        tmp_path, monkeypatch, operator_email="op@example.com",
    )
    with TestClient(
        app_mod.app, headers={"X-Forwarded-Email": "op@example.com"},
    ) as c:
        r = c.get("/admin")
        assert "Vendor cost" in r.text


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
