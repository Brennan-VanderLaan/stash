"""First-run onboarding tours — per-feature seen flags, version
bumps, replay reset, JSON state endpoint, and the auto-fire
catalogue on each registered page prefix.

The actual overlay UI is JS-driven (see base.html); these tests
cover the DAO + endpoint surface so the JS layer has a stable
contract to lean on.
"""

from __future__ import annotations

import json


def test_tour_state_unseen_returns_every_tour_as_false(client):
    r = client.get("/api/v1/tour/state",
                   headers={"Accept": "application/json"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "welcome" in body["seen"]
    # Fresh tenant → nothing seen yet.
    assert body["seen"]["welcome"] is False


def test_tour_state_auto_play_matches_current_path(client):
    """``auto_play`` carries the tours that should fire on this
    page load — filtered by URL prefix + the user's seen state."""
    r = client.get("/api/v1/tour/state?path=/home",
                   headers={"Accept": "application/json"})
    auto = {t["feature"] for t in r.json()["auto_play"]}
    assert "welcome" in auto
    # Box-detail tour shouldn't fire on the home page.
    assert "box_detail" not in auto


def test_tour_state_box_detail_only_on_box_page(client):
    """The box_detail tour fires only on a /boxes/{id} URL."""
    r = client.get("/api/v1/tour/state?path=/boxes/42",
                   headers={"Accept": "application/json"})
    auto = {t["feature"] for t in r.json()["auto_play"]}
    assert "box_detail" in auto
    assert "welcome" not in auto


def test_tour_mark_seen_dropping_from_auto_play(client):
    """After marking seen, the same tour no longer appears in
    ``auto_play`` for the same user."""
    client.post("/tour/welcome/seen",
                headers={"Accept": "application/json"})
    r = client.get("/api/v1/tour/state?path=/home",
                   headers={"Accept": "application/json"})
    auto = {t["feature"] for t in r.json()["auto_play"]}
    assert "welcome" not in auto
    assert r.json()["seen"]["welcome"] is True


def test_tour_reset_brings_back_auto_play(client):
    """Reset clears the seen row so the tour fires again on next load."""
    client.post("/tour/welcome/seen",
                headers={"Accept": "application/json"})
    r = client.post("/tour/welcome/reset", follow_redirects=False)
    assert r.status_code == 303
    follow = client.get("/api/v1/tour/state?path=/home",
                        headers={"Accept": "application/json"})
    auto = {t["feature"] for t in follow.json()["auto_play"]}
    assert "welcome" in auto


def test_tour_reset_all_clears_every_seen_record(client):
    """``/tour/reset-all`` empties the user's tour_seen rows."""
    client.post("/tour/welcome/seen",
                headers={"Accept": "application/json"})
    client.post("/tour/box_detail/seen",
                headers={"Accept": "application/json"})
    r = client.post("/tour/reset-all", follow_redirects=False)
    assert r.status_code == 303
    state = client.get("/api/v1/tour/state",
                       headers={"Accept": "application/json"}).json()
    assert state["seen"]["welcome"] is False
    assert state["seen"]["box_detail"] is False


def test_tour_version_bump_forces_re_show(client, monkeypatch):
    """Operator bumps a tour version → the user's old seen-row
    falls below the registered version and the tour re-fires."""
    # Mark seen at v1.
    client.post("/tour/welcome/seen",
                headers={"Accept": "application/json"})
    # Bump the registered version.
    import dao.tours as dao_tours
    original = dao_tours.TOURS
    bumped = []
    for t in original:
        copy = dict(t)
        if copy["feature"] == "welcome":
            copy = {**copy, "version": copy["version"] + 1}
        bumped.append(copy)
    monkeypatch.setattr(dao_tours, "TOURS", bumped)
    monkeypatch.setattr(
        dao_tours, "_TOURS_BY_FEATURE",
        {t["feature"]: t for t in bumped},
    )
    state = client.get("/api/v1/tour/state?path=/home",
                       headers={"Accept": "application/json"}).json()
    # The user's seen-row (v1) is below the now-current version (v2),
    # so the tour treats them as unseen.
    assert state["seen"]["welcome"] is False
    auto = {t["feature"] for t in state["auto_play"]}
    assert "welcome" in auto


def test_tour_get_returns_steps(client):
    """``GET /api/v1/tour/{feature}`` returns the full step list
    for the replay-from-/usage path."""
    r = client.get("/api/v1/tour/welcome",
                   headers={"Accept": "application/json"})
    assert r.status_code == 200
    body = r.json()
    assert body["feature"] == "welcome"
    assert isinstance(body["steps"], list)
    assert len(body["steps"]) > 0


def test_tour_get_404_for_unknown_feature(client):
    r = client.get("/api/v1/tour/bogus",
                   headers={"Accept": "application/json"})
    assert r.status_code == 404


def test_usage_renders_tour_catalogue(client):
    page = client.get("/usage").text
    assert "Onboarding tours" in page
    # Replay buttons navigate to the tour's home page with a
    # ``?tour=<feature>`` query param; the overlay JS auto-plays
    # on that page.  Earlier passes had a JS-only data-tour-replay
    # trigger that fired the tour in-place on /usage, where every
    # target selector missed because the elements were on other
    # pages — feedback #7.
    assert 'href="/home?tour=welcome"' in page
    assert 'href="/boxes/?tour=box_detail"' in page
    assert 'href="/labels?tour=labels"' in page


def test_tour_overlay_renders_for_authed_user(client):
    """The overlay div + JS land on every authed page via base.html."""
    page = client.get("/home").text
    assert 'id="tour-overlay"' in page
    assert 'startTour' in page  # the replay-from-/usage hook


def test_tour_seen_endpoint_anonymous_actor_403(client):
    """The endpoint requires a real user email; bearer tokens carry
    a synthetic ``api_token:N`` actor and 403 here."""
    from dao import api_tokens, Actor
    actor = Actor(
        email="test@example.com", tenant_id=client.test_tenant_id,
        role="maintainer", is_operator=False,
        memberships=((client.test_tenant_id, "maintainer"),),
        shares=(),
    )
    token = api_tokens.create(
        actor, name="t", role="maintainer",
    )["plaintext"]
    r = client.post(
        "/tour/welcome/seen",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    assert r.status_code == 403
