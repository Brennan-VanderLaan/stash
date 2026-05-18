"""Public-leaderboard handles — opt-in display names tied to
``feedback.actor_email``.  Privacy bug feedback #30: the
original /leaderboard rendered the local-part of the email,
which is a real disclosure of "who's signed up for this stash"
to anyone who reaches the page.  Handles fix that.
"""

from __future__ import annotations

import pytest


def _seed_feedback(client, *, body: str, status: str, actor_email: str):
    with client.app_module.db() as conn:
        conn.execute(
            "INSERT INTO feedback (tenant_id, actor_email, body, status) "
            "VALUES (?, ?, ?, ?)",
            (client.test_tenant_id, actor_email, body, status),
        )
        conn.commit()


# ── DAO validation ──────────────────────────────────────────────────


def test_set_handle_happy_path(client):
    from dao import Actor, handles as dao_handles
    a = Actor(email=client.test_email, tenant_id=client.test_tenant_id,
              role="maintainer", is_operator=False,
              memberships=((client.test_tenant_id, "maintainer"),))
    row = dao_handles.set_handle(a, "card_wrangler")
    assert row["handle"] == "card_wrangler"
    assert row["handle_lower"] == "card_wrangler"
    assert row["revoked_at"] is None


def test_set_handle_preserves_case_but_unique_case_insensitive(client):
    from dao import Actor, handles as dao_handles
    a = Actor(email="a@example.com", tenant_id=client.test_tenant_id,
              role="maintainer", is_operator=False, memberships=())
    b = Actor(email="b@example.com", tenant_id=client.test_tenant_id,
              role="maintainer", is_operator=False, memberships=())
    dao_handles.set_handle(a, "Brennan_V")
    with pytest.raises(dao_handles.HandleError) as exc:
        dao_handles.set_handle(b, "brennan_v")
    assert "taken" in exc.value.reason.lower()


@pytest.mark.parametrize("bad,reason_keyword", [
    ("",         "blank"),
    ("a",        "2 characters"),
    ("a" * 25,   "24 characters"),
    ("-leading", "letters"),
    ("_leading", "letters"),
    ("has spaces", "letters"),
    ("has.dot",  "letters"),
    ("emoji😿",  "letters"),
])
def test_set_handle_rejects_bad_shapes(client, bad, reason_keyword):
    from dao import Actor, handles as dao_handles
    a = Actor(email="a@example.com", tenant_id=client.test_tenant_id,
              role="maintainer", is_operator=False, memberships=())
    with pytest.raises(dao_handles.HandleError) as exc:
        dao_handles.set_handle(a, bad)
    assert reason_keyword.lower() in exc.value.reason.lower()


def test_active_handle_returns_none_for_revoked(client):
    from dao import Actor, handles as dao_handles
    user = Actor(email="u@example.com", tenant_id=client.test_tenant_id,
                 role="maintainer", is_operator=False, memberships=())
    op = Actor(email="op@example.com", tenant_id=None, role=None,
               is_operator=True, memberships=())
    dao_handles.set_handle(user, "good_one")
    assert dao_handles.active_handle("u@example.com") == "good_one"
    dao_handles.revoke_handle(op, "u@example.com", reason="test")
    assert dao_handles.active_handle("u@example.com") is None


def test_user_can_pick_new_handle_after_revocation(client):
    """Revoke isn't a permanent ban — the user picks a new
    acceptable handle and shows up by name again.  Existing
    revoke row stays for the audit trail; the upsert clears the
    revoke columns on the active row."""
    from dao import Actor, handles as dao_handles
    user = Actor(email="u@example.com", tenant_id=client.test_tenant_id,
                 role="maintainer", is_operator=False, memberships=())
    op = Actor(email="op@example.com", tenant_id=None, role=None,
               is_operator=True, memberships=())
    dao_handles.set_handle(user, "first_pick")
    dao_handles.revoke_handle(op, "u@example.com", reason="test")
    dao_handles.set_handle(user, "second_pick")
    row = dao_handles.get_handle("u@example.com")
    assert row["handle"] == "second_pick"
    assert row["revoked_at"] is None


# ── Route + render ──────────────────────────────────────────────────


