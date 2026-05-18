"""First-party marketing analytics for the public landing surface.

Captures anonymous page-view + time-on-page events from
unauthenticated visitors on the public pages (landing, /about/*,
/encircle-alternative) plus the post-auth pre-tenant /signup page.
Used to answer the operator's "where are visitors dropping off?"
question — especially "they got to /signup but didn't pick a
tenant name."

Privacy posture: no third-party shipping, ever.  The session is
identified by a first-party cookie stash sets; the events table
holds path + duration timing, never form contents.  Disclosed
on /about/privacy as "first-party page analytics for traffic
insights."

API shape:

* :func:`record_pageview` — call from the POST track endpoint
  on every page view.  Upserts the session row + appends a
  ``'pageview'`` event.
* :func:`record_leave` — call when the visitor leaves a page
  (visibilitychange / beforeunload).  Appends a ``'leave'`` event
  with the elapsed ms, updates the session's total_duration_ms.
* :func:`record_signup_visit` — convenience for stamping
  reached_signup_at when /signup renders.  Helps the funnel
  view answer "of N visitors, M reached /signup."
* :func:`record_conversion` — call from POST /signup on success
  to link the just-created tenant to its originating marketing
  session.

Operator queries:

* :func:`recent_sessions` — newest sessions for the /admin
  marketing widget.
* :func:`funnel_breakdown` — aggregate counts at each funnel
  step (landed → visited pricing → reached signup → converted).
* :func:`per_page_stats` — average time-on-page + visit count
  per public path.
"""

from __future__ import annotations

from typing import Optional

from dao._base import Actor, db, require_operator


_TRACKED_PUBLIC_PATHS = (
    "/",
    "/about",
    "/about/",  # trailing slash variant
    "/about/pricing",
    "/about/refunds",
    "/about/privacy",
    "/about/terms",
    "/about/sub-processors",
    "/about/contact",
    "/about/transparency",
    "/encircle-alternative",
    "/signup",
)


def is_tracked_path(path: str) -> bool:
    """Return True iff ``path`` is one of the public marketing
    surfaces we want to record events for.  Anything else
    (authenticated app pages, API routes, static assets) is
    silently ignored — we don't want to log every /thumbs/X.jpg
    request as a "page view"."""
    if not path:
        return False
    path = path.split("?", 1)[0].split("#", 1)[0]
    return path in _TRACKED_PUBLIC_PATHS


def _upsert_session(
    conn,
    session_id: str,
    *,
    landing_path: str,
    referrer: str,
    user_agent: str,
    viewport_w: Optional[int],
    viewport_h: Optional[int],
) -> None:
    """Insert a fresh session row OR bump last_seen_at on an
    existing one.  Landing path / referrer / UA are stamped only
    on first insert — subsequent pageviews stay attributed to the
    original entry point."""
    conn.execute(
        "INSERT INTO marketing_sessions "
        "  (session_id, landing_path, referrer, user_agent, "
        "   viewport_w, viewport_h, pageviews) "
        "VALUES (?, ?, ?, ?, ?, ?, 0) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "  last_seen_at = CURRENT_TIMESTAMP",
        (session_id, landing_path, referrer, user_agent,
         viewport_w, viewport_h),
    )


def record_pageview(
    session_id: str,
    *,
    path: str,
    referrer: str = "",
    user_agent: str = "",
    viewport_w: Optional[int] = None,
    viewport_h: Optional[int] = None,
) -> None:
    """Record a page view from an anonymous visitor.  No-op if the
    path isn't in the public-marketing tracked set (defense-in-
    depth — the route also filters, but a misconfigured client
    shouldn't be able to bloat the events table with random
    paths)."""
    if not session_id or not is_tracked_path(path):
        return
    with db() as conn:
        _upsert_session(
            conn, session_id,
            landing_path=path, referrer=referrer,
            user_agent=user_agent,
            viewport_w=viewport_w, viewport_h=viewport_h,
        )
        conn.execute(
            "UPDATE marketing_sessions "
            "SET pageviews = pageviews + 1 "
            "WHERE session_id = ?",
            (session_id,),
        )
        conn.execute(
            "INSERT INTO marketing_events "
            "  (session_id, event_type, path) "
            "VALUES (?, 'pageview', ?)",
            (session_id, path),
        )
        # If this page view IS the /signup page, stamp the
        # reached_signup_at watermark.  Funnel uses this to
        # answer "what fraction got to the tenant-name step
        # but bailed before submitting."
        if path == "/signup":
            conn.execute(
                "UPDATE marketing_sessions "
                "SET reached_signup_at = COALESCE("
                "  reached_signup_at, CURRENT_TIMESTAMP) "
                "WHERE session_id = ?",
                (session_id,),
            )
        conn.commit()


def record_leave(
    session_id: str,
    *,
    path: str,
    duration_ms: int,
) -> None:
    """Record a leave event with the elapsed ms on that page.
    Caller is the page-unload handler (visibilitychange or
    beforeunload) — sendBeacon-ready, no response needed."""
    if not session_id or not is_tracked_path(path):
        return
    duration_ms = max(0, int(duration_ms))
    # Cap absurd durations (browser tab left open for a week)
    # so a single zombie tab doesn't dominate the per-page-stats
    # averages.  30 min is generous for a real reader.
    if duration_ms > 30 * 60 * 1000:
        duration_ms = 30 * 60 * 1000
    with db() as conn:
        conn.execute(
            "INSERT INTO marketing_events "
            "  (session_id, event_type, path, duration_ms) "
            "VALUES (?, 'leave', ?, ?)",
            (session_id, path, duration_ms),
        )
        conn.execute(
            "UPDATE marketing_sessions "
            "SET total_duration_ms = total_duration_ms + ?, "
            "    last_seen_at = CURRENT_TIMESTAMP "
            "WHERE session_id = ?",
            (duration_ms, session_id),
        )
        conn.commit()


