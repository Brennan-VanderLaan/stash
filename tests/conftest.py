import base64
import importlib
import secrets
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# Default test actor: every TestClient session is authenticated as this
# email, which is the sole maintainer of a Test tenant created in the
# `client` fixture.  Tests that need a different actor (a second tenant,
# a readonly member, an unrecognised email expecting a 403) construct a
# TestClient with their own headers explicitly.
TEST_EMAIL = "test@example.com"
TEST_TENANT_NAME = "Test"


@pytest.fixture(autouse=True)
def _test_kek(monkeypatch):
    """Every test gets a fresh KEK env var so app.py can import.  Tests
    that reload `app` directly (i.e. don't use the `client` fixture)
    inherit this; the DEK cache is also cleared so a tenant's wrapped
    DEK from a previous test can't accidentally decrypt under a new
    KEK."""
    monkeypatch.setenv(
        "STASH_KEK", base64.b64encode(secrets.token_bytes(32)).decode(),
    )
    # Disable the bearer-over-HTTPS guard for the whole suite by
    # default — TestClient runs against ``http://testserver``, so
    # the guard would 401 every bearer-auth test otherwise.  Tests
    # that specifically exercise the guard set this back to "true"
    # on a per-test basis.
    monkeypatch.setenv("STASH_REQUIRE_HTTPS_TOKENS", "false")
    # Default a Gemini API key so AI-surface routes don't 503 in
    # tests that monkeypatch the upstream call.  Tests that want
    # to exercise the missing-key path delenv it explicitly.
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    import vault
    vault.clear_dek_cache()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STASH_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))
    # The actor middleware looks up STASH_OPERATOR_EMAILS; clear it for
    # tests so we don't accidentally mark TEST_EMAIL as an operator.
    monkeypatch.delenv("STASH_OPERATOR_EMAILS", raising=False)
    # TestClient runs over plain http://testserver — disable the
    # bearer-over-HTTPS guard for tests that don't bother stamping
    # X-Forwarded-Proto: https on every request.  Tests that
    # specifically exercise the HTTPS guard re-set this themselves.
    monkeypatch.setenv("STASH_REQUIRE_HTTPS_TOKENS", "false")

    if "app" in sys.modules:
        del sys.modules["app"]
    import app as app_module
    importlib.reload(app_module)
    import vault
    vault.clear_dek_cache()

    # Stand up a Test tenant + maintainer member for the default actor.
    # The migration only auto-creates a Personal tenant when there's
    # pre-multi-tenancy data to fold in; tests start with an empty DB
    # and need their tenant created explicitly.
    with app_module.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES (?, 'pro')",
            (TEST_TENANT_NAME,),
        )
        tenant_id = cur.lastrowid
        conn.execute(
            "INSERT INTO tenant_members "
            "(tenant_id, email, role, joined_at) "
            "VALUES (?, ?, 'maintainer', CURRENT_TIMESTAMP)",
            (tenant_id, TEST_EMAIL),
        )
        conn.commit()

    with TestClient(
        app_module.app,
        # Inject the actor header on every request so the new
        # current_actor middleware resolves to TEST_EMAIL → Test
        # tenant → maintainer without each test having to set it
        # by hand.
        headers={"X-Forwarded-Email": TEST_EMAIL},
    ) as c:
        c.app_module = app_module
        c.test_email = TEST_EMAIL
        c.test_tenant_id = tenant_id
        yield c


def _promote_to_operator(c):
    """Promote the test actor to operator at runtime by
    monkey-poking ``_OPERATOR_EMAILS`` on the live app module.
    Used by the ``operator_client`` fixture below; also exposed
    so a single test can opt in mid-flight when needed.

    Why this and not ``monkeypatch.setenv``: ``_OPERATOR_EMAILS``
    is a module-level ``frozenset`` constructed at import time,
    so re-setting the env var after import is a no-op.  Direct
    replacement of the constant is the simplest path to
    operator behaviour in a test that already has a running
    app + tenant."""
    c.app_module._OPERATOR_EMAILS = frozenset({c.test_email.lower()})


@pytest.fixture
def as_operator(client):
    """Side-effect fixture: promote the default ``client``'s
    actor email to operator before the test runs.  Use as an
    extra parameter alongside ``client``:

        def test_admin_thing(client, as_operator):
            client.post("/admin/maintenance/cleanup")

    Body code keeps using ``client`` — the fixture just patches
    ``_OPERATOR_EMAILS`` so middleware resolves the existing
    actor with ``is_operator=True``.  Avoids the
    rename-every-reference churn of a wrapper-fixture pattern."""
    _promote_to_operator(client)
    yield