def test_leaderboard_renders_anonymous_when_no_handle(client):
    """The privacy bug fix: with no handles set, the public
    podium reads as 'Anonymous' — never the email local-part."""
    _seed_feedback(
        client, body="x", status="done",
        actor_email="someone.specific@example.com",
    )
    page = client.get("/leaderboard").text
    # The email local-part must not leak.
    assert "someone.specific" not in page
    # And the page DOES surface Anonymous.
    assert "Anonymous" in page


def test_leaderboard_renders_handle_when_set(client):
    """With a handle set, the public podium shows the handle."""
    from dao import Actor, handles as dao_handles
    actor = Actor(email="contrib@example.com",
                  tenant_id=client.test_tenant_id,
                  role="maintainer", is_operator=False,
                  memberships=((client.test_tenant_id, "maintainer"),))
    dao_handles.set_handle(actor, "shipbuilder")
    _seed_feedback(
        client, body="x", status="done",
        actor_email="contrib@example.com",
    )
    page = client.get("/leaderboard").text
    assert "shipbuilder" in page
    assert "contrib@example.com" not in page


def test_set_handle_route_round_trips_validation_error(client):
    """A POST with an invalid handle redirects back with the
    error reason in the query string so the form can echo it."""
    r = client.post(
        "/usage/handle", data={"handle": "-leading"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "handle_error=" in r.headers["location"]
    assert "letters" in r.headers["location"]


def test_set_handle_route_happy_path(client):
    """Valid handle round-trips to /usage with a success flag."""
    r = client.post(
        "/usage/handle", data={"handle": "valid_pick"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "handle_set=1" in r.headers["location"]
    # Confirm DAO state.
    from dao import handles as dao_handles
    assert dao_handles.active_handle(client.test_email) == "valid_pick"


# ── Real-time availability probe (#67) ─────────────────────────────


def test_check_availability_dao_returns_ok_for_unused(client):
    """Unused handle → available=True."""
    from dao import Actor, handles as dao_handles
    a = Actor(email=client.test_email, tenant_id=client.test_tenant_id,
              role="maintainer", is_operator=False, memberships=())
    r = dao_handles.check_availability(a, "fresh_name")
    assert r == {"available": True, "reason": ""}


def test_check_availability_dao_flags_taken(client):
    """Handle taken by another email → available=False with reason."""
    from dao import Actor, handles as dao_handles
    other = Actor(email="someone-else@example.com",
                  tenant_id=client.test_tenant_id,
                  role="maintainer", is_operator=False, memberships=())
    dao_handles.set_handle(other, "first_in")
    me = Actor(email=client.test_email, tenant_id=client.test_tenant_id,
               role="maintainer", is_operator=False, memberships=())
    r = dao_handles.check_availability(me, "first_in")
    assert r["available"] is False
    assert "taken" in r["reason"].lower()


def test_check_availability_dao_lets_owner_reclaim(client):
    """Re-typing your own current handle counts as available (the
    POST upsert is idempotent — don't warn the user about
    themselves)."""
    from dao import Actor, handles as dao_handles
    a = Actor(email=client.test_email, tenant_id=client.test_tenant_id,
              role="maintainer", is_operator=False, memberships=())
    dao_handles.set_handle(a, "my_handle")
    r = dao_handles.check_availability(a, "my_handle")
    assert r["available"] is True


def test_check_availability_dao_validates_shape(client):
    """Invalid shape (whitespace, too short, etc.) comes back as
    available=False with the validator's reason — same surface
    as a uniqueness conflict so the JS can show one inline
    message either way."""
    from dao import Actor, handles as dao_handles
    a = Actor(email=client.test_email, tenant_id=client.test_tenant_id,
              role="maintainer", is_operator=False, memberships=())
    r = dao_handles.check_availability(a, "x")
    assert r["available"] is False
    assert "2 characters" in r["reason"]


def test_check_availability_route_returns_json(client):
    """GET /usage/handle/check?handle=… returns the same shape
    the DAO does."""
    r = client.get("/usage/handle/check?handle=brand_new")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["reason"] == ""


def test_check_availability_route_flags_taken_for_other(client):
    """End-to-end: someone else has it, we get available=False."""
    from dao import Actor, handles as dao_handles
    other = Actor(email="other@example.com",
                  tenant_id=client.test_tenant_id,
                  role="maintainer", is_operator=False, memberships=())
    dao_handles.set_handle(other, "owned_already")
    r = client.get("/usage/handle/check?handle=owned_already")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert "taken" in body["reason"].lower()
