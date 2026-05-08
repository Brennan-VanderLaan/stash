"""Phase 5 — link-share tenant invites.

Covers the DAO surface (mint / get / redeem / revoke) and the
end-to-end HTTP flow that a maintainer + invitee actually walk
through:

1. Maintainer mints an invite via POST /usage/invites.
2. The page round-trips the URL into ``?invite_url=…`` so it can be
   copy-pasted out-of-band (no email plumbing yet).
3. Invitee, currently a non-member, hits GET /invite/<token> — the
   middleware bypass lets them through despite no membership.
4. Invitee POSTs to accept; tenant_members gains a row; the next
   request from that email sees a normal authenticated session.

The identity-vs-invite collision (spec § "Sign-up + onboarding") is
verified explicitly: an invite minted to ``alice@old.example.com``
that's accepted by ``alice@new.example.com`` binds the membership
to the actual sign-in identity and audit-logs the rebind.
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


def _new_tenant_app(tmp_path, monkeypatch, *, owner: str = "owner@example.com"):
    """Spin up a fresh app+db with a single tenant whose maintainer
    is ``owner``.  Returns ``(app_module, tenant_id)``."""
    monkeypatch.setenv("STASH_DB", str(tmp_path / "stash.db"))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.setenv("STASH_KEK",
                       base64.b64encode(secrets.token_bytes(32)).decode())
    monkeypatch.delenv("STASH_OPERATOR_EMAILS", raising=False)
    if "app" in sys.modules:
        del sys.modules["app"]
    import app as app_module
    importlib.reload(app_module)
    import vault
    vault.clear_dek_cache()
    with app_module.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('Movers', 'pro')"
        )
        tenant_id = cur.lastrowid
        conn.execute(
            "INSERT INTO tenant_members "
            "(tenant_id, email, role, joined_at) "
            "VALUES (?, ?, 'maintainer', CURRENT_TIMESTAMP)",
            (tenant_id, owner),
        )
        conn.commit()
    return app_module, tenant_id


# ── DAO surface ─────────────────────────────────────────────────────


def test_dao_create_and_redeem(tmp_path, monkeypatch):
    app_mod, tenant_id = _new_tenant_app(tmp_path, monkeypatch)
    from dao import Actor, invites as dao_invites

    owner = Actor(
        email="owner@example.com", tenant_id=tenant_id, role="maintainer",
        is_operator=False, memberships=((tenant_id, "maintainer"),),
    )
    invite = dao_invites.create(owner, email="wife@example.com",
                                role="maintainer")
    assert invite["token"]
    assert invite["email"] == "wife@example.com"
    assert invite["role"] == "maintainer"

    # Redeem with the same email — straight membership grant.
    result = dao_invites.redeem(invite["token"],
                                actual_email="wife@example.com")
    assert result == {
        "tenant_id": tenant_id, "role": "maintainer", "rebound": False,
    }

    # Membership row exists.
    with app_mod.db() as conn:
        row = conn.execute(
            "SELECT role FROM tenant_members "
            "WHERE tenant_id = ? AND email = ?",
            (tenant_id, "wife@example.com"),
        ).fetchone()
    assert row["role"] == "maintainer"

    # Token is now consumed — second redemption fails.
    from dao import NotFoundError
    with pytest.raises(NotFoundError):
        dao_invites.redeem(invite["token"], actual_email="wife@example.com")


def test_dao_redeem_with_different_email_rebinds(tmp_path, monkeypatch):
    """Identity-vs-invite collision: invite to ``alice@old`` accepted
    by ``alice@new`` binds to the actual sign-in identity and audits
    the rebind so the inviter can spot a surprise."""
    app_mod, tenant_id = _new_tenant_app(tmp_path, monkeypatch)
    from dao import Actor, invites as dao_invites

    owner = Actor(
        email="owner@example.com", tenant_id=tenant_id, role="maintainer",
        is_operator=False, memberships=((tenant_id, "maintainer"),),
    )
    invite = dao_invites.create(owner, email="alice@old.example.com")
    result = dao_invites.redeem(invite["token"],
                                actual_email="alice@new.example.com")
    assert result["rebound"] is True
    # Membership is on the *actual* email, not the typed one.
    with app_mod.db() as conn:
        row = conn.execute(
            "SELECT email FROM tenant_members WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchall()
        emails = {r["email"] for r in row}
        assert "alice@new.example.com" in emails
        assert "alice@old.example.com" not in emails
        # Audit log captures the rebind.
        audits = conn.execute(
            "SELECT metadata_json FROM audit_log "
            "WHERE action = 'invite.accept'",
        ).fetchall()
    import json
    metas = [json.loads(a["metadata_json"]) for a in audits]
    assert any(m.get("rebound") for m in metas)


def test_dao_redeem_expired_fails(tmp_path, monkeypatch):
    """A token whose expires_at is in the past must not redeem."""
    app_mod, tenant_id = _new_tenant_app(tmp_path, monkeypatch)
    from dao import Actor, NotFoundError, invites as dao_invites

    owner = Actor(
        email="owner@example.com", tenant_id=tenant_id, role="maintainer",
        is_operator=False, memberships=((tenant_id, "maintainer"),),
    )
    invite = dao_invites.create(owner, email="late@example.com")
    # Backdate the token by hand — same effect as it sitting on
    # somebody's desk for two months.
    with app_mod.db() as conn:
        conn.execute(
            "UPDATE tenant_invites SET expires_at = '2020-01-01T00:00:00+00:00' "
            "WHERE token = ?",
            (invite["token"],),
        )
        conn.commit()
    with pytest.raises(NotFoundError):
        dao_invites.redeem(invite["token"],
                           actual_email="late@example.com")


def test_dao_revoke_drops_outstanding(tmp_path, monkeypatch):
    app_mod, tenant_id = _new_tenant_app(tmp_path, monkeypatch)
    from dao import Actor, NotFoundError, invites as dao_invites

    owner = Actor(
        email="owner@example.com", tenant_id=tenant_id, role="maintainer",
        is_operator=False, memberships=((tenant_id, "maintainer"),),
    )
    invite = dao_invites.create(owner, email="oops@example.com")
    dao_invites.revoke(owner, invite["token"])
    # Revoked tokens become unredeemable.
    with pytest.raises(NotFoundError):
        dao_invites.redeem(invite["token"],
                           actual_email="oops@example.com")
    # Idempotent revoke — second call 404s.
    with pytest.raises(NotFoundError):
        dao_invites.revoke(owner, invite["token"])


# ── HTTP flow ───────────────────────────────────────────────────────


def test_http_invite_flow_end_to_end(tmp_path, monkeypatch):
    app_mod, tenant_id = _new_tenant_app(tmp_path, monkeypatch)
    owner_headers = {"X-Forwarded-Email": "owner@example.com"}
    invitee_headers = {"X-Forwarded-Email": "wife@example.com"}
    with TestClient(app_mod.app, headers=owner_headers) as owner_client, \
         TestClient(app_mod.app, headers=invitee_headers) as wife_client:
        # 1. Maintainer mints. Redirect carries ?invite_url= back.
        r = owner_client.post(
            "/usage/invites",
            data={"email": "wife@example.com", "role": "maintainer"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        loc = r.headers["location"]
        assert "invite_url=" in loc

        # 2. The /usage page now shows the freshly-minted link.
        page = owner_client.get(loc).text
        assert "/invite/" in page
        # Pull the token out of the URL the page rendered.
        import re
        m = re.search(r"/invite/([A-Za-z0-9_-]{16,})", page)
        assert m, "invite link not surfaced on /usage"
        token = m.group(1)

        # 3. Invitee, no membership yet, can still hit /invite/<token>
        #    because the middleware bypass fires.
        r = wife_client.get(f"/invite/{token}")
        assert r.status_code == 200
        assert "Movers" in r.text
        # Confirm she's still 403'd on a normal page (proves it's the
        # bypass that let her through, not a stale membership).
        r = wife_client.get("/", follow_redirects=False)
        assert r.status_code == 403

        # 4. Accept. Redirects home, and the next page load works.
        r = wife_client.post(f"/invite/{token}/accept",
                             follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        r = wife_client.get("/")
        assert r.status_code == 200

        # 5. /usage shows wife as a member now.
        members_page = owner_client.get("/usage").text
        assert "wife@example.com" in members_page


def test_http_invite_revoked_token_returns_404(tmp_path, monkeypatch):
    """Once an invite is revoked, the accept POST 404s — no silent
    success that leaves the recipient thinking they joined."""
    app_mod, tenant_id = _new_tenant_app(tmp_path, monkeypatch)
    owner_headers = {"X-Forwarded-Email": "owner@example.com"}
    with TestClient(app_mod.app, headers=owner_headers) as owner_client:
        owner_client.post(
            "/usage/invites",
            data={"email": "ghost@example.com"},
            follow_redirects=False,
        )
        # Pull the freshly-minted token directly from the DAO listing.
        # Avoids brittle URL parsing from the redirect chain.
        from dao import Actor, invites as dao_invites
        owner = Actor(
            email="owner@example.com", tenant_id=tenant_id,
            role="maintainer", is_operator=False,
            memberships=((tenant_id, "maintainer"),),
        )
        outstanding = dao_invites.list_for_tenant(owner)
        assert outstanding, "expected one outstanding invite"
        token = outstanding[0]["token"]
        # Revoke from the maintainer's side.
        rev = owner_client.post(f"/usage/invites/{token}/revoke",
                                follow_redirects=False)
        assert rev.status_code == 303

    invitee_headers = {"X-Forwarded-Email": "ghost@example.com"}
    with TestClient(app_mod.app, headers=invitee_headers) as ghost:
        # Bypass no longer fires (token row is gone), so middleware
        # 403s before the route runs.  That's fine: the recipient
        # never learns whether the token ever existed, which is the
        # right info-leak posture.
        r = ghost.post(f"/invite/{token}/accept", follow_redirects=False)
        assert r.status_code in (403, 404)
