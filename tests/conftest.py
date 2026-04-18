import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


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
