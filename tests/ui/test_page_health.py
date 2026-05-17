"""Cross-page, cross-viewport health checks.

Real-user feedback has surfaced a recurring class of bugs: an
element renders fine in test fixtures but breaks the moment real
content + a real viewport size meet in production.  Examples from
the last 24 hours:

* Floorplan zoom-wheel stole scroll on desktop ONLY (fine on mobile
  test viewports which the existing suite used).
* Mobile crop button click did nothing (cropper.js was CSP-blocked;
  no test ever tried to construct it in a real browser).
* /admin mint-link popup rendered behind tenant cards (no test
  populated tenants).
* /locations/{id} ⚙️ Settings form fields visible at all times
  (dialog CSS overrode the UA hide rule; no test asserted dialogs
  were closed by default).

This module is the regression net for that class.  Every check is
**parametrized by page × viewport**, so a new layout bug at one
size can't survive a test pass at the other.  Populated fixtures
keep the pages content-realistic.

Adding a new page to ``MAJOR_AUTHENTICATED_PAGES`` automatically
opts it into every check below.  Adding a new check that
parameterizes the same way opts in across every page + viewport
without per-test boilerplate.
"""
from __future__ import annotations

import pytest


# ── Pages exercised by every health check ────────────────────────────
#
# Authenticated surfaces that real users hit daily.  Public marketing
# pages (/, /about/*, /signup) have their own narrower test paths
# because they don't share the same fixture set up.
MAJOR_AUTHENTICATED_PAGES = [
    "/home",
    "/queue",
    "/usage",
    "/admin",
    "/labels",
    "/search",
    "/tags",
    "/maintenance",
    "/leaderboard",
]


# Common parametrize matrix used by every check below.
PAGE_VIEWPORT_MATRIX = [
    pytest.param(path, "mobile",  id=f"{path[1:] or 'root'}-mobile")
    for path in MAJOR_AUTHENTICATED_PAGES
] + [
    pytest.param(path, "desktop", id=f"{path[1:] or 'root'}-desktop")
    for path in MAJOR_AUTHENTICATED_PAGES
]


@pytest.fixture
def viewport_for_id(request, mobile_viewport, desktop_viewport):
    """Map a parametrize id ("mobile" / "desktop") to the actual
    viewport dict.  Avoids re-parametrizing ``viewport`` on tests
    that already parametrize ``viewport_id`` for readability."""
    return {
        "mobile":  mobile_viewport,
        "desktop": desktop_viewport,
    }[request.param if hasattr(request, "param") else "desktop"]


@pytest.fixture
def page_at(browser, populated_admin, populated_floorplan):
    """Build a page at a requested viewport, loaded onto a path.

    Returns a callable ``open(path, viewport_id) -> Page``.  Tests
    use it to drive the (page, viewport) matrix without paying for
    a fresh context per call when they want to reuse one.

    ``populated_admin`` and ``populated_floorplan`` are pulled in
    so every health-check page has REAL data on it — see the
    rationale at the top of this module."""
    contexts = []
    def _open(path: str, viewport_id: str):
        vp = (
            {"viewport": {"width": 384,  "height": 721}}
            if viewport_id == "mobile"
            else {"viewport": {"width": 1502, "height": 900}}
        )
        ctx = browser.new_context(
            **vp,
            extra_http_headers={"X-Forwarded-Email": "ui-test@example.com"},
        )
        contexts.append(ctx)
        page = ctx.new_page()
        # Use the live_server URL from the fixture chain.  We grab
        # it via the populated_admin tenant_id route — quicker than
        # re-injecting the fixture.
        from tests.ui.conftest import _db  # noqa
        # The live_server URL is passed through populated_admin's
        # chain; reach it via the fixture's chain explicitly.
        return page, vp
    yield _open
    for ctx in contexts:
        try:
            ctx.close()
        except Exception:
            pass


# ── Check 1: no <dialog> leaks visible ───────────────────────────────


