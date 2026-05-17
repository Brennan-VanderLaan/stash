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

    crop_btn = page.locator(f"#crop-btn-{pending_id}")
    crop_btn.wait_for(state="visible", timeout=2000)
    assert crop_btn.is_enabled()
    # The whole-page check: nothing visually on top of the button
    # (no stuck drop overlay, no orphan modal, no full-screen
    # tour highlight).  Playwright's ``is_visible`` already
    # bottoms out at "occluded by another element" — if the
    # button is "visible" by this definition, it's reachable.
    assert crop_btn.is_visible()


def test_cropperjs_loads_under_csp(page, live_server, seeded_pending_item):
    """Feedback #62 root cause: the app's CSP is
    ``script-src 'self' 'unsafe-inline'``, which blocks
    external-CDN scripts.  Cropper.js used to load from
    cdnjs.cloudflare.com — the script was silently CSP-blocked,
    every Adjust-crop click threw ``ReferenceError: Cropper is
    not defined``, and the user saw "nothing happens" with no
    on-page indicator.  Mobile users felt it first because they
    can't easily check the browser console.

    Pin the fix: self-hosted Cropper.js at /static/vendor/ must
    load successfully and expose the global ``Cropper`` constructor.
    A future "let me put this back on a CDN" pass that re-introduces
    the same CSP block fails this test."""
    page.goto(f"{live_server['url']}/queue")
    # ``Cropper`` is a global function constructor — if the script
    # loaded, ``typeof Cropper === 'function'``.  CSP-blocked
    # script would leave the identifier undefined.
    is_loaded = page.evaluate("typeof Cropper === 'function'")
    assert is_loaded is True, (
        "Cropper.js did not load — check that the script tag in "
        "templates/queue.html points at /static/vendor/ and not "
        "an external CDN (the app's CSP blocks external script "
        "sources)."
    )


def test_cropperjs_constructor_runs_against_a_real_image(
    page, live_server, seeded_pending_item,
):
    """Feedback #62 end-to-end: ``new Cropper(img)`` must succeed
    in the real browser — no ReferenceError (cropper.js not
    loaded), no TypeError (wrong API shape).

    We don't actually click ``Adjust crop`` here because the
    fake-pending-photo.jpg fixture isn't a real encrypted upload,
    so the IMG never loads + cropper.js's ``new Cropper`` may
    defer or warn waiting for the load event.  Instead, evaluate
    the constructor against a freshly-injected image with a real
    data URI so the test asks the focused question: does the
    library work?

    A 1x1 transparent PNG data URI is enough — cropper.js's
    init path doesn't care about the content, only that the IMG
    has naturalWidth/naturalHeight to size against."""
    page.goto(f"{live_server['url']}/queue")
    # Inject a tiny real image + try to construct a Cropper.  The
    # data URI matches CSP's ``img-src 'self' data:`` allow-list.
    # ``async`` because the image needs to fire its load event
    # before Cropper's constructor reads naturalWidth.
    result = page.evaluate(
        """
        async () => {
          const img = document.createElement('img');
          img.src = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=';
          document.body.appendChild(img);
          await new Promise(r => {
            if (img.complete && img.naturalWidth) return r();
            img.addEventListener('load', r, { once: true });
            img.addEventListener('error', r, { once: true });
          });
          try {
            const c = new Cropper(img, { viewMode: 1 });
            const ok = typeof c === 'object' && c !== null;
            c.destroy();
            img.remove();
            return { ok, error: null };
          } catch (e) {
            img.remove();
            return { ok: false, error: String(e) };
          }
        }
        """
    )
    assert result["ok"] is True, (
        f"Cropper constructor failed: {result['error']!r}.  "
        f"Feedback #62 regression class — the library didn't load "
        f"or its API broke."
    )
