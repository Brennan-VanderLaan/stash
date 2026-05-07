"""Localization seam tests.

v1 ships English-only, so most of what we're checking here is that
the wiring doesn't break:

* `app._()` is the identity (NullTranslations is loaded).
* The Babel-driven `datetime` / `date` Jinja filters render.
* Templates that use `{{ _("...") }}` produce the source string.

A future locale lands as a `.po` file under `locale/` plus swapping
NullTranslations for `gettext.translation(...)`; these tests should
keep passing as long as the seam stays in place.
"""

from datetime import datetime


def test_gettext_identity_for_english_only(client):
    """`_()` passes source strings through unchanged in v1."""
    assert client.app_module._("Boxes") == "Boxes"
    assert client.app_module._("Sort queue · Stash") == "Sort queue · Stash"


def test_translations_object_is_null_translations_in_v1(client):
    """No real catalog is loaded yet; swapping in a real one is a
    one-line change in app.py."""
    import gettext as _gt
    assert isinstance(client.app_module._translations, _gt.NullTranslations)


def test_datetime_filter_formats_with_default_locale(client):
    """Babel renders the timestamp through the deployment-default
    locale (env STASH_DEFAULT_LOCALE, fallback 'en')."""
    fmt = client.app_module._format_datetime
    rendered = fmt(datetime(2026, 5, 7, 9, 30, 0))
    # English medium format is "May 7, 2026, 9:30:00 AM" or similar —
    # exact format varies by Babel version, so just check the content
    # we care about.
    assert "2026" in rendered
    assert "May" in rendered or "5" in rendered
    assert ("9:30" in rendered) or ("09:30" in rendered)


def test_datetime_filter_handles_iso_strings(client):
    """Sqlite hands us datetime columns as ISO strings; the filter
    accepts those as well as datetime objects."""
    fmt = client.app_module._format_datetime
    assert "2026" in fmt("2026-05-07 09:30:00")
    assert fmt(None) == ""
    assert fmt("") == ""


def test_template_renders_wrapped_strings(client):
    """The base layout's nav now flows through `_()`. The home page
    uses base.html; rendering it should still produce the
    English-source strings."""
    r = client.get("/")
    assert r.status_code == 200
    text = r.text
    # Header nav.
    assert ">Boxes</a>" in text
    assert ">Where</a>" in text
    assert ">Ingest</a>" in text
    assert ">Sort</a>" in text


def test_jinja_i18n_extension_is_installed(client):
    """The jinja2 i18n extension is wired up so `{% trans %}` works.
    Without this, templates using the trans tag would raise
    TemplateSyntaxError on first render."""
    env = client.app_module.templates.env
    extensions = env.extensions
    assert "jinja2.ext.InternationalizationExtension" in extensions \
        or "jinja2.ext.i18n" in extensions \
        or any("i18n" in k.lower() for k in extensions)
