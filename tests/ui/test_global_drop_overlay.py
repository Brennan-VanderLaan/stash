"""UI regression — global drop-to-ingest overlay.

The first cut of the global drag-drop ingest used a dragenter /
dragleave counter to balance show/hide.  Real browsers fire those
events asymmetrically (drag leaving the window without a clean
dragleave, nested children firing both on the same crossing, VS
Code / DevTools-originated drags that never emit a final
dragleave) and the counter got stuck above zero — the overlay
froze on screen, dimming + blurring the whole app and blocking
real-user workflow.

This module pins three regression invariants:

1. **On a vanilla page load, the overlay is hidden.**  No drag
   event has fired, nothing should be visible.  Pure sanity check
   for the "stuck on initial render" symptom that prompted the
   emergency fix.
2. **The overlay is non-blocking even if it ever does become
   visible.**  ``pointer-events: none`` on the overlay element is
   load-bearing — if a browser quirk leaves it stuck, the user can
   still interact with the app underneath.  This pins the CSS rule
   so a future copy refactor can't quietly drop it.
3. **Simulated file dragover → shown; idle 300 ms → hidden.**
   The dragover-with-timeout pattern is what replaced the broken
   counter.  Drive a synthetic ``DragEvent`` with ``types =
   ['Files']`` (the marker the JS reads) and observe the visibility
   transitions.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def signed_in_page(page, live_server, seeded_tenant):
    """Drop the test session onto /home with the actor middleware
    resolving to the seeded UI Test tenant.  Avoids the public
    landing surface which has its own (separate) DOM."""
    page.goto(f"{live_server['url']}/home")
    return page


def test_overlay_hidden_on_initial_page_load(signed_in_page):
    """The "stuck on screen" symptom that prompted the emergency
    fix: the overlay was visible before any drag had occurred,
    dimming and blurring the whole app.  This test fails loudly
    if a future change re-introduces the regression."""
    overlay = signed_in_page.locator("#global-drop-overlay")
    # The element exists in the DOM (it's pre-rendered so the JS
    # can show it on a real drag without injecting markup), but
    # MUST start hidden via the ``hidden`` attribute.
    assert overlay.count() == 1, "overlay element should be in DOM"
    assert overlay.is_hidden(), "overlay must not be visible on page load"


def test_overlay_is_pointer_events_none_so_stuck_state_doesnt_block(
    signed_in_page,
):
    """Belt-and-suspenders: even if a browser quirk leaves the
    overlay visible without a real drag, ``pointer-events: none``
    means clicks pass through to the app underneath.  Pin the rule
    so a future style refactor can't drop the guarantee."""
    overlay = signed_in_page.locator("#global-drop-overlay")
    pointer_events = overlay.evaluate(
        "el => getComputedStyle(el).pointerEvents"
    )
    assert pointer_events == "none", (
        f"overlay must be ``pointer-events: none`` to never block "
        f"the app; got {pointer_events!r}"
    )


def test_simulated_file_drag_shows_then_idle_hides(signed_in_page):
    """Drive the dragover handler with a synthetic ``DragEvent``
    carrying the ``Files`` type marker.  Overlay should appear on
    the first dragover and auto-hide after the 200 ms idle
    timeout when no further dragover events fire.

    Synthetic ``DragEvent`` workaround: ``dataTransfer.types`` is
    read-only in browsers, so we set up a Proxy / Object.defineProperty
    in JS to pretend the drag carries files.  The handler's
    ``hasFiles()`` check inspects ``dataTransfer.types`` first."""
    page = signed_in_page
    overlay = page.locator("#global-drop-overlay")
    assert overlay.is_hidden()

    # Dispatch a synthetic dragover that the handler will believe
    # carries files.
    page.evaluate(
        """
        () => {
          const e = new Event('dragover', { bubbles: true, cancelable: true });
          // Forge a dataTransfer with a ``types`` getter the handler
          // recognises.  Real DragEvents have a read-only dataTransfer
          // assigned by the browser; on a synthetic Event we can attach
          // our own.
          Object.defineProperty(e, 'dataTransfer', {
            value: {
              types: ['Files'],
              dropEffect: 'none',
            },
          });
          window.dispatchEvent(e);
        }
        """
    )
    # Overlay should be visible immediately after the synthetic
    # dragover (the handler calls overlay.hidden = false synchronously).
    overlay.wait_for(state="visible", timeout=2000)
    assert overlay.is_visible()

    # No further dragover events — the 200 ms idle timer should
    # fire and hide the overlay.  Give it a comfortable headroom
    # for CI machines under load.
    overlay.wait_for(state="hidden", timeout=2000)
    assert overlay.is_hidden()


def test_overlay_click_dismisses_immediately(signed_in_page):
    """If the dragover-timeout safety net ever fails to fire (some
    new browser quirk we haven't anticipated), the user can still
    dismiss the overlay by clicking the hint card.  Pin the
    escape-hatch behaviour."""
    page = signed_in_page
    overlay = page.locator("#global-drop-overlay")

    # Force the overlay visible (bypass the drag flow — we're
    # testing the dismiss path, not the show path).
    page.evaluate(
        "() => { document.getElementById('global-drop-overlay').hidden = false; }"
    )
    overlay.wait_for(state="visible", timeout=2000)

    # Click the dismissable hint card inside the overlay.
    page.locator(".global-drop-hint").click()
    overlay.wait_for(state="hidden", timeout=2000)
    assert overlay.is_hidden()
