"""Regression tests for deploy/docker-compose.yml settings that
have already broken production once.

oauth2-proxy's ``SKIP_AUTH_ROUTES`` list is the configuration that
decides which endpoints reach stash without a session cookie.  Get
this wrong and:

* Stripe webhook deliveries get intercepted with a 302 to
  /oauth2/sign_in (the bug this test class was born from —
  user paid, Stripe Dashboard showed active, tenant never flipped
  to Pro because /webhooks/stripe POSTs from Stripe's IPs arrived
  with no cookie and oauth2-proxy auth-rejected them).
* OAuth discovery endpoints (.well-known) get gated, which
  every standards-compliant MCP client probes BEFORE auth.
* Public marketing surfaces (/, /about/...) require sign-in,
  which breaks ad attribution + SEO.

Pin every endpoint that MUST be unauthenticated so a future
"let me clean up this config" pass can't quietly drop one.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "deploy" / "docker-compose.yml"


@pytest.fixture(scope="module")
def skip_auth_routes() -> list[str]:
    """Parse ``OAUTH2_PROXY_SKIP_AUTH_ROUTES`` from the compose
    file.  The value is a comma-separated list of regex patterns;
    we just need to confirm a given pattern is present, not
    compile each as a regex."""
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(
        r"OAUTH2_PROXY_SKIP_AUTH_ROUTES:\s*(\S+)", text,
    )
    if not match:
        pytest.fail("OAUTH2_PROXY_SKIP_AUTH_ROUTES not set in docker-compose.yml")
    return [p.strip() for p in match.group(1).split(",")]


def test_stripe_webhook_is_publicly_reachable(skip_auth_routes):
    """``/webhooks/stripe`` MUST be unauthenticated — Stripe's
    delivery POSTs arrive with no session cookie and oauth2-proxy
    would otherwise 302 them to sign-in.  Without this rule,
    every Pro upgrade silently fails to apply: Stripe charges
    the card, Stripe Dashboard shows the subscription active,
    but the webhook never lands in stash to flip
    ``tenants.plan`` → 'pro'.

    Real production incident 2026-05-17 — user upgraded, paid,
    didn't get Pro features.  This test pins the fix so the
    same regression can't recur."""
    assert any("webhooks/stripe" in p for p in skip_auth_routes), (
        f"/webhooks/stripe missing from SKIP_AUTH_ROUTES: "
        f"{skip_auth_routes!r}.  Stripe webhook deliveries will "
        f"be auth-rejected by oauth2-proxy and tenants won't be "
        f"upgraded after paying."
    )


def test_oauth_discovery_endpoints_are_publicly_reachable(skip_auth_routes):
    """``/.well-known/oauth-*`` MUST be unauthenticated.  Every
    standards-compliant MCP client probes these BEFORE auth to
    discover where to send the user for the OAuth flow."""
    joined = ",".join(skip_auth_routes)
    assert "well-known/oauth-" in joined, (
        f"OAuth discovery missing from SKIP_AUTH_ROUTES: {skip_auth_routes!r}"
    )


def test_public_marketing_surfaces_are_publicly_reachable(skip_auth_routes):
    """Landing (``/``), /about/*, /static/, /encircle-alternative
    are part of the marketing funnel — ad traffic clicking through
    must reach them without a Google sign-in."""
    joined = ",".join(skip_auth_routes)
    for required in (
        "^/$",                        # landing
        "/about",                     # the /about/* tree
        "/static/",                   # CSS, fonts
        "encircle-alternative",       # ad campaign landing
    ):
        assert required in joined, (
            f"public marketing route {required!r} missing from "
            f"SKIP_AUTH_ROUTES: {skip_auth_routes!r}"
        )


def test_healthz_is_publicly_reachable(skip_auth_routes):
    """``/healthz`` is what Docker/uptime checks ping — gating
    behind auth would mark the container unhealthy."""
    assert any("healthz" in p for p in skip_auth_routes), (
        f"/healthz missing from SKIP_AUTH_ROUTES: {skip_auth_routes!r}"
    )


def test_api_v1_and_mcp_are_publicly_reachable(skip_auth_routes):
    """``/api/v1/`` and ``/mcp`` use their own bearer-token auth —
    oauth2-proxy must not pre-empt with a Google session check
    (agents don't have a Google session)."""
    joined = ",".join(skip_auth_routes)
    assert "api/v1" in joined, (
        f"/api/v1/ missing from SKIP_AUTH_ROUTES: {skip_auth_routes!r}"
    )
    assert "/mcp" in joined, (
        f"/mcp missing from SKIP_AUTH_ROUTES: {skip_auth_routes!r}"
    )
