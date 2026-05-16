"""Targeted ad-campaign landing for displaced Encircle users.

Three contracts this page MUST maintain or the campaign breaks:

1. The page is reachable without authentication (oauth2-proxy +
   stash actor middleware both bypass).  Visitors land here from
   a Google Search Ad — they can't be bounced to a Google sign-in
   wall before they read the pitch.
2. Crawlers see ``noindex,nofollow`` so the page never ranks
   organically.  We're paying for this traffic; competitors
   shouldn't shadow-rank against our keywords.
3. No internal cross-links — not in /about, not in the public
   landing, not in the footer.  Discovery is paid-only.
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
    monkeypatch.setenv("STASH_DB", str(tmp_path / "stash.db"))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.setenv(
        "STASH_KEK",
        base64.b64encode(secrets.token_bytes(32)).decode(),
    )
    monkeypatch.delenv("STASH_OPERATOR_EMAILS", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if "app" in sys.modules:
        del sys.modules["app"]
    import app as app_module
    importlib.reload(app_module)
    return app_module


def test_encircle_alternative_renders_without_auth(tmp_path, monkeypatch):
    """Ad-traffic visitors must reach the page without an
    X-Forwarded-Email header.  oauth2-proxy in front lets the path
    through via its SKIP_AUTH_ROUTES regex; the stash actor
    middleware bypasses via _AUTH_BYPASS_EXACT."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        r = c.get("/encircle-alternative")
    assert r.status_code == 200
    assert "Encircle" in r.text


def test_encircle_alternative_carries_noindex(tmp_path, monkeypatch):
    """The page must declare ``noindex,nofollow`` so Google
    doesn't surface it organically.  We're paying for this
    traffic via Search Ads; ranking it for free would dilute
    the campaign attribution."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        page = c.get("/encircle-alternative").text
    assert 'name="robots"' in page
    assert "noindex" in page
    assert "nofollow" in page


def test_encircle_alternative_not_linked_from_public_surfaces(
    tmp_path, monkeypatch,
):
    """No internal cross-links — discovery is paid-only.  Catches
    a future "let's just add it to the /about nav" accident that
    would silently leak the URL into organic traffic and break the
    campaign's attribution model."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        public_surfaces = [
            "/",
            "/about",
            "/about/pricing",
            "/about/transparency",
            "/about/refunds",
            "/about/privacy",
            "/about/terms",
            "/about/sub-processors",
            "/about/contact",
        ]
        for path in public_surfaces:
            body = c.get(path).text
            assert "/encircle-alternative" not in body, (
                f"{path} cross-links to /encircle-alternative — "
                "the campaign-only URL leaked into the public surface."
            )


def test_robots_txt_disallows_encircle_alternative(tmp_path, monkeypatch):
    """robots.txt belt-and-suspenders the meta-noindex.  Good
    crawlers respect the Disallow; bad ones still hit the meta
    on the page itself."""
    app_mod = _bootstrap_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as c:
        robots = c.get("/robots.txt").text
    assert "Disallow: /encircle-alternative" in robots
