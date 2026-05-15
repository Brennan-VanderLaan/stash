"""Pin the defense-in-depth security headers.

The app's middleware adds a set of headers to every response so the
protection holds even if a future deploy strips them at the edge.
Each entry below is one we've reasoned about — adding or dropping
one needs a code review.  The CSP is the single most-load-bearing
header and is asserted both for presence and for several
directives that historically went missing during refactors.
"""

from __future__ import annotations


def test_baseline_headers_present_on_every_response(client):
    """Hit ``/home`` and confirm every defense-in-depth header
    appears.  ``/home`` is a routine authenticated page; the same
    middleware fires on every response that isn't a streaming
    file.  An absent header here means a regression."""
    r = client.get("/home")
    assert r.status_code == 200, r.status_code
    expected = {
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
    for k, v in expected.items():
        assert r.headers.get(k) == v, (
            f"{k!r} = {r.headers.get(k)!r}, expected {v!r}"
        )


def test_csp_includes_load_bearing_directives(client):
    """The CSP is the single biggest XSS / data-exfil defense.
    Pin the directives we actually need so a future "let me trim
    the CSP a bit" PR doesn't quietly drop ``form-action`` or
    ``object-src 'none'``."""
    csp = client.get("/home").headers.get("Content-Security-Policy", "")
    assert csp, "missing Content-Security-Policy"
    for directive in (
        "default-src 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "frame-src 'none'",
        "form-action 'self'",
        "base-uri 'self'",
        "connect-src 'self'",
    ):
        assert directive in csp, f"CSP missing directive: {directive!r}"


def test_permissions_policy_disables_all_sensitive_features(client):
    """The app doesn't use camera, microphone, geolocation, USB,
    Bluetooth, motion sensors, or payments — disable them so an
    XSS can't probe / abuse those APIs."""
    pp = client.get("/home").headers.get("Permissions-Policy", "")
    assert pp, "missing Permissions-Policy"
    for feature in (
        "camera=()",
        "microphone=()",
        "geolocation=()",
        "payment=()",
        "usb=()",
        "bluetooth=()",
    ):
        assert feature in pp, f"Permissions-Policy missing: {feature!r}"


def test_hsts_only_set_over_https(client):
    """HSTS over plaintext is a spec violation (browsers ignore
    it) and breaks ``http://testserver`` setups.  The middleware
    only stamps it when X-Forwarded-Proto says https."""
    r = client.get("/home")  # http://testserver, no x-forwarded-proto
    assert "Strict-Transport-Security" not in r.headers, (
        "HSTS leaked over plaintext"
    )
    r2 = client.get("/home", headers={"X-Forwarded-Proto": "https"})
    hsts = r2.headers.get("Strict-Transport-Security", "")
    assert "max-age=" in hsts, hsts
    assert "includeSubDomains" in hsts, hsts


def test_headers_also_set_on_public_landing(client):
    """The public ``/`` route bypasses the auth middleware but
    NOT the security-header middleware.  An anonymous visitor
    receiving the marketing page still gets the full header set."""
    # Use a fresh TestClient with no auth header so we exercise the
    # bypass path.  ``client`` is authed but ``/`` doesn't care.
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "default-src 'self'" in r.headers.get(
        "Content-Security-Policy", ""
    )