def record_conversion(
    session_id: str,
    tenant_id: int,
) -> None:
    """Stamp a marketing session as converted to a tenant.  Called
    from POST /signup on successful tenant creation, with the
    session_id read out of the stash-set cookie."""
    if not session_id or not tenant_id:
        return
    with db() as conn:
        conn.execute(
            "UPDATE marketing_sessions "
            "SET converted_at = COALESCE("
            "  converted_at, CURRENT_TIMESTAMP), "
            "    converted_tenant_id = COALESCE("
            "  converted_tenant_id, ?) "
            "WHERE session_id = ?",
            (tenant_id, session_id),
        )
        conn.commit()


# ── Operator reads ─────────────────────────────────────────────────


def recent_sessions(actor: Actor, *, limit: int = 25) -> list[dict]:
    """Newest sessions, with a count of distinct paths visited +
    the converted-tenant id (or NULL).  Powers the /admin
    marketing widget's "recent visitors" list."""
    require_operator(actor)
    with db() as conn:
        rows = conn.execute(
            "SELECT s.session_id, s.first_seen_at, s.last_seen_at, "
            "       s.landing_path, s.referrer, s.user_agent, "
            "       s.pageviews, s.total_duration_ms, "
            "       s.reached_signup_at, s.converted_at, "
            "       s.converted_tenant_id, "
            "       (SELECT COUNT(DISTINCT path) FROM marketing_events "
            "         WHERE session_id = s.session_id "
            "           AND event_type = 'pageview') "
            "         AS distinct_paths "
            "FROM marketing_sessions s "
            "ORDER BY s.first_seen_at DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def funnel_breakdown(actor: Actor, *, days: int = 30) -> dict:
    """Aggregate funnel counts over the last ``days`` days.  Each
    step is a strict superset of the next — a visitor who
    converted must have reached signup, must have landed.

    Returns ``{landed, visited_pricing, reached_signup, converted}``
    so the template can render the drop-off rates without doing
    its own math."""
    require_operator(actor)
    with db() as conn:
        # Total sessions in the window.
        landed = conn.execute(
            "SELECT COUNT(*) AS n FROM marketing_sessions "
            "WHERE first_seen_at >= datetime('now', ?) ",
            (f"-{int(days)} days",),
        ).fetchone()["n"]
        # Visitors who hit /about/pricing at least once.
        visited_pricing = conn.execute(
            "SELECT COUNT(DISTINCT s.session_id) AS n "
            "FROM marketing_sessions s "
            "JOIN marketing_events e ON e.session_id = s.session_id "
            "WHERE s.first_seen_at >= datetime('now', ?) "
            "  AND e.event_type = 'pageview' "
            "  AND e.path = '/about/pricing'",
            (f"-{int(days)} days",),
        ).fetchone()["n"]
        # Visitors who reached /signup (post-auth, pre-tenant).
        reached_signup = conn.execute(
            "SELECT COUNT(*) AS n FROM marketing_sessions "
            "WHERE first_seen_at >= datetime('now', ?) "
            "  AND reached_signup_at IS NOT NULL",
            (f"-{int(days)} days",),
        ).fetchone()["n"]
        # Visitors who actually created a tenant.
        converted = conn.execute(
            "SELECT COUNT(*) AS n FROM marketing_sessions "
            "WHERE first_seen_at >= datetime('now', ?) "
            "  AND converted_at IS NOT NULL",
            (f"-{int(days)} days",),
        ).fetchone()["n"]
    return {
        "days": days,
        "landed": landed,
        "visited_pricing": visited_pricing,
        "reached_signup": reached_signup,
        "converted": converted,
        # The drop-off question: of N who reached the tenant-name
        # step, how many bailed without submitting?  This is the
        # "ad campaign click → bounced at signup" lever the
        # operator most wants to see (#feedback driving this
        # whole table).
        "signup_dropoff": max(0, reached_signup - converted),
    }


def per_page_stats(actor: Actor, *, days: int = 30) -> list[dict]:
    """Per-path: total views, distinct visitors, average dwell
    time (ms).  Sorted by views desc so the /admin table reads
    top-to-bottom by interest."""
    require_operator(actor)
    with db() as conn:
        rows = conn.execute(
            "SELECT e.path, "
            "       COUNT(*) FILTER ("
            "         WHERE e.event_type = 'pageview') AS views, "
            "       COUNT(DISTINCT e.session_id) AS distinct_visitors, "
            "       AVG(e.duration_ms) FILTER ("
            "         WHERE e.event_type = 'leave' "
            "           AND e.duration_ms IS NOT NULL) "
            "         AS avg_duration_ms "
            "FROM marketing_events e "
            "WHERE e.created_at >= datetime('now', ?) "
            "GROUP BY e.path "
            "ORDER BY views DESC",
            (f"-{int(days)} days",),
        ).fetchall()
    return [dict(r) for r in rows]
