"""UI regression — /locations/{id} hidden surfaces stay hidden.

Operator report (feedback #63, 2026-05-17): "I'm seeing visual bugs
where replace floorplan and delete this floor are showing up behind
the floorplan image at all times, it's a strange bug — we'll need
playwright tests to make sure this is rendering properly / that
this goes away.  I suspect we are running into cases where we need
to populate more data into pages to get the rendering bugs to
occur."

The user diagnosed the test gap correctly: a dialog with custom CSS
that overrides UA ``display: none`` renders inline at all times.
The bug only surfaces when there's enough content on the page that
the leaked form fields visibly overlap something else (in this
case, the floorplan image).

Same bug class as ``#floorplan-box-dialog`` (feedback #37/#41/#46
ages ago) and ``.global-drop-overlay[hidden]`` (today's emergency
overlay fix).  This module is the regression net for the whole
class — every ``<dialog>`` whose CSS includes an unscoped
``display:`` rule will trip these assertions.
"""
from __future__ import annotations

import pytest


def test_location_settings_dialog_is_hidden_until_opened(
    page, live_server, populated_floorplan,
):
    """The Settings dialog on /locations/{id} renders ALWAYS-VISIBLE
    when its custom CSS sets ``display: flex`` without the
    ``[open]`` qualifier.  Verify the dialog is hidden on initial
    page load (no Settings button click) — the user shouldn't see
    Replace-floorplan / Delete-this-floor controls just by visiting
    the page."""
    location_id = populated_floorplan["location_id"]
    page.goto(f"{live_server['url']}/locations/{location_id}")

    dialog = page.locator("#location-settings-dialog")
    assert dialog.count() == 1, "dialog element should be in the DOM"
    assert dialog.is_hidden(), (
        "Location-settings dialog must not be visible on initial "
        "load — the Replace-floorplan / Delete-this-floor controls "
        "should only appear when the user clicks ⚙️ Settings.  "
        "Feedback #63: dialog CSS is overriding UA's "
        "``dialog:not([open]) { display: none }`` rule."
    )

    # Belt-and-suspenders: the form fields inside the dialog must
    # also not be reachable by clicking on the page.  If the dialog
    # IS rendered inline (the bug), clicking near the bottom of the
    # page would land on a form input rather than the underlying
    # location content.
    replace_input = page.locator(
        "#location-settings-dialog input[type='file']"
    ).first
    assert replace_input.is_hidden(), (
        "Replace-floorplan input is reachable without the user "
        "opening the Settings dialog — bug #63 reproduces."
    )


def test_location_settings_dialog_opens_when_settings_clicked(
    page, live_server, populated_floorplan,
):
    """Counterpart to the test above: when the user DOES click
    Settings, the dialog opens.  Pins the positive case so a
    future fix that hides the dialog too aggressively (e.g.,
    forgetting the ``[open]`` selector entirely) fails loudly."""
    location_id = populated_floorplan["location_id"]
    page.goto(f"{live_server['url']}/locations/{location_id}")

    settings_btn = page.locator("[data-open-location-settings]").first
    settings_btn.scroll_into_view_if_needed()
    settings_btn.click()

    dialog = page.locator("#location-settings-dialog")
    dialog.wait_for(state="visible", timeout=2000)
    assert dialog.is_visible()

    # The form fields inside are now visible and clickable.
    rename_input = page.locator(
        "#location-settings-dialog input[name='name']"
    ).first
    rename_input.wait_for(state="visible", timeout=1000)
    assert rename_input.is_visible()


# ── Generic sweep: no dialog should leak visible on page load ──
#
# The whole bug class this module exists for: a custom
# ``<dialog>`` whose CSS sets ``display: flex`` / ``display:
# block`` / etc. without scoping to ``[open]`` overrides the
# UA's ``dialog:not([open]) { display: none }`` rule.  Result:
# the dialog renders inline on every page load, visible at all
# times, until someone notices.
#
# Walk the major signed-in surfaces with populated fixtures and
# assert NO ``<dialog>`` element is visible without user
# interaction.  Adding a new ``<dialog>`` later that forgets the
# ``[open]`` scope fails this test wherever it lands.


MAJOR_AUTHENTICATED_PAGES = [
    "/home",
    "/queue",
    "/usage",
    "/admin",
    "/labels",
    "/search",
    "/tags",
    "/maintenance",
]


@pytest.mark.parametrize("path", MAJOR_AUTHENTICATED_PAGES)
def test_no_dialog_leaks_visible_on_page_load(
    page, live_server, populated_admin, populated_floorplan, path,
):
    """For every major authenticated surface: load the page with
    populated data, assert every ``<dialog>`` element in the DOM
    is hidden.  A dialog with custom CSS that forgets to scope
    its visibility rules to ``[open]`` fails this assertion
    immediately and points at the leaking element.

    Uses both populated fixtures so the page renders with real
    content beneath any potential leaked dialog — the user's
    diagnosis was that empty pages mask these bugs."""
    page.goto(f"{live_server['url']}{path}")
    # Some pages need a beat for JS to settle (queue's
    # cropper.js init, admin's chart rendering); use
    # ``domcontentloaded`` then a tiny wait for ``networkidle``
    # so any dialog the page might programmatically open during
    # init has had a chance to do so.
    page.wait_for_load_state("domcontentloaded")
    leaked = page.evaluate(
        """
        () => {
          const out = [];
          for (const d of document.querySelectorAll('dialog')) {
            const cs = getComputedStyle(d);
            if (cs.display === 'none') continue;
            if (d.hasAttribute('open')) continue;  // legitimately open
            out.push({
              id: d.id || '(no id)',
              cls: d.className || '(no class)',
              display: cs.display,
            });
          }
          return out;
        }
        """
    )
    assert leaked == [], (
        f"On {path}, {len(leaked)} <dialog> element(s) are "
        f"rendering visibly without an ``[open]`` attribute:\n"
        + "\n".join(
            f"  - #{d['id']} .{d['cls']} (display: {d['display']})"
            for d in leaked
        )
        + "\nThe dialog's CSS likely has an unscoped ``display:`` "
        "rule overriding the UA's ``dialog:not([open]) { display: "
        "none }``.  Scope the rule to ``selector[open]``."
    )
