"""First-run onboarding tour state.

Each "feature" is a discrete tour the user sees once.  When the
operator wants every user to re-see the updated copy (because the
feature changed significantly), they bump the ``version`` in
``TOURS`` below — rows in ``tour_seen`` whose version is below
the registered version are treated as "not seen", which forces a
re-show on next page load.

Tour definitions live in this module (Python source) rather than
the database because they're code-adjacent: the step ``target``
selectors point at specific elements on specific pages, so editing
copy + selectors should ride alongside the template changes that
introduced them.

State lives per *user* (by email) rather than per-tenant because
a tour is a UX preference of the human; switching tenants
shouldn't reset the lifetime of "I've seen this introduction".
"""

from __future__ import annotations

import obs
from dao._base import db


_log = obs.get_logger("dao.tours")


# ── Tour catalogue ──────────────────────────────────────────────────


# Each entry:
#   page:    URL prefix (or "/" for the home page) on which the tour
#            should fire automatically.  The JS layer matches the
#            current path against this prefix.
#   version: bump to force a re-show after a major change.
#   title:   plain-text label rendered in the /usage tour list.
#   steps:   ordered list of overlay steps.  ``target`` is a CSS
#            selector (or null for a centred modal); ``title`` +
#            ``body`` render inside the tooltip; ``position`` hints
#            where the tooltip sits relative to ``target``.
TOURS: list[dict] = [
    {
        "feature": "welcome",
        "page": "/home",
        "version": 1,
        "title": "Welcome to Stash",
        "steps": [
            {
                "target": None,
                "title": "Welcome.",
                "body": (
                    "Stash is your household inventory: take a photo, "
                    "we sort it into boxes, you find anything later "
                    "with one search.  Let's take 30 seconds to show "
                    "you around."
                ),
            },
            {
                "target": ".box-list, .box-group, .empty",
                "title": "Your boxes live here",
                "body": (
                    "Each box holds the actual things in a real "
                    "container.  Boxes are grouped by Room and "
                    "Location, the way they live in your house."
                ),
                "position": "below",
            },
            {
                # Desktop: top-nav Ingest link.  Mobile: the same
                # link in the always-visible bottom tab-bar (the
                # ``.tab-bar a[href='/ingest']`` selector matches
                # both because both nav containers carry that
                # anchor in the rendered DOM — the visibility
                # filter in the overlay JS picks whichever is
                # currently on-screen).
                "target": ".tab-bar a[href='/ingest'], a[href='/ingest']",
                "title": "Add stuff via a photo",
                "body": (
                    "Tap Ingest to snap a photo of a pile or a single "
                    "thing.  The AI breaks it into individual items "
                    "and drops them into the sort queue."
                ),
                "position": "above",
            },
            {
                # /queue lives in the desktop top-nav.  On mobile it
                # lives behind the More tab in a hidden bottom sheet,
                # so we point at the More button itself with a hint
                # in the copy.
                "target": "header a[href='/queue'], #more-tab",
                "title": "Sort queue",
                "body": (
                    "Items waiting to be filed land here.  The AI "
                    "suggests a box for each; one tap accepts.  "
                    "On mobile, tap More → Sort queue."
                ),
                "position": "above",
            },
            {
                "target": "header a[href='/labels'], #more-tab",
                "title": "Print labels",
                "body": (
                    "Each box gets a QR-coded label.  Scan with your "
                    "phone to jump straight to its contents.  "
                    "On mobile, tap More → Labels."
                ),
                "position": "above",
            },
            {
                "target": "#feedback-launcher",
                "title": "Found something to fix?",
                "body": (
                    "Tap the Feedback button anytime.  Add an optional "
                    "screenshot and we'll see it on our triage queue."
                ),
                "position": "left",
            },
        ],
    },
    {
        "feature": "box_detail",
        "page": "/boxes/",
        "version": 1,
        "title": "Working with a box",
        "steps": [
            {
                "target": ".item-grid, .box-card-body, .card-title",
                "title": "Box contents",
                "body": (
                    "Every item in the box lives here.  Tap an item "
                    "tile to see details + edit tags + move it to "
                    "another box."
                ),
                "position": "below",
            },
            {
                "target": ".bulk-tag-disclosure, .box-edit-cta",
                "title": "Tag everything in this box",
                "body": (
                    "Want every item to share a tag (e.g. 'kitchen' "
                    "or 'fragile')?  Tap '+ Tag all' instead of "
                    "tagging each one separately."
                ),
                "position": "below",
            },
            {
                "target": "a[href*='/audit']",
                "title": "Audit",
                "body": (
                    "Opening the box and want to confirm everything "
                    "is still there?  Audit walks you through every "
                    "item — swipe right if it's in the box, left if "
                    "it's missing.  Missing items move to the sort "
                    "queue with provenance."
                ),
                "position": "below",
            },
        ],
    },
    {
        "feature": "floors",
        "page": "/locations/",
        "version": 1,
        "title": "Floors & rooms",
        "steps": [
            {
                "target": None,
                "title": "This is a floor of one of your locations.",
                "body": (
                    "Locations contain floors; floors contain rooms; "
                    "rooms contain boxes.  This page is where you "
                    "draw the floor's layout — drag the floorplan "
                    "to mark out each room, then assign boxes to a "
                    "room from the box detail page."
                ),
            },
            {
                "target": "#floor-actions, .floor-actions-summary",
                "title": "Manage this floor",
                "body": (
                    "Tap this to rename the floor, swap the "
                    "floorplan image, or delete the floor (along "
                    "with every room on it)."
                ),
                "position": "above",
            },
        ],
    },
    {
        "feature": "labels",
        "page": "/labels",
        "version": 1,
        "title": "Printing labels",
        "steps": [
            {
                "target": ".label-format-form",
                "title": "Pick your Avery sheet",
                "body": (
                    "Stash prints to off-the-shelf Avery shipping "
                    "labels.  5523 (10/sheet) is the default — drop "
                    "it in your printer, hit Print, done."
                ),
                "position": "below",
            },
            {
                "target": ".label-group",
                "title": "Boxes group by location",
                "body": (
                    "Labels are bucketed by where the box lives so "
                    "a print job for one room is two taps."
                ),
                "position": "below",
            },
            {
                "target": "#copies-select",
                "title": "Print copies for big boxes",
                "body": (
                    "Wrap a label around all four sides of a box?  "
                    "Bump Copies to 4 — every selected label prints "
                    "that many times."
                ),
                "position": "above",
            },
        ],
    },
]


