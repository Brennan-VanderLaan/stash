"""Phase 7 — per-tenant backup at /usage/backup.

The zip shape:

* ``stash.db`` — SQLite file with the schema and only this tenant's
  rows.  Other tenants' rows are pruned via DELETE-then-VACUUM.
* ``uploads/{tenant_id}/<name>`` — every encrypted blob the tenant
  owns, traveling as ciphertext.
* ``manifest.json`` — version + tenant_id + row counts.

Tests verify the slice is clean (no other tenant's data leaks),
the manifest is well-formed, the encrypted blobs are present, and
audit_log gains a ``backup.export`` row each time a maintainer
downloads.
"""

from __future__ import annotations

import base64
import importlib
import io
import json
import secrets
import sqlite3
import sys
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _bootstrap_two_tenants(tmp_path, monkeypatch):
    """Two tenants with overlapping data so leakage tests have
    something to fail on.  T1 has a box + item; T2 has a different
    box + item.  Both have one upload-shaped file in their dir."""
    monkeypatch.setenv("STASH_DB", str(tmp_path / "stash.db"))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.setenv(
        "STASH_KEK",
        base64.b64encode(secrets.token_bytes(32)).decode(),
    )
    monkeypatch.delenv("STASH_OPERATOR_EMAILS", raising=False)
    if "app" in sys.modules:
        del sys.modules["app"]
    import app as app_module
    importlib.reload(app_module)
    import vault
    vault.clear_dek_cache()

    with app_module.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('Mine', 'pro')"
        )
        t1 = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('Theirs', 'pro')"
        )
        t2 = cur.lastrowid
        for tid, owner in ((t1, "me@example.com"),
                           (t2, "them@example.com")):
            conn.execute(
                "INSERT INTO tenant_members "
                "(tenant_id, email, role, joined_at) "
                "VALUES (?, ?, 'maintainer', CURRENT_TIMESTAMP)",
                (tid, owner),
            )
        # T1 box + item.
        conn.execute(
            "INSERT INTO boxes (id, name, location, notes, tenant_id) "
            "VALUES (1, 'Mine box', 'A', '', ?)",
            (t1,),
        )
        conn.execute(
            "INSERT INTO items (id, box_id, name, notes, tenant_id) "
            "VALUES (10, 1, 'Whisk', '', ?)",
            (t1,),
        )
        # T2 box + item.
        conn.execute(
            "INSERT INTO boxes (id, name, location, notes, tenant_id) "
            "VALUES (2, 'Theirs box', 'B', '', ?)",
            (t2,),
        )
        conn.execute(
            "INSERT INTO items (id, box_id, name, notes, tenant_id) "
            "VALUES (20, 2, 'Spatula', '', ?)",
            (t2,),
        )
        conn.commit()

    # Two upload files, one per tenant directory.
    uploads = Path(tmp_path / "uploads")
    (uploads / str(t1)).mkdir(parents=True, exist_ok=True)
    (uploads / str(t2)).mkdir(parents=True, exist_ok=True)
    (uploads / str(t1) / "mine.jpg").write_bytes(b"ciphertext-mine")
    (uploads / str(t2) / "theirs.jpg").write_bytes(b"ciphertext-theirs")

    return app_module, dict(t1=t1, t2=t2)


def _open_zip(zip_bytes: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(zip_bytes), "r")


def test_backup_zip_contains_only_my_tenants_rows(tmp_path, monkeypatch):
    """The pruned SQLite carries this tenant's rows only.  No
    cross-tenant leakage of the other tenant's box + item."""
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    headers = {"X-Forwarded-Email": "me@example.com"}
    with TestClient(app_mod.app, headers=headers) as c:
        r = c.get("/usage/backup")
        assert r.status_code == 200
        zip_bytes = r.content

    with _open_zip(zip_bytes) as zf:
        names = zf.namelist()
        assert "stash.db" in names
        assert "manifest.json" in names
        # Extract the DB to a temp file and inspect.
        db_bytes = zf.read("stash.db")

    db_path = tmp_path / "extracted.db"
    db_path.write_bytes(db_bytes)
    conn = sqlite3.connect(db_path)
    try:
        # Boxes: only the one belonging to T1.
        boxes = conn.execute("SELECT name, tenant_id FROM boxes").fetchall()
        assert boxes == [("Mine box", ids["t1"])]
        items = conn.execute("SELECT name, tenant_id FROM items").fetchall()
        assert items == [("Whisk", ids["t1"])]
        # Tenants table: only T1.
        tenants = conn.execute("SELECT id, name FROM tenants").fetchall()
        assert tenants == [(ids["t1"], "Mine")]
        # tenant_members: only the T1 member.
        members = conn.execute(
            "SELECT email, tenant_id FROM tenant_members"
        ).fetchall()
        assert members == [("me@example.com", ids["t1"])]
    finally:
        conn.close()


