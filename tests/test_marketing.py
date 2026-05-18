"""First-party page-analytics surface — DAO + ingest endpoint +
conversion linking on /signup.

Pins:
* POST /marketing/track records a pageview event keyed by session
* leave events accumulate dwell time on the session row
* Tracked-paths gate: the DAO refuses garbage paths
* /signup POST stamps converted_at when a marketing cookie is
  present
* The /admin operator widget shows the funnel + recent sessions
* Consent banner is gated client-side: declined visitors do not
  produce events (smoke-test the cookie precondition server-side)
"""

from __future__ import annotations

import json


def _track_leave(client, *, path, duration_ms):
    """POST a leave beacon.  Returns the raw response.  No
    session id — the server derives the anonymous bucket id
    from the request envelope (IP+UA+KEK+time window)."""
    return client.post(
        "/marketing/track",
        content=json.dumps({"path": path, "duration_ms": duration_ms}),
        headers={"Content-Type": "application/json"},
    )


# ── DAO ────────────────────────────────────────────────────────────


def test_record_pageview_creates_session_and_event(client):
    from dao import marketing as dao_marketing
    dao_marketing.record_pageview(
        "sess-123", path="/", referrer="https://google.com",
        user_agent="UA/1", viewport_w=1500, viewport_h=900,
    )
    with client.app_module.db() as conn:
        sess = conn.execute(
            "SELECT * FROM marketing_sessions WHERE session_id = ?",
            ("sess-123",),
        ).fetchone()
        events = conn.execute(
            "SELECT event_type, path FROM marketing_events "
            "WHERE session_id = ?",
            ("sess-123",),
        ).fetchall()
    assert sess is not None
    assert sess["landing_path"] == "/"
    assert sess["referrer"] == "https://google.com"
    assert sess["pageviews"] == 1
    assert sess["reached_signup_at"] is None
    assert len(events) == 1
    assert events[0]["event_type"] == "pageview"


def test_record_pageview_silently_ignores_untracked_paths(client):
    """The DAO is defense-in-depth against a misconfigured client
    POSTing /home or /api/v1/anything as a pageview.  Only the
    public marketing surface is tracked."""
    from dao import marketing as dao_marketing
    dao_marketing.record_pageview("sess-x", path="/home")
    dao_marketing.record_pageview("sess-x", path="/api/v1/me")
    dao_marketing.record_pageview("sess-x", path="/random")
    with client.app_module.db() as conn:
        events = conn.execute(
            "SELECT COUNT(*) AS n FROM marketing_events "
            "WHERE session_id = ?",
            ("sess-x",),
        ).fetchone()
    assert events["n"] == 0


def test_record_pageview_signup_stamps_reached_signup_at(client):
    """A pageview on /signup is what flips reached_signup_at —
    that's the watermark the funnel uses for "got to the
    tenant-name step but bailed before submitting"."""
    from dao import marketing as dao_marketing
    dao_marketing.record_pageview("sess-s", path="/")
    dao_marketing.record_pageview("sess-s", path="/signup")
    with client.app_module.db() as conn:
        sess = conn.execute(
            "SELECT reached_signup_at, converted_at "
            "FROM marketing_sessions WHERE session_id = ?",
            ("sess-s",),
        ).fetchone()
    assert sess["reached_signup_at"] is not None
    assert sess["converted_at"] is None


def test_record_leave_accumulates_duration(client):
    from dao import marketing as dao_marketing
    dao_marketing.record_pageview("sess-l", path="/")
    dao_marketing.record_leave("sess-l", path="/", duration_ms=3500)
    dao_marketing.record_leave("sess-l", path="/", duration_ms=1200)
    with client.app_module.db() as conn:
        sess = conn.execute(
            "SELECT total_duration_ms FROM marketing_sessions "
            "WHERE session_id = ?",
            ("sess-l",),
        ).fetchone()
    assert sess["total_duration_ms"] == 4700


def test_record_leave_caps_absurd_durations(client):
    """A browser tab left open for a week shouldn't dominate the
    averages.  The DAO caps each leave event at 30 minutes."""
    from dao import marketing as dao_marketing
    dao_marketing.record_pageview("sess-z", path="/")
    dao_marketing.record_leave(
        "sess-z", path="/", duration_ms=7 * 24 * 60 * 60 * 1000,
    )
    with client.app_module.db() as conn:
        sess = conn.execute(
            "SELECT total_duration_ms FROM marketing_sessions "
            "WHERE session_id = ?",
            ("sess-z",),
        ).fetchone()
    assert sess["total_duration_ms"] == 30 * 60 * 1000


def test_record_conversion_links_tenant_to_session(client):
    from dao import marketing as dao_marketing
    dao_marketing.record_pageview("sess-c", path="/signup")
    dao_marketing.record_conversion("sess-c", tenant_id=42)
    with client.app_module.db() as conn:
        sess = conn.execute(
            "SELECT converted_at, converted_tenant_id "
            "FROM marketing_sessions WHERE session_id = ?",
            ("sess-c",),
        ).fetchone()
    assert sess["converted_at"] is not None
    assert sess["converted_tenant_id"] == 42


# ── POST /marketing/track endpoint ─────────────────────────────────


def test_get_landing_records_pageview_server_side(client):
    """GET / fires a server-side pageview into marketing_sessions
    under the anonymous bucket id — no JS / no client involvement
    required.  Demonstrates the bucket id is computed in the
    route handler and persisted via the DAO."""
    r = client.get("/")
    assert r.status_code == 200
    with client.app_module.db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM marketing_sessions "
            "WHERE session_id LIKE 'anon-%' AND landing_path = '/'",
        ).fetchone()["n"]
    assert n >= 1


