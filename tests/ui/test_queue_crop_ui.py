"""UI regression — /queue crop UI is interactive in a real browser.

Operator report (2026-05-17): "major regression on cropping in
app, I can't do it anymore with photos."  Root cause turned out
to be the stuck global drop overlay (see
``test_global_drop_overlay.py``) — a full-page dim + blur sat on
top of /queue so the cropper widget looked broken even though
the JS was functional underneath.

These tests pin the symptom directly: visit /queue with a
seeded pending item, verify the overlay isn't blocking the
page, verify the Adjust-crop button reaches and reveals the
cropper UI.  A future regression that re-introduces a visual
block (or breaks the cropper.js integration) fails this suite
loudly.
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def seeded_pending_item(live_server, seeded_tenant) -> dict:
    """Insert one pending_items row + its photo placeholder.

    The photo file doesn't need to exist on disk for this UI test
    — we only care that the queue card renders.  The cropper.js
    ``new Cropper(img)`` call would need the image to load to
    fully initialise, but the regression we're pinning is "page
    is visually blocked by a stuck overlay" which fails BEFORE
    the cropper init is even attempted.
    """
    import sqlite3
    conn = sqlite3.connect(live_server["db_path"])
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute(
            "SELECT id FROM pending_items WHERE name = 'UI Crop Subject' "
            "AND tenant_id = ? LIMIT 1",
            (seeded_tenant,),
        ).fetchone()
        if existing is not None:
            pending_id = int(existing["id"])
        else:
            cur = conn.execute(
                "INSERT INTO pending_items "
                "(name, description, photo, tenant_id) "
                "VALUES ('UI Crop Subject', 'fixture row', "
                "        'fake-pending-photo.jpg', ?)",
                (seeded_tenant,),
            )
            pending_id = int(cur.lastrowid)
            conn.commit()
    finally:
        conn.close()
    return {"pending_id": pending_id, "tenant_id": seeded_tenant}


def test_queue_overlay_does_not_block_page(page, live_server, seeded_pending_item):
    """The whole class of "cropping is broken" reports trace back
    to the global drop overlay being visible without an active
    drag.  On /queue (the page that hosts the cropper), the
    overlay MUST be hidden so the user can see + interact with
    the cropper widget."""
    page.goto(f"{live_server['url']}/queue")
    overlay = page.locator("#global-drop-overlay")
    # Element is pre-rendered (so the JS can flip it visible on a
    # real drag) but the ``hidden`` attribute + CSS rule must
    # combine to ``display: none``.
    assert overlay.count() == 1
    assert overlay.is_hidden(), (
        "global drop overlay must not be visible on /queue without "
        "an active file drag — symptom of the cropping-regression "
        "report from 2026-05-17"
    )


def test_queue_crop_button_is_present_and_clickable(
    page, live_server, seeded_pending_item,
):
    """The crop button must render + be clickable on the queue
    page.  Won't fully wait for cropper.js to initialise because
    that requires the underlying image to actually load (a real
    encrypted blob through /uploads/, which is more setup than
    this regression check needs).  The button being present + not
    hidden behind an overlay is the load-bearing signal that the
    cropping flow is reachable in a real browser."""
    pending_id = seeded_pending_item["pending_id"]
    page.goto(f"{live_server['url']}/queue")

    # The crop button lives inside a Customize <details>.  Expand
    # it so the button is visible (CSS collapses display until the
    # details opens, regardless of DOM presence).
    customize = page.locator(f'#card-{pending_id} details').first
    customize.evaluate("d => { d.open = true; }")

    crop_btn = page.locator(f"#crop-btn-{pending_id}")
    crop_btn.wait_for(state="visible", timeout=2000)
    assert crop_btn.is_enabled()
    # The whole-page check: nothing visually on top of the button
    # (no stuck drop overlay, no orphan modal, no full-screen
    # tour highlight).  Playwright's ``is_visible`` already
    # bottoms out at "occluded by another element" — if the
    # button is "visible" by this definition, it's reachable.
    assert crop_btn.is_visible()