def test_backup_zip_contains_only_my_uploads(tmp_path, monkeypatch):
    """Per-tenant uploads dir is included; the other tenant's files
    are not."""
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    headers = {"X-Forwarded-Email": "me@example.com"}
    with TestClient(app_mod.app, headers=headers) as c:
        r = c.get("/usage/backup")
        assert r.status_code == 200
    with _open_zip(r.content) as zf:
        names = zf.namelist()
    assert f"uploads/{ids['t1']}/mine.jpg" in names
    # T2's file must not appear under any tenant prefix in the zip.
    assert not any("theirs.jpg" in n for n in names)
    assert not any(n.startswith(f"uploads/{ids['t2']}/") for n in names)


def test_backup_manifest_shape(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    headers = {"X-Forwarded-Email": "me@example.com"}
    with TestClient(app_mod.app, headers=headers) as c:
        r = c.get("/usage/backup")
    with _open_zip(r.content) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["format_version"] == 1
    assert manifest["tenant_id"] == ids["t1"]
    assert manifest["tenant_name"] == "Mine"
    assert manifest["exported_by"] == "me@example.com"
    # Row counts reflect what's actually in the zip.
    assert manifest["row_counts"]["boxes"] == 1
    assert manifest["row_counts"]["items"] == 1
    assert manifest["row_counts"]["tenant_members"] == 1
    assert manifest["uploads_count"] == 1
    assert manifest["uploads_bytes"] == len(b"ciphertext-mine")
    # Timestamp parses as ISO-8601.
    from datetime import datetime
    datetime.fromisoformat(manifest["exported_at"])


def test_backup_audit_log(tmp_path, monkeypatch):
    """Each download leaves a backup.export audit row so an operator
    can later see who pulled a backup and when."""
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    headers = {"X-Forwarded-Email": "me@example.com"}
    with TestClient(app_mod.app, headers=headers) as c:
        c.get("/usage/backup")
    with app_mod.db() as conn:
        rows = conn.execute(
            "SELECT actor_email, action, target_id, metadata_json "
            "FROM audit_log WHERE action = 'backup.export'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_email"] == "me@example.com"
    assert rows[0]["target_id"] == ids["t1"]
    meta = json.loads(rows[0]["metadata_json"])
    assert meta["uploads_count"] == 1


def test_backup_readonly_member_forbidden(tmp_path, monkeypatch):
    """A readonly member can't trigger a backup."""
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    with app_mod.db() as conn:
        conn.execute(
            "INSERT INTO tenant_members "
            "(tenant_id, email, role, joined_at) "
            "VALUES (?, 'guest@example.com', 'readonly', "
            " CURRENT_TIMESTAMP)",
            (ids["t1"],),
        )
        conn.commit()
    headers = {"X-Forwarded-Email": "guest@example.com"}
    with TestClient(app_mod.app, headers=headers) as c:
        r = c.get("/usage/backup", follow_redirects=False)
    assert r.status_code == 403


def test_backup_zip_db_passes_integrity_check(tmp_path, monkeypatch):
    """The pruned SQLite file is valid + passes integrity_check.
    A botched VACUUM or schema mismatch would fail this; same gate
    /maintenance/import already runs against the global zip."""
    app_mod, ids = _bootstrap_two_tenants(tmp_path, monkeypatch)
    headers = {"X-Forwarded-Email": "me@example.com"}
    with TestClient(app_mod.app, headers=headers) as c:
        r = c.get("/usage/backup")
    with _open_zip(r.content) as zf:
        db_bytes = zf.read("stash.db")
    db_path = tmp_path / "extracted.db"
    db_path.write_bytes(db_bytes)
    conn = sqlite3.connect(db_path)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    assert result[0] == "ok"
