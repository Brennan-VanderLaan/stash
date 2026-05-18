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


def _track(client, payload):
    """POST a marketing event.  Returns the raw response."""
    return client.post(
        "/marketing/track",
        content=json.dumps(payload),
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


def test_track_endpoint_records_pageview(client):
    r = _track(client, {
        "event": "pageview",
        "session_id": "ep-1",
        "path": "/",
        "referrer": "https://news.example",
        "viewport_w": 1200, "viewport_h": 800,
    })
    assert r.status_code == 204
    with client.app_module.db() as conn:
        sess = conn.execute(
            "SELECT * FROM marketing_sessions WHERE session_id = ?",
            ("ep-1",),
        ).fetchone()
    assert sess is not None
    assert sess["landing_path"] == "/"


def test_track_endpoint_drops_garbage_payloads(client):
    """Bad JSON, missing fields, unknown events — all silently 204
    so a flaky client doesn't surface as a console error on the
    marketing page."""
    r = _track(client, {})
    assert r.status_code == 204
    r = _track(client, {"event": "pageview"})  # missing session_id + path
    assert r.status_code == 204
    r = _track(client, {
        "event": "bogus", "session_id": "x", "path": "/",
    })
    assert r.status_code == 204
    with client.app_module.db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM marketing_events",
        ).fetchone()["n"]
    assert n == 0


def test_track_endpoint_bypasses_auth(client):
    """The bypass list lets anonymous visitors POST to /marketing/track
    without going through Google sign-in.  Hit the endpoint with no
    auth context (TestClient default) and expect 204, not 401/403."""
    r = client.post(
        "/marketing/track",
        content=json.dumps({
            "event": "pageview", "session_id": "anon-1", "path": "/",
        }),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 204


# ── Signup → conversion link ───────────────────────────────────────


def test_signup_success_stamps_marketing_conversion(client):
    """When the user submits the tenant-name form on /signup and a
    stash_mkt cookie is present, the marketing session gets
    converted_at + converted_tenant_id stamped."""
    # Pre-create the marketing session as if the visitor had
    # landed on / and clicked through to /signup.
    from dao import marketing as dao_marketing
    dao_marketing.record_pageview("signup-sess", path="/")
    dao_marketing.record_pageview("signup-sess", path="/signup")
    # Use a fresh email that doesn't yet have a tenant.  The
    # test client fixture already has a tenant for client.test_email
    # — for this test we want the actor to be tenantless so the
    # signup path is allowed.  Easiest: signup with a different
    # email via X-Forwarded-Email header.
    r = client.post(
        "/signup",
        data={"tenant_name": "Brand New Tenant"},
        headers={
            "X-Forwarded-Email": "newbie@example.com",
            "Cookie": "stash_mkt=signup-sess",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    with client.app_module.db() as conn:
        sess = conn.execute(
            "SELECT converted_at, converted_tenant_id "
            "FROM marketing_sessions WHERE session_id = ?",
            ("signup-sess",),
        ).fetchone()
    assert sess["converted_at"] is not None
    assert sess["converted_tenant_id"] is not None


def test_signup_without_marketing_cookie_still_succeeds(client):
    """A visitor who declined the analytics cookie should still be
    able to sign up — the conversion stamp is best-effort, not a
    gate."""
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
