"""Phase 8 — B2 (S3-compatible) upload of per-tenant backups.

Tonight's slice ships the upload helper + the manual /admin
trigger; the nightly scheduler comes with the cron-decision later
(roadmap markers in spec.md).

Tests stub out the boto3 client via ``dao.backups._B2_CLIENT_FACTORY``
so the suite doesn't need real B2 credentials and doesn't talk to
the network.  The stub records every put_object call so we can
assert on key shape, body bytes, content-type, and metadata.
"""

from __future__ import annotations

import base64
import importlib
import json
import secrets
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── Stub client ─────────────────────────────────────────────────────


class _RecordingS3:
    """Minimal stand-in for the boto3 S3 client surface we use.
    Records put_object calls so tests can assert on key + body +
    metadata without talking to B2."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def put_object(self, *, Bucket, Key, Body, ContentType=None,
                   Metadata=None):  # noqa: N803 — boto3 kwargs are PascalCase
        self.calls.append({
            "bucket": Bucket,
            "key": Key,
            "body": Body,
            "content_type": ContentType,
            "metadata": Metadata or {},
        })


def _bootstrap(tmp_path, monkeypatch, *, with_b2=True):
    """Spin up an empty stash with one tenant + one operator.
    When ``with_b2``, sets the four B2_* env vars + monkeypatches
    the boto3 factory to the recording stub."""
    monkeypatch.setenv("STASH_DB", str(tmp_path / "stash.db"))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.setenv(
        "STASH_KEK",
        base64.b64encode(secrets.token_bytes(32)).decode(),
    )
    monkeypatch.setenv("STASH_OPERATOR_EMAILS", "op@example.com")
    if with_b2:
        monkeypatch.setenv("B2_KEY_ID", "kid")
        monkeypatch.setenv("B2_APPLICATION_KEY", "ksecret")
        monkeypatch.setenv("B2_ENDPOINT", "https://s3.test.example")
        monkeypatch.setenv("B2_BUCKET", "stash-test-bucket")
    else:
        for v in ("B2_KEY_ID", "B2_APPLICATION_KEY",
                  "B2_ENDPOINT", "B2_BUCKET"):
            monkeypatch.delenv(v, raising=False)
    if "app" in sys.modules:
        del sys.modules["app"]
    import app as app_module
    importlib.reload(app_module)
    import vault
    vault.clear_dek_cache()

    with app_module.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('Movers', 'pro')"
        )
        tid = cur.lastrowid
        conn.execute(
            "INSERT INTO tenant_members "
            "(tenant_id, email, role, joined_at) "
            "VALUES (?, 'me@example.com', 'maintainer', "
            " CURRENT_TIMESTAMP)",
            (tid,),
        )
        conn.execute(
            "INSERT INTO boxes (name, location, notes, tenant_id) "
            "VALUES ('Kitchen', 'A', '', ?)",
            (tid,),
        )
        conn.commit()

    # Make a per-tenant uploads dir with one file so the zip has
    # something to carry.
    udir = Path(tmp_path / "uploads" / str(tid))
    udir.mkdir(parents=True, exist_ok=True)
    (udir / "demo.jpg").write_bytes(b"ciphertext-demo")

    stub = _RecordingS3()
    if with_b2:
        from dao import backups as dao_backups
        monkeypatch.setattr(dao_backups, "_B2_CLIENT_FACTORY", lambda: stub)

    return app_module, tid, stub


# ── Configuration gating ────────────────────────────────────────────


def test_admin_dashboard_hides_button_when_b2_unset(tmp_path, monkeypatch):
    """When the env vars aren't set, the /admin page renders without
    the Upload-to-B2 form so the operator doesn't click into a 503."""
    app_mod, tid, _stub = _bootstrap(tmp_path, monkeypatch, with_b2=False)
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": "op@example.com"}) as c:
        r = c.get("/admin")
    assert "B2 not configured" in r.text
    assert "Upload to B2" not in r.text


def test_admin_dashboard_shows_button_when_b2_set(tmp_path, monkeypatch):
    app_mod, tid, _stub = _bootstrap(tmp_path, monkeypatch)
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": "op@example.com"}) as c:
        r = c.get("/admin")
    assert "Upload to B2" in r.text