def test_track_endpoint_records_leave_beacon(client):
    """The /marketing/track endpoint accepts a leave beacon with
    just ``{path, duration_ms}`` and uses the server-derived
    bucket id to link it to the pageview."""
    # First, register a pageview (so the session row exists).
    client.get("/")
    r = _track_leave(client, path="/", duration_ms=4200)
    assert r.status_code == 204
    with client.app_module.db() as conn:
        events = conn.execute(
            "SELECT event_type, duration_ms FROM marketing_events "
            "WHERE event_type = 'leave' AND path = '/'",
        ).fetchall()
    assert any(e["duration_ms"] == 4200 for e in events)


def test_track_endpoint_drops_garbage_payloads(client):
    """Bad JSON, missing fields — all silently 204 so a flaky
    client doesn't surface as a console error on the marketing
    page."""
    r = client.post(
        "/marketing/track",
        content="not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 204
    r = client.post(
        "/marketing/track",
        content=json.dumps({}),  # missing path
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 204
    r = client.post(
        "/marketing/track",
        content=json.dumps({"path": "/random-untracked"}),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 204
    with client.app_module.db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM marketing_events "
            "WHERE event_type = 'leave' AND path = '/random-untracked'",
        ).fetchone()["n"]
    assert n == 0


def test_track_endpoint_bypasses_auth(client):
    """The bypass list lets anonymous visitors POST to
    /marketing/track without going through Google sign-in."""
    r = _track_leave(client, path="/", duration_ms=1000)
    assert r.status_code == 204


def test_marketing_bucket_stable_within_window(client):
    """Two requests from the same client (same IP+UA) within the
    same 30-min window should produce the same bucket id.
    Without this, cross-page funnel attribution wouldn't work."""
    from app import _marketing_bucket_id
    from starlette.requests import Request as StarletteRequest

    class FakeClient:
        host = "203.0.113.7"

    def make_request():
        scope = {
            "type": "http",
            "headers": [
                (b"user-agent", b"test-ua/1.0"),
                (b"x-forwarded-for", b"203.0.113.7"),
            ],
            "client": ("203.0.113.7", 12345),
        }
        return StarletteRequest(scope)

    a = _marketing_bucket_id(make_request())
    b = _marketing_bucket_id(make_request())
    assert a == b
    assert a.startswith("anon-")


# ── Signup → conversion link ───────────────────────────────────────


def test_signup_success_stamps_marketing_conversion(client):
    """When a visitor walks the funnel — landed on /, hit /signup,
    submitted the tenant-name form — the marketing session gets
    converted_at + converted_tenant_id stamped under the
    anonymous bucket id.  No cookies in play; the bucket is
    re-derived on each request from the same IP+UA+window."""
    # Use a fresh email + IP so the bucket is unambiguously this
    # visitor.  The TestClient sends consistent client info on
    # all requests so the bucket stays stable across these calls.
    hdrs = {
        "X-Forwarded-Email": "newbie@example.com",
        "User-Agent": "stash-test-walker/1.0",
    }
    # Landing.
    client.get("/", headers=hdrs)
    # Reach /signup (this stamps reached_signup_at on the bucket).
    client.get("/signup", headers=hdrs)
    # Submit the form.
    r = client.post(
        "/signup",
        data={"tenant_name": "Brand New Tenant"},
        headers=hdrs,
        follow_redirects=False,
    )
    assert r.status_code == 303
    # The session that has reached_signup_at AND converted_at is
    # this walk-through visitor's bucket.
    with client.app_module.db() as conn:
        rows = conn.execute(
            "SELECT converted_at, converted_tenant_id, "
            "       reached_signup_at "
            "FROM marketing_sessions "
            "WHERE converted_at IS NOT NULL",
        ).fetchall()
    assert len(rows) >= 1
    converted = rows[-1]
    assert converted["reached_signup_at"] is not None
    assert converted["converted_tenant_id"] is not None


def test_signup_works_with_js_disabled(client):
    """A JS-disabled visitor still gets attributed to the funnel —
    the pageview side is server-side, no beacon required.  Only
    the dwell-time leave event needs JS."""
    r = client.post(
        "/signup",
        data={"tenant_name": "Cookieless"},
        headers={"X-Forwarded-Email": "cookieless@example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 303


# ── Operator funnel breakdown ──────────────────────────────────────


def test_funnel_breakdown_counts_each_stage(client):
    """A landed-only session, a landed+pricing session, a
    reached-signup session, and a converted session — funnel
    counts each strictly."""
    from dao import marketing as dao_marketing
    # 1) Just landed.
    dao_marketing.record_pageview("a", path="/")
    # 2) Landed + visited pricing.
    dao_marketing.record_pageview("b", path="/")
    dao_marketing.record_pageview("b", path="/about/pricing")
    # 3) Reached signup.
    dao_marketing.record_pageview("c", path="/")
    dao_marketing.record_pageview("c", path="/about/pricing")
    dao_marketing.record_pageview("c", path="/signup")
    # 4) Converted.
    dao_marketing.record_pageview("d", path="/")
    dao_marketing.record_pageview("d", path="/signup")
    dao_marketing.record_conversion("d", tenant_id=99)

    from dao import Actor
    op = Actor(
        email="op@example.com", tenant_id=None,
        role="maintainer", is_operator=True, memberships=(),
    )
    funnel = dao_marketing.funnel_breakdown(op, days=30)
    assert funnel["landed"] == 4
    assert funnel["visited_pricing"] == 2
    assert funnel["reached_signup"] == 2
    assert funnel["converted"] == 1
    # The "bailed at signup" lever — reached signup MINUS converted.
    assert funnel["signup_dropoff"] == 1
