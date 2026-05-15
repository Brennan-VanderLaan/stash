"""Public /about pages — Stripe KYC-grade transparency surface.

Pages must be reachable without authentication (oauth2-proxy +
stash actor middleware both bypass), must include the business
name + contact email + refund/cancellation policy.
"""

from __future__ import annotations

import base64
import importlib
import secrets
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _bootstrap_app(tmp_path, monkeypatch, **env):
    """Reload app with a fresh env so STASH_PUBLIC_* values land."""
    monkeypatch.setenv("STASH_DB", str(tmp_path / "stash.db"))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.setenv(
        "STASH_KEK",
        base64.b64encode(secrets.token_bytes(32)).decode(),
    )
    monkeypatch.delenv("STASH_OPERATOR_EMAILS", raising=False)
    monkeypatch.setenv("STASH_REQUIRE_HTTPS_TOKENS", "false")
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    if "app" in sys.modules:
        del sys.modules["app"]
    import app as app_module
    importlib.reload(app_module)
    return app_module


_ALL_ABOUT_PATHS = (
    "/about",
    "/about/pricing",
    "/about/terms",
    "/about/privacy",
    "/about/refunds",
    "/about/contact",
    "/about/sub-processors",
    "/about/transparency",
)


def test_about_pages_render_without_auth(tmp_path, monkeypatch):
    """Every /about page is accessible without an
    ``X-Forwarded-Email`` header — Stripe's KYC review crawler hits
    these unauthenticated, and a 403 would tank the verification."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        for path in _ALL_ABOUT_PATHS:
            r = c.get(path)
            assert r.status_code == 200, f"{path} returned {r.status_code}"


def test_about_pages_carry_business_name(tmp_path, monkeypatch):
    """Stripe requires the business name to match what we registered
    with them.  Env-var-configurable so prod can swap in a different
    name without touching code."""
    app_mod = _bootstrap_app(
        tmp_path, monkeypatch,
        STASH_PUBLIC_BUSINESS_NAME="Test Stash Co.",
    )
    with TestClient(app_mod.app) as c:
        for path in _ALL_ABOUT_PATHS:
            assert "Test Stash Co." in c.get(path).text


def test_about_contact_includes_email(tmp_path, monkeypatch):
    """Stripe specifically checks for a reachable customer-service
    channel.  We surface the configured email on the /about/contact
    page + as a footer link site-wide."""
    app_mod = _bootstrap_app(
        tmp_path, monkeypatch,
        STASH_PUBLIC_CONTACT_EMAIL="help@example.com",
    )
    with TestClient(app_mod.app) as c:
        page = c.get("/about/contact").text
    assert "help@example.com" in page
    assert "mailto:help@example.com" in page


def test_about_refunds_page_describes_policy(tmp_path, monkeypatch):
    """Stripe required fields on the page: refund + cancellation
    policy.  This test pins that both are surfaced in plain English
    so a future refactor can't accidentally drop one."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        page = c.get("/about/refunds").text
    assert "Cancellation" in page
    assert "Refunds" in page
    assert "14-day money-back" in page


def test_about_pricing_lists_both_plans(tmp_path, monkeypatch):
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        page = c.get("/about/pricing").text
    assert "Free" in page
    assert "Pro" in page
    # Pro tier display price defaults to "$4" (env var
    # ``STASH_PRO_PRICE_DISPLAY`` overrides if a deploy wants
    # a different number; Stripe is the source of truth, the
    # page just mirrors it).
    assert "$4" in page


def test_about_pricing_price_is_configurable(tmp_path, monkeypatch):
    """``STASH_PRO_PRICE_DISPLAY`` overrides the published price."""
    app_mod = _bootstrap_app(
        tmp_path, monkeypatch, STASH_PRO_PRICE_DISPLAY="$6",
    )
    with TestClient(app_mod.app) as c:
        page = c.get("/about/pricing").text
    assert "$6" in page


def test_about_sub_processors_lists_vendors(tmp_path, monkeypatch):
    """GDPR + Stripe both want the sub-processor list to be on the
    public site.  Pin the vendor names we currently rely on so a
    silent removal triggers the test."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        page = c.get("/about/sub-processors").text
    for vendor in ("Google", "Anthropic", "Stripe", "Backblaze B2"):
        assert vendor in page, f"sub-processors missing {vendor}"


def test_about_pages_default_contact_email_when_unset(tmp_path, monkeypatch):
    """No env var set → falls back to support@<host>.  Better than
    a hardcoded fake; operator gets a sane default during initial
    Stripe activation."""
    monkeypatch.delenv("STASH_PUBLIC_CONTACT_EMAIL", raising=False)
    app_mod = _bootstrap_app(
        tmp_path, monkeypatch,
        STASH_PUBLIC_URL="https://stash.example.com",
    )
    with TestClient(app_mod.app) as c:
        page = c.get("/about/contact").text
    assert "support@stash.example.com" in page


def test_about_index_links_to_every_sibling(tmp_path, monkeypatch):
    """Nav strip on the public site exposes every other /about page
    so a Stripe reviewer can click through without guessing the
    URL structure."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        page = c.get("/about").text
    for sibling in ("/about/pricing", "/about/refunds",
                    "/about/privacy", "/about/terms",
                    "/about/sub-processors", "/about/contact",
                    "/about/transparency"):
        assert f'href="{sibling}"' in page


def test_about_transparency_breaks_down_costs(tmp_path, monkeypatch):
    """The transparency page surfaces the cost ledger including the
    MA-tax line and the labor allocation — both load-bearing pieces
    of the "honest about margins" posture.  Pin the headline rows so
    a future copy refactor can't quietly drop them."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        page = c.get("/about/transparency").text
    # Cost lines: each row in the ledger.
    for line in ("Stripe processing fee", "AI APIs", "Compute",
                 "Storage", "Bandwidth", "State business taxes",
                 "Allocated to humans", "Remainder"):
        assert line in page
    # Roles glossary — Maintainer + Read-only + Operator + Admin
    # all named so the org-structure signal is on the public page.
    for role in ("Maintainer", "Read-only", "Operator", "Admin"):
        assert role in page


def test_about_transparency_reflects_configured_price(tmp_path, monkeypatch):
    """The page title + ledger headline both reflect
    STASH_PRO_PRICE_DISPLAY — a deploy that ships a different
    Pro price doesn't end up with a stale "$4" in the copy."""
    app_mod = _bootstrap_app(
        tmp_path, monkeypatch, STASH_PRO_PRICE_DISPLAY="$6",
    )
    with TestClient(app_mod.app) as c:
        page = c.get("/about/transparency").text
    assert "$6" in page


def test_about_pages_hide_feedback_widget(tmp_path, monkeypatch):
    """The in-app feedback bubble is a tenant-side surface; it
    shouldn't appear on the public pages (where the viewer may be
    a prospect or a compliance reviewer)."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        page = c.get("/about").text
    assert 'id="feedback-launcher"' not in page