# Fast lookup by feature id.
_TOURS_BY_FEATURE: dict[str, dict] = {t["feature"]: t for t in TOURS}


# ── State queries ───────────────────────────────────────────────────


def state_for_actor(actor_email: str | None) -> dict[str, bool]:
    """Map of feature → bool indicating whether the user has seen
    the *current* version of each tour.  Anonymous users (no email)
    get "seen=False" for everything so the tour can still fire on
    a public-share page if we ever expose one there."""
    seen: dict[str, bool] = {t["feature"]: False for t in TOURS}
    if not actor_email:
        return seen
    with db() as conn:
        rows = conn.execute(
            "SELECT feature, version FROM tour_seen WHERE actor_email = ?",
            (actor_email,),
        ).fetchall()
    by_feature = {r["feature"]: r["version"] for r in rows}
    for tour in TOURS:
        recorded_version = by_feature.get(tour["feature"], 0)
        seen[tour["feature"]] = recorded_version >= tour["version"]
    return seen


def tours_for_page(actor_email: str | None, path: str) -> list[dict]:
    """The tours whose ``page`` prefix matches ``path`` AND which
    the user hasn't yet seen (or has seen an older version of).
    Used by the JS layer to decide whether to auto-fire on load."""
    seen = state_for_actor(actor_email)
    out: list[dict] = []
    for tour in TOURS:
        page = tour["page"]
        # "/" is special — only match the literal home path so a
        # box detail page doesn't auto-fire the welcome tour.
        if page == "/":
            matches = path == "/"
        else:
            matches = path == page or path.startswith(page)
        if matches and not seen[tour["feature"]]:
            out.append(tour)
    return out


def mark_seen(actor_email: str, feature: str) -> None:
    """Record that the user has completed (or dismissed) a tour at
    its current version.  Idempotent — repeating the call updates
    seen_at but doesn't error."""
    tour = _TOURS_BY_FEATURE.get(feature)
    if tour is None:
        # Unknown feature ID — silently drop rather than fail; the
        # JS layer might be racing a server update where the tour
        # was renamed.
        return
    version = tour["version"]
    with db() as conn:
        conn.execute(
            "INSERT INTO tour_seen (actor_email, feature, version) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(actor_email, feature) "
            "DO UPDATE SET version = excluded.version, "
            "              seen_at = CURRENT_TIMESTAMP",
            (actor_email, feature, version),
        )
        conn.commit()
    _log.info("tour.seen actor=%s feature=%s version=%s",
              actor_email, feature, version)


def reset(actor_email: str, feature: str) -> None:
    """Drop the user's seen-record for one tour so they can replay
    it.  No-op if the row doesn't exist."""
    if feature not in _TOURS_BY_FEATURE:
        return
    with db() as conn:
        conn.execute(
            "DELETE FROM tour_seen WHERE actor_email = ? AND feature = ?",
            (actor_email, feature),
        )
        conn.commit()
    _log.info("tour.reset actor=%s feature=%s", actor_email, feature)


def reset_all(actor_email: str) -> None:
    """Replay every tour for one user."""
    with db() as conn:
        conn.execute(
            "DELETE FROM tour_seen WHERE actor_email = ?",
            (actor_email,),
        )
        conn.commit()
    _log.info("tour.reset_all actor=%s", actor_email)


def catalogue() -> list[dict]:
    """Public-shape list of every tour for the /usage management
    section: id, title, page, version.  Steps aren't included
    here — they're a frontend concern."""
    return [
        {"feature": t["feature"], "title": t["title"],
         "page": t["page"], "version": t["version"]}
        for t in TOURS
    ]
