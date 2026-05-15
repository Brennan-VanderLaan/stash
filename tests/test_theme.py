"""Theme system — palette tokens + user-facing picker.

The theme picker is a CSS-variable swap driven by ``data-theme`` on
``<html>``.  We don't render-test the visual outcome (that's a job
for the browser); we pin the shape of the system so future work
doesn't accidentally drop the bootstrap script, the picker UI, or
one of the palette blocks.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLE = ROOT / "static" / "style.css"


def test_default_palette_is_softer_than_matrix():
    """The forest default must use the lower-contrast bg, not the
    near-black ``#0f1f14`` that defined the matrix-terminal look.
    Catches a future revert to the bright neon palette."""
    css = STYLE.read_text(encoding="utf-8")
    # The default :root must declare the new forest bg.
    assert "--bg:           #1b231d" in css, \
        "Forest default --bg has regressed; expected #1b231d"
    # And the matrix palette must still be available under its own
    # ``[data-theme]`` selector so users who want the old look can
    # opt back in.
    assert "[data-theme=\"matrix\"]" in css


def test_all_four_palettes_define_required_tokens():
    """Each theme block must define the core semantic tokens or the
    swatch will render with mixed values (white text on white card,
    etc.) when the user previews it."""
    css = STYLE.read_text(encoding="utf-8")
    REQUIRED = (
        "--bg:", "--surface:", "--text:", "--accent:",
        "--border:", "--danger:", "--success:",
    )
    for theme in ("forest", "matrix", "slate", "parchment"):
        # Find the block beginning with the theme selector and ending
        # at the next "}".  Crude but enough to assert presence.
        marker = f"[data-theme=\"{theme}\"]"
        idx = css.find(marker)
        assert idx >= 0, f"missing theme block: {theme}"
        block = css[idx: idx + 2500]
        for tok in REQUIRED:
            assert tok in block, f"theme {theme!r} missing token {tok!r}"


def test_bootstrap_script_present_in_authed_layout(client):
    """The head bootstrap is what prevents FOUC.  Drop it and every
    page flashes the default palette before the user's pick lands.
    Pin it in the authenticated layout."""
    r = client.get("/home")
    assert r.status_code == 200, r.status_code
    body = r.text
    assert "localStorage.getItem('stash-theme')" in body
    assert "data-theme" in body


def test_bootstrap_script_present_in_public_layout(client):
    """Same anti-FOUC guarantee for the public /about pages, which
    intentionally don't extend base.html (no actor state)."""
    r = client.get("/about")
    assert r.status_code == 200
    body = r.text
    assert "localStorage.getItem('stash-theme')" in body


def test_theme_picker_renders_on_usage(client):
    """/usage hosts the picker — four swatches, one button each.  If
    any palette goes missing, users can't pick it."""
    r = client.get("/usage")
    assert r.status_code == 200, r.status_code
    body = r.text
    for slug in ("forest", "matrix", "slate", "parchment"):
        assert f'data-theme-pick="{slug}"' in body, \
            f"theme picker missing {slug!r} swatch"
    assert "theme-picker" in body
    assert 'id="appearance"' in body


def test_legacy_var_aliases_still_resolve():
    """Older selectors reference ``--panel`` / ``--panel-2`` /
    ``--muted``.  Forest theme aliases them to the new semantic
    tokens so the refactor doesn't break unrelated rules."""
    css = STYLE.read_text(encoding="utf-8")
    assert "--panel:        var(--surface);" in css
    assert "--panel-2:      var(--surface-2);" in css
    assert "--muted:        var(--text-muted);" in css