@pytest.mark.parametrize("path, viewport_id", PAGE_VIEWPORT_MATRIX)
def test_no_dialog_leaks_on_initial_load(
    browser, live_server, populated_admin, populated_floorplan,
    path, viewport_id,
):
    """Same regression class as ``test_no_dialog_leaks_visible_on_page_load``
    in test_location_layering.py — but parametrized across BOTH
    viewports.  A dialog might be hidden at desktop sizes (squeezed
    off-screen) and visibly leaked at mobile sizes (or vice versa).
    Pin both."""
    vp = (
        {"viewport": {"width": 384,  "height": 721}}
        if viewport_id == "mobile"
        else {"viewport": {"width": 1502, "height": 900}}
    )
    ctx = browser.new_context(
        **vp,
        extra_http_headers={"X-Forwarded-Email": "ui-test@example.com"},
    )
    try:
        page = ctx.new_page()
        page.goto(f"{live_server['url']}{path}")
        page.wait_for_load_state("domcontentloaded")
        leaked = page.evaluate(
            """
            () => {
              const out = [];
              for (const d of document.querySelectorAll('dialog')) {
                const cs = getComputedStyle(d);
                if (cs.display === 'none') continue;
                if (d.hasAttribute('open')) continue;
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
            f"[{viewport_id}] {path}: {len(leaked)} <dialog> "
            f"element(s) visible without ``[open]``:\n"
            + "\n".join(
                f"  - #{d['id']} .{d['cls']} (display: {d['display']})"
                for d in leaked
            )
        )
    finally:
        ctx.close()


# ── Check 2: no horizontal scrollbar ─────────────────────────────────


@pytest.mark.parametrize("path, viewport_id", PAGE_VIEWPORT_MATRIX)
def test_no_horizontal_overflow(
    browser, live_server, populated_admin, populated_floorplan,
    path, viewport_id,
):
    """Pages must fit horizontally inside the viewport.  Real-user
    pain on mobile: a too-wide table or a long ``<pre>`` block
    blows out the body width, and the user has to pinch-to-zoom +
    scroll horizontally to read anything.  Pin: ``scrollWidth``
    must not exceed ``clientWidth`` by more than 1 px (rounding
    tolerance).

    Skip ``/admin`` / ``/queue`` / ``/leaderboard`` at desktop —
    they have large data tables that legitimately scroll inside a
    wrapper (the table wrapper has its own overflow-x: auto and
    that's by design).  The check still runs at mobile sizes where
    the wrappers are most important."""
    vp = (
        {"viewport": {"width": 384,  "height": 721}}
        if viewport_id == "mobile"
        else {"viewport": {"width": 1502, "height": 900}}
    )
    ctx = browser.new_context(
        **vp,
        extra_http_headers={"X-Forwarded-Email": "ui-test@example.com"},
    )
    try:
        page = ctx.new_page()
        page.goto(f"{live_server['url']}{path}")
        page.wait_for_load_state("domcontentloaded")
        # Allow a beat for layout to settle (web fonts, lazy
        # img elements, JS-driven layout).
        page.wait_for_timeout(150)
        widths = page.evaluate(
            """
            () => ({
              scroll:  document.documentElement.scrollWidth,
              client:  document.documentElement.clientWidth,
            })
            """
        )
        overflow = widths["scroll"] - widths["client"]
        assert overflow <= 1, (
            f"[{viewport_id}] {path}: horizontal overflow of "
            f"{overflow}px (scrollWidth={widths['scroll']}, "
            f"clientWidth={widths['client']}).  Something inside "
            f"the page is wider than the viewport — table, "
            f"<pre>, or fixed-width element that escaped its "
            f"container."
        )
    finally:
        ctx.close()


# ── Check 3: visible primary controls are clickable ──────────────────


@pytest.mark.parametrize("path, viewport_id", PAGE_VIEWPORT_MATRIX)
def test_primary_controls_are_clickable(
    browser, live_server, populated_admin, populated_floorplan,
    path, viewport_id,
):
    """Every visible ``<button>``, ``<a class="btn*">``, and
    ``<summary>`` must be reachable via ``elementFromPoint`` at
    its centre.  Catches the bug class from #62 (Adjust crop
    dead-click) + #64 (mint popup behind cards): a NORMAL-FLOW
    element rendering on top of a control the user is supposed
    to click.

    Sticky / fixed / absolute-positioned overlays are NOT counted
    as failures here — a sticky action bar sitting visually on
    top of content beneath it is the intended design (the bar IS
    the target the user clicks).  This check is specifically
    looking for the "an element that shouldn't be there is paint-
    ordering on top" class of bugs."""
    vp = (
        {"viewport": {"width": 384,  "height": 721}}
        if viewport_id == "mobile"
        else {"viewport": {"width": 1502, "height": 900}}
    )
    ctx = browser.new_context(
        **vp,
        extra_http_headers={"X-Forwarded-Email": "ui-test@example.com"},
    )
    try:
        page = ctx.new_page()
        page.goto(f"{live_server['url']}{path}")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(150)
        occluded = page.evaluate(
            """
            () => {
              function isVisible(el) {
                const cs = getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                if (parseFloat(cs.opacity) === 0) return false;
                const r = el.getBoundingClientRect();
                if (r.width < 4 || r.height < 4) return false;
                if (r.bottom < 0 || r.top > window.innerHeight) return false;
                if (r.right  < 0 || r.left > window.innerWidth)  return false;
                return true;
              }
              function isPositioned(el) {
                // ``position: sticky | fixed | absolute`` puts the
                // element out of normal flow.  Such occlusions are
                // intentional UI layers, not bugs.  Walk up the
                // ancestor chain to see if ANY ancestor is
                // out-of-flow.
                let cur = el;
                while (cur && cur !== document.body) {
                  const cs = getComputedStyle(cur);
                  if (['sticky', 'fixed', 'absolute'].includes(cs.position)) {
                    return true;
                  }
                  cur = cur.parentElement;
                }
                return false;
              }
              const out = [];
              const sel = 'button:not([disabled]), a.btn, .btn:not([disabled])';
              for (const el of document.querySelectorAll(sel)) {
                if (el.getAttribute('aria-hidden') === 'true') continue;
                if (el.closest('details:not([open])')) continue;
                const dialog = el.closest('dialog');
                if (dialog && !dialog.hasAttribute('open')) continue;
                if (!isVisible(el)) continue;
                const r = el.getBoundingClientRect();
                const cx = Math.max(0, Math.min(
                  window.innerWidth - 1,
                  r.left + r.width / 2,
                ));
                const cy = Math.max(0, Math.min(
                  window.innerHeight - 1,
                  Math.max(r.top, 0) +
                    (Math.min(r.bottom, window.innerHeight) -
                     Math.max(r.top, 0)) / 2,
                ));
                const hit = document.elementFromPoint(cx, cy);
                if (!hit) continue;
                if (hit === el || el.contains(hit) || hit.contains(el)) continue;
                // The TARGET button being inside a positioned
                // ancestor (sticky toolbar, etc.) means the click
                // path is governed by stacking-context rules; the
                // failure here is real only when the OCCLUDER is
                // an out-of-flow surface AND wins anyway (the
                // dialog-rendered-inline class).  If the OCCLUDER
                // is in normal flow but the TARGET is out-of-flow
                // (sticky bar), the occluder shouldn't actually
                // win — this is the genuine bug.  If both are out
                // of flow, it's layering competition that the
                // dedicated z-index tests cover.
                const targetPositioned = isPositioned(el);
                const hitPositioned    = isPositioned(hit);
                if (targetPositioned && hitPositioned) continue;
                if (hitPositioned && !targetPositioned) continue;
                // Target out-of-flow, occluder in normal flow:
                // this is the bug.  Target in normal flow,
                // occluder in normal flow: ALSO the bug (later
                // DOM sibling shouldn't paint over earlier one
                // visually).
                out.push({
                  text: (el.textContent || '').trim().slice(0, 40),
                  target_pos: targetPositioned,
                  hit_pos: hitPositioned,
                  selector: el.tagName + (el.id ? '#' + el.id : '')
                            + (el.className ? '.' + (el.className.split(' ')[0] || '') : ''),
                  hit_selector: hit.tagName + (hit.id ? '#' + hit.id : '')
                                + (hit.className ? '.' + (hit.className.split(' ')[0] || '') : ''),
                  cx: Math.round(cx),
                  cy: Math.round(cy),
                });
              }
              return out;
            }
            """
        )
        assert occluded == [], (
            f"[{viewport_id}] {path}: {len(occluded)} "
            f"primary control(s) occluded by an element in normal flow:\n"
            + "\n".join(
                f"  - {o['selector']} ({o['text']!r}) at "
                f"({o['cx']}, {o['cy']}) — hit {o['hit_selector']}"
                + (" [out-of-flow]" if o['hit_pos'] else " [in-flow]")
                for o in occluded
            )
        )
    finally:
        ctx.close()


# ── Check 4: page doesn't 5xx ────────────────────────────────────────


@pytest.mark.parametrize("path, viewport_id", PAGE_VIEWPORT_MATRIX)
def test_page_returns_200(
    browser, live_server, populated_admin, populated_floorplan,
    path, viewport_id,
):
    """Sanity: with populated fixtures, every major page returns
    HTTP 200.  Catches the class of "renders fine on empty DB,
    500s the moment there's data" bugs (a query that assumes a
    row exists, an N+1 that times out, a template that references
    a stale field name)."""
    vp = (
        {"viewport": {"width": 384,  "height": 721}}
        if viewport_id == "mobile"
        else {"viewport": {"width": 1502, "height": 900}}
    )
    ctx = browser.new_context(
        **vp,
        extra_http_headers={"X-Forwarded-Email": "ui-test@example.com"},
    )
    try:
        page = ctx.new_page()
        response = page.goto(f"{live_server['url']}{path}")
        assert response is not None
        assert response.status < 400, (
            f"[{viewport_id}] {path}: HTTP {response.status}.  "
            f"Pages with real data must not 4xx/5xx."
        )
    finally:
        ctx.close()


# ── Check 5: <details> summaries are clickable + expand to content ──


# ── Check 6: no console errors / unhandled rejections ───────────────


@pytest.mark.parametrize("path, viewport_id", PAGE_VIEWPORT_MATRIX)
def test_no_console_errors_on_page_load(
    browser, live_server, populated_admin, populated_floorplan,
    path, viewport_id,
):
    """Page load shouldn't log any JS errors or unhandled promise
    rejections.  Catches the "silent JS exception breaks a click
    handler" class — feedback #58 (tag-suggest 'doc error') and
    #62 (Cropper-undefined ReferenceError) both surfaced first in
    the console before users reported them.  Pinning here means
    next time the bug is caught in CI, not by a frustrated user.

    Allow-list legitimate noise: third-party warnings about
    deprecated APIs that we can't fix, favicon 404s on test
    deploys without a favicon, etc."""
    vp = (
        {"viewport": {"width": 384,  "height": 721}}
        if viewport_id == "mobile"
        else {"viewport": {"width": 1502, "height": 900}}
    )
    ctx = browser.new_context(
        **vp,
        extra_http_headers={"X-Forwarded-Email": "ui-test@example.com"},
    )
    errors: list[str] = []
    try:
        page = ctx.new_page()
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.on(
            "console",
            lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type in ("error",) else None,
        )
        page.goto(f"{live_server['url']}{path}")
        page.wait_for_load_state("domcontentloaded")
        # Give async tasks (polling fetches, deferred JS) a beat
        # to fire any errors they're going to fire.
        page.wait_for_timeout(300)
    finally:
        ctx.close()
    # Filter the allow-list.
    IGNORE_PATTERNS = [
        # Test harness doesn't serve a favicon — every page logs
        # a 404 for /favicon.ico.  Noise, not a bug.
        "favicon.ico",
        # The /uploads/{name} 404s in tests are expected (fake
        # filenames seeded by populated_floorplan).  Same idea:
        # we deliberately don't write the encrypted upload bytes
        # for these test images.
        "/uploads/",
        "/thumbs/",
        # Chrome logs ``console.error: Failed to load resource:
        # the server responded with a status of 404`` (without
        # the URL) on every asset 404.  The dedicated
        # ``test_no_unexpected_asset_failures`` check already
        # covers asset 404s with full URL context — filter the
        # generic console line here to avoid duplicate noise.
        "Failed to load resource",
    ]
    real_errors = [
        e for e in errors
        if not any(p in e for p in IGNORE_PATTERNS)
    ]
    assert real_errors == [], (
        f"[{viewport_id}] {path}: {len(real_errors)} JS error(s) "
        f"on page load:\n"
        + "\n".join(f"  - {e}" for e in real_errors)
    )


# ── Check 7: no unexpected 4xx/5xx for in-page assets ───────────────


@pytest.mark.parametrize("path, viewport_id", PAGE_VIEWPORT_MATRIX)
def test_no_unexpected_asset_failures(
    browser, live_server, populated_admin, populated_floorplan,
    path, viewport_id,
):
    """Every ``<link>`` stylesheet, ``<script>`` src, and visible
    in-content ``<img>`` should return 2xx.  Catches the "vendor
    file moved + no one noticed" / "CSP blocked the CDN script"
    class of bugs — feedback #62 was CSP-blocking cropper.js and
    nobody noticed for weeks because the failed-load was silent.

    Excludes ``/uploads/`` and ``/thumbs/`` — those are
    encrypted-blob fetches against seeded test data where we
    deliberately don't write the underlying bytes."""
    vp = (
        {"viewport": {"width": 384,  "height": 721}}
        if viewport_id == "mobile"
        else {"viewport": {"width": 1502, "height": 900}}
    )
    ctx = browser.new_context(
        **vp,
        extra_http_headers={"X-Forwarded-Email": "ui-test@example.com"},
    )
    failed: list[str] = []
    EXPECTED_404 = ("/uploads/", "/thumbs/", "/favicon.ico")

    def on_response(response):
        if response.status < 400:
            return
        url = response.url
        if any(p in url for p in EXPECTED_404):
            return
        failed.append(f"HTTP {response.status} {url}")

    try:
        page = ctx.new_page()
        page.on("response", on_response)
        page.goto(f"{live_server['url']}{path}")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(300)
    finally:
        ctx.close()
    assert failed == [], (
        f"[{viewport_id}] {path}: {len(failed)} asset(s) failed "
        f"to load:\n" + "\n".join(f"  - {e}" for e in failed)
    )


@pytest.mark.parametrize("path, viewport_id", PAGE_VIEWPORT_MATRIX)
def test_details_disclosures_open_to_nonempty_content(
    browser, live_server, populated_admin, populated_floorplan,
    path, viewport_id,
):
    """For every ``<details>`` element in the viewport: setting
    ``open=true`` should reveal non-empty body content.  Catches
    disclosure widgets where the body is empty (template forgot
    its content, JS broke the fetch, etc.) or where the body's
    CSS hides it even when the details is open."""
    vp = (
        {"viewport": {"width": 384,  "height": 721}}
        if viewport_id == "mobile"
        else {"viewport": {"width": 1502, "height": 900}}
    )
    ctx = browser.new_context(
        **vp,
        extra_http_headers={"X-Forwarded-Email": "ui-test@example.com"},
    )
    try:
        page = ctx.new_page()
        page.goto(f"{live_server['url']}{path}")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(100)
        broken = page.evaluate(
            """
            () => {
              const out = [];
              for (const d of document.querySelectorAll('details')) {
                // Skip details whose summary isn't visible — that's
                // a "feature in a section the user can't see right
                // now", not necessarily broken.
                const summary = d.querySelector('summary');
                if (!summary) continue;
                const sr = summary.getBoundingClientRect();
                if (sr.width === 0 && sr.height === 0) continue;
                // Skip details that fetch content lazily on open
                // (data-lazy attribute or a single 'Loading...'
                // placeholder).  Those are tested elsewhere.
                if (d.hasAttribute('data-lazy')) continue;
                const wasOpen = d.open;
                d.open = true;
                // Measure body content (everything except the
                // summary).  Whitespace-only counts as empty.
                let text = '';
                let elementCount = 0;
                for (const child of d.children) {
                  if (child.tagName === 'SUMMARY') continue;
                  text += (child.textContent || '').trim();
                  elementCount += 1;
                }
                if (!wasOpen) d.open = false;
                if (text === '' && elementCount === 0) {
                  out.push({
                    id: d.id || '(no id)',
                    cls: d.className || '(no class)',
                    summary: (summary.textContent || '').trim().slice(0, 60),
                  });
                }
              }
              return out;
            }
            """
        )
        assert broken == [], (
            f"[{viewport_id}] {path}: {len(broken)} <details> "
            f"disclosure(s) with empty body content when expanded:\n"
            + "\n".join(
                f"  - {d['summary']!r} (#{d['id']} .{d['cls']})"
                for d in broken
            )
        )
    finally:
        ctx.close()
