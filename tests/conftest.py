import importlib
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


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STASH_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))
    # The actor middleware looks up STASH_OPERATOR_EMAILS; clear it for
    # tests so we don't accidentally mark TEST_EMAIL as an operator.
    monkeypatch.delenv("STASH_OPERATOR_EMAILS", raising=False)

    if "app" in sys.modules:
        del sys.modules["app"]
    import app as app_module
    importlib.reload(app_module)

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
