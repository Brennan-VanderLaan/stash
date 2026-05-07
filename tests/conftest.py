import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _bypass_email_gate(monkeypatch):
    """Tests hit the app directly without oauth2-proxy in front, so flip the
    gate off for the whole suite.  Applied to every test (autouse) so manual
    `importlib.reload(app)` callers get it too."""
    monkeypatch.setenv("FULLY_PUBLIC", "true")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STASH_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))
    if "app" in sys.modules:
        del sys.modules["app"]
    import app as app_module
    importlib.reload(app_module)
    with TestClient(app_module.app) as c:
        c.app_module = app_module
        yield c
