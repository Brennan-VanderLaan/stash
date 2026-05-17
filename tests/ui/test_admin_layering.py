"""UI regression — /admin popups MUST paint above the section content.

Operator report (feedback #64, 2026-05-17): "When I do mint
onboarding link the pop up input thing ends up rendering behind
the tenants data / tenant cards.  I think we need to do more
tests with playwright where we have data in the page / things
going on as well as the existing tests where we test blank loads."

Root cause class: z-index + stacking context rules pass on a
blank page (no cards to stack against) and break the moment real
data populates the section.  This module is the regression net —
populated tenant fixtures plus an explicit elementFromPoint check
to confirm the rendered popup wins the paint contest.
"""
from __future__ import annotations

import pytest


def _click_x_y_topmost(page, x: float, y: float) -> str:
    """What CSS class is the topmost element at (x, y)?  Drives
    the elementFromPoint-style overlap check below."""
    return page.evaluate(
        "([x, y]) => {"
        "  const el = document.elementFromPoint(x, y);"
        "  return el ? (el.className || '') + ' /tag:' + el.tagName : 'none';"
        "}",
        [x, y],
    )


def test_mint_onboarding_link_popup_paints_above_tenant_cards(
    page, live_server, populated_admin,
):
    """Open the Mint-link disclosure on a /admin page that already
    has six tenant cards rendered below.  The form's centre point
    MUST resolve to a descendant of ``.admin-quick-action`` — if
    elementFromPoint finds a tenant card there instead, the user
    cannot interact with the form (the bug #64 reproduces)."""
    page.goto(f"{live_server['url']}/admin")
    # Tenants section is deep in the page; scroll it into view
    # first so the form (when opened) sits within the visible
    # viewport.  ``elementFromPoint`` returns null for
    # coordinates outside the viewport, which would mask the
    # actual occlusion problem we're testing.
    trigger = page.locator(".admin-quick-action > summary").first
    trigger.scroll_into_view_if_needed()
    # Open the disclosure.  Use evaluate so we don't fight any
    # native click bubble issues; ``open=true`` is the canonical
    # way to expand a <details>.
    page.evaluate(
        "() => {"
        "  const d = document.querySelector('.admin-quick-action');"
        "  if (d) d.open = true;"
        "}"
    )
    form = page.locator(".admin-quick-action-form").first
    form.wait_for(state="visible", timeout=2000)
    # Scroll again — opening the form may have pushed the
    # mid-point below the viewport bottom on a small browser
    # window.  ``scroll_into_view_if_needed`` on the form itself
    # makes sure the centre is reachable for elementFromPoint.
    form.scroll_into_view_if_needed()

    # Probe the form's centre point.  ``elementFromPoint``
    # returns whichever element is on top at the given coords;
    # if that's a tenant card, the form is occluded.
    box = form.bounding_box()
    assert box is not None, "form has no bounding box — not laid out"
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2

    topmost = _click_x_y_topmost(page, cx, cy)
    assert "admin-tenant-card" not in topmost, (
        f"Mint-onboarding-link form is occluded by a tenant card "
        f"at its centre point ({cx:.0f}, {cy:.0f}); topmost "
        f"element is {topmost!r}.  Feedback #64 regression — "
        f"the form needs to win the z-index contest against "
        f"populated tenant content."
    )
    # Sanity: the topmost element should be inside the form
    # itself (input / select / button / label) or the form
    # surface.  Anything else means we lost the layout race.
    assert ("admin-quick-action" in topmost
            or "/tag:INPUT" in topmost
            or "/tag:SELECT" in topmost
            or "/tag:LABEL" in topmost
            or "/tag:BUTTON" in topmost), (
        f"unexpected topmost element under form centre: "
        f"{topmost!r}"
    )


def test_mint_form_inputs_are_clickable_when_data_below(
    page, live_server, populated_admin,
):
    """Beyond the z-index check: a user must actually be able to
    type into the form's plan/role/expires inputs.  If the form
    were occluded but Playwright happened to read its bounding
    box anyway, ``form.locator('input').click()`` would land on
    the wrong element and silently fail to focus.  This test
    asserts the click reaches the intended input."""
    page.goto(f"{live_server['url']}/admin")
    page.evaluate(
        "() => {"
        "  const d = document.querySelector('.admin-quick-action');"
        "  if (d) d.open = true;"
        "}"
    )

    expires = page.locator(".admin-quick-action-form input[name='expires_in_days']")
    expires.wait_for(state="visible", timeout=2000)
    expires.click()
    # If the click missed (form occluded), this input wouldn't be
    # the active element.
    is_focused = expires.evaluate(
        "el => document.activeElement === el"
    )
    assert is_focused is True, (
        "Expires-in-days input could not be focused — the form "
        "is occluded by something above it in the stacking order."
    )