def test_b2_upload_503s_when_unconfigured(tmp_path, monkeypatch):
    """The POST surface returns a clean 503 instead of a 500
    when called against an unconfigured deployment."""
    app_mod, tid, _stub = _bootstrap(tmp_path, monkeypatch, with_b2=False)
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": "op@example.com"}) as c:
        r = c.post(f"/admin/tenants/{tid}/backup", follow_redirects=False)
    assert r.status_code == 503
    assert "B2" in r.text


# ── Upload happy path ───────────────────────────────────────────────


def test_b2_upload_puts_object_at_expected_key(tmp_path, monkeypatch):
    """Operator triggers the upload from /admin; the stub records
    one put_object call keyed at ``<tid>/<YYYY-MM-DD>.zip`` in the
    configured bucket."""
    app_mod, tid, stub = _bootstrap(tmp_path, monkeypatch)
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": "op@example.com"}) as c:
        r = c.post(f"/admin/tenants/{tid}/backup", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin?")
    assert "backup_status=" in r.headers["location"]
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["bucket"] == "stash-test-bucket"
    # Key is "<tid>/YYYY-MM-DD.zip".
    parts = call["key"].split("/")
    assert parts[0] == str(tid)
    assert parts[1].endswith(".zip")
    # Body decodes as a zip.
    import io as _io
    import zipfile
    with zipfile.ZipFile(_io.BytesIO(call["body"]), "r") as zf:
        assert "stash.db" in zf.namelist()
        assert "manifest.json" in zf.namelist()
    assert call["content_type"] == "application/zip"
    # B2 object metadata carries provenance.
    assert call["metadata"]["stash-tenant-id"] == str(tid)
    assert call["metadata"]["stash-exported-by"] == "op@example.com"
    assert call["metadata"]["stash-format-version"] == "1"
    assert call["metadata"]["stash-zip-sha256"]


def test_b2_upload_audits(tmp_path, monkeypatch):
    """audit_log gets a backup.b2_upload entry with the upload
    summary so an operator can later see who uploaded what + when."""
    app_mod, tid, _stub = _bootstrap(tmp_path, monkeypatch)
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": "op@example.com"}) as c:
        c.post(f"/admin/tenants/{tid}/backup", follow_redirects=False)
    with app_mod.db() as conn:
        rows = conn.execute(
            "SELECT actor_email, action, target_id, metadata_json "
            "FROM audit_log WHERE action = 'backup.b2_upload'"
        ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["actor_email"] == "op@example.com"
    assert row["target_id"] == tid
    meta = json.loads(row["metadata_json"])
    assert meta["bucket"] == "stash-test-bucket"
    assert meta["sha256"]
    assert meta["size"] > 0


def test_b2_upload_records_telemetry(tmp_path, monkeypatch):
    """A successful upload writes a backup_bytes usage_event so the
    /usage page eventually reflects DR storage as part of the
    cost-transparency block (phase 13)."""
    app_mod, tid, _stub = _bootstrap(tmp_path, monkeypatch)
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": "op@example.com"}) as c:
        c.post(f"/admin/tenants/{tid}/backup", follow_redirects=False)
    with app_mod.db() as conn:
        row = conn.execute(
            "SELECT surface, kind, units FROM usage_events "
            "WHERE tenant_id = ? AND kind = 'backup_bytes'",
            (tid,),
        ).fetchone()
    assert row is not None
    assert row["surface"] == "backup"
    assert row["units"] > 0


def test_b2_upload_404s_for_non_operator(tmp_path, monkeypatch):
    """Tenant maintainers (no operator flag) can't trigger the
    /admin path — same opacity rule as the rest of /admin (404,
    not 403, so the surface stays opaque)."""
    app_mod, tid, _stub = _bootstrap(tmp_path, monkeypatch)
    with TestClient(app_mod.app, headers={"X-Forwarded-Email": "me@example.com"}) as c:
        r = c.post(f"/admin/tenants/{tid}/backup", follow_redirects=False)
    assert r.status_code == 404
