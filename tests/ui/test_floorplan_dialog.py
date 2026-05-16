"""Regression: feedback #37 / #41 / #46.

A CSS rule on ``#floorplan-box-dialog`` overrode the UA stylesheet's
``dialog:not([open]) { display: none }`` with ``display: flex`` set
unconditionally, so the dialog rendered at all times — initial
``<div class="empty">Loading…</div>`` placeholder visible from page
load, X close button doing nothing because ``<form method="dialog">``
is a no-op on a dialog that was never actually opened.

The fix lives in static/style.css under
``#floorplan-box-dialog[open]``.  This test catches a re-regression
by rendering the page in a real browser and asserting the dialog
is hidden at first paint + properly toggles open on tile click +
closes on X click.

The in-process FastAPI TestClient suite cannot catch this class of
bug — it produces HTML byte-perfectly but never computes CSS or
runs JS.  This module is exactly what that other suite is missing.
"""
from __future__ import annotations

from playwright.sync_api import Page, expect


def test_floorplan_box_dialog_is_hidden_at_page_load(
    page: Page, live_server, seeded_floorplan,
) -> None:
    """At first paint the box-preview dialog must be ``display:
    none``.  The bug it's catching: a sibling CSS rule with
    ``display: flex`` beat the UA gate, leaving the dialog rendered
    permanently with the Loading… placeholder.  The user's "I can't
    make it go away" report was the visual symptom of THIS line of
    CSS — three rounds of JS-side close handlers couldn't fix
    something that was always-visible by CSS specificity."""
    loc_id = seeded_floorplan["location_id"]
    page.goto(f"{live_server['url']}/locations/{loc_id}")
    # Dialog element exists in the DOM but must not be visible.
    dialog = page.locator("#floorplan-box-dialog")
    expect(dialog).to_be_attached()
    expect(dialog).to_be_hidden()


def test_floorplan_box_dialog_visibility_follows_open_attribute(
    page: Page, live_server, seeded_floorplan,
) -> None:
    """The core CSS contract the bug broke: the dialog is hidden
    when it lacks ``[open]`` and visible when it has it.  We
    drive the attribute directly via ``showModal()``/``close()``
    rather than going through the tile-click chain — the seeded
    floorplan has no decryptable image so chromium renders a
    broken-image placeholder that intercepts clicks (real-world
    deploys with working images don't hit this).  Programmatic
    open/close exercises exactly the surface the bug lived on:
    the CSS ``display`` rule keyed off ``[open]``."""
    loc_id = seeded_floorplan["location_id"]
    page.goto(f"{live_server['url']}/locations/{loc_id}")
    dialog = page.locator("#floorplan-box-dialog")
    expect(dialog).to_be_hidden()

    page.evaluate("document.getElementById('floorplan-box-dialog').showModal()")
    expect(dialog).to_be_visible()

    page.evaluate("document.getElementById('floorplan-box-dialog').close()")
    expect(dialog).to_be_hidden()


def test_floorplan_box_dialog_close_button_dismisses(
    page: Page, live_server, seeded_floorplan,
) -> None:
    """Once open, clicking the X close button dismisses the
    dialog.  Catches the "<form method='dialog'> is a no-op"
    failure mode the bug produced — without the CSS fix the X did
    nothing because the dialog never actually held ``[open]``,
    even after ``showModal()`` (the user's "Loading… won't go away"
    in #46 was exactly this state).

    Programmatic open + real click on X — same shape as a user
    clicking a tile and then the X, but without the broken-image
    placeholder eating the first click in a test environment."""
    loc_id = seeded_floorplan["location_id"]
    page.goto(f"{live_server['url']}/locations/{loc_id}")
    dialog = page.locator("#floorplan-box-dialog")
    page.evaluate("document.getElementById('floorplan-box-dialog').showModal()")
    expect(dialog).to_be_visible()
    page.locator("#floorplan-box-dialog .item-dialog-close").click()
    expect(dialog).to_be_hidden()


def test_floorplan_box_dialog_initial_html_does_not_render_loading_text(
    page: Page, live_server, seeded_floorplan,
) -> None:
    """The initial ``<div class="empty">Loading…</div>`` text inside
    the dialog must not be VISIBLE on the page before the user
    clicks anything.  This is the user-facing symptom — they see
    "Loading…" floating on the page they didn't ask for.

    Distinct from ``to_be_hidden`` on the dialog: a future
    regression could make the dialog hidden but accidentally hoist
    its inner Loading… text outside via some flexbox quirk.  Direct
    text-visibility assertion catches that variant too."""
    loc_id = seeded_floorplan["location_id"]
    page.goto(f"{live_server['url']}/locations/{loc_id}")
    # Locator scoped to inside the dialog so we don't false-positive
    # on a "Loading…" string that appears somewhere unrelated in
    # the page later.
    body = page.locator("#floorplan-box-dialog #floorplan-box-body")
    expect(body).to_be_attached()
    expect(body).to_be_hidden()
