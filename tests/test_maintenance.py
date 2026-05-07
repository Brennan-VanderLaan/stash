"""Tests for file-leak fixes and the maintenance endpoints (export + orphan cleanup)."""

import io
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from vision import DetectedItem


def _fake_jpg() -> bytes:
    """A decodable JPEG (save_photo_bytes re-encodes; raw b'x' would fall through)."""
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), color=(200, 50, 50)).save(buf, format="JPEG")
    return buf.getvalue()


def _uploads(client) -> Path:
    return Path(client.app_module.UPLOAD_DIR)


def _ingest_and_assign(client):
    client.post("/boxes", data={"name": "Box"})
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="thing", description="d", bbox=[0, 0, 500, 500]),
    ]):
        client.post("/ingest", files={"photos": ("p.jpg", io.BytesIO(_fake_jpg()), "image/jpeg")})
    client.post("/queue/1/assign", data={"box_id": "1", "name": "thing"})


# ── Leak fixes ───────────────────────────────────────────────────────

def test_recrop_deletes_orphaned_old_crop(client):
    """Cropping twice should leave only source + current crop on disk."""
    _ingest_and_assign(client)
    with client.app_module.db() as conn:
        row = conn.execute("SELECT photo, source_photo FROM items WHERE id = 1").fetchone()
    first_crop = row["photo"]
    source = row["source_photo"]
    assert first_crop != source
    assert (_uploads(client) / first_crop).exists()

    # Re-crop to a different region — old crop file should be cleaned up
    client.post("/items/1/recrop", data={
        "crop_y_min": "500", "crop_x_min": "500",
        "crop_y_max": "1000", "crop_x_max": "1000",
    })
    assert not (_uploads(client) / first_crop).exists(), "old crop file leaked"
    with client.app_module.db() as conn:
        row2 = conn.execute("SELECT photo, source_photo FROM items WHERE id = 1").fetchone()
    assert (_uploads(client) / row2["photo"]).exists()
    assert (_uploads(client) / row2["source_photo"]).exists()


def test_recrop_revert_does_not_delete_source(client):
    """Reverting sets photo=source; source file must remain (it's now also photo)."""
    _ingest_and_assign(client)
    with client.app_module.db() as conn:
        row = conn.execute("SELECT photo, source_photo FROM items WHERE id = 1").fetchone()
    old_crop, source = row["photo"], row["source_photo"]

    client.post("/items/1/recrop", data={"skip_crop": "1"})
    assert (_uploads(client) / source).exists()
    # The previous crop is orphaned and should be cleaned
    assert not (_uploads(client) / old_crop).exists()


def test_replace_photo_deletes_old_files(client):
    """Replacing a photo removes the old file when nothing else references it."""
    client.post("/boxes", data={"name": "Box"})
    client.post(
        "/boxes/1/items",
        data={"name": "mug"},
        files={"photo": ("p.jpg", io.BytesIO(_fake_jpg()), "image/jpeg")},
    )
    with client.app_module.db() as conn:
        old = conn.execute("SELECT photo FROM items WHERE id = 1").fetchone()["photo"]
    assert (_uploads(client) / old).exists()

    client.post(
        "/items/1/replace-photo",
        files={"photo": ("new.jpg", io.BytesIO(_fake_jpg()), "image/jpeg")},
    )
    assert not (_uploads(client) / old).exists(), "old photo leaked on replace"
    with client.app_module.db() as conn:
        new = conn.execute("SELECT photo FROM items WHERE id = 1").fetchone()["photo"]
    assert (_uploads(client) / new).exists()


# ── Orphan sweep ─────────────────────────────────────────────────────

def test_cleanup_removes_unreferenced_files_only(client):
    _ingest_and_assign(client)
    # Drop an unrelated file into uploads/
    stray = _uploads(client) / "stray-orphan.jpg"
    stray.write_bytes(b"junk")

    # Grab the set of referenced files before cleanup
    with client.app_module.db() as conn:
        referenced = {
            r[0] for r in conn.execute(
                "SELECT photo FROM items UNION SELECT source_photo FROM items "
                "UNION SELECT photo FROM pending_items UNION SELECT photo FROM ingest_jobs"
            ).fetchall() if r[0]
        }

    r = client.post("/maintenance/cleanup", follow_redirects=False)
    assert r.status_code == 303
    assert "cleaned=1" in r.headers["location"]
    assert not stray.exists()
    for name in referenced:
        assert (_uploads(client) / name).exists(), f"cleanup deleted referenced file {name}"


def test_maintenance_page_reports_orphan_count(client):
    _ingest_and_assign(client)
    (_uploads(client) / "orphan1.jpg").write_bytes(b"x")
    (_uploads(client) / "orphan2.jpg").write_bytes(b"x")
    page = client.get("/maintenance").text
    assert "Orphaned" in page
    # Two stray files plus anything already orphaned — just assert >= 2
    assert "Clean up orphan files" in page


# ── Export ───────────────────────────────────────────────────────────

def test_export_zip_contains_db_and_referenced_uploads(client):
    _ingest_and_assign(client)
    r = client.get("/maintenance/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "stash-backup-" in r.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
    assert "stash.db" in names
    with client.app_module.db() as conn:
        referenced = {
            r[0] for r in conn.execute(
                "SELECT photo FROM items UNION SELECT source_photo FROM items "
                "UNION SELECT photo FROM pending_items UNION SELECT photo FROM ingest_jobs"
            ).fetchall() if r[0]
        }
    for name in referenced:
        assert f"uploads/{name}" in names, f"missing {name} from export"


def test_export_excludes_orphan_files(client):
    _ingest_and_assign(client)
    (_uploads(client) / "orphan.jpg").write_bytes(b"junk")
    r = client.get("/maintenance/export")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
    assert "uploads/orphan.jpg" not in names


# ── Import ───────────────────────────────────────────────────────────

def test_import_zip_round_trip_replaces_db_and_uploads(client):
    """Export from a populated state, wipe, re-import — all data comes back."""
    _ingest_and_assign(client)
    backup = client.get("/maintenance/export").content

    # Wipe state: drop the assigned item and remove its upload files
    with client.app_module.db() as conn:
        photos = [r[0] for r in conn.execute(
            "SELECT photo FROM items UNION SELECT source_photo FROM items"
        ).fetchall() if r[0]]
        conn.execute("DELETE FROM items")
        conn.execute("DELETE FROM boxes")
        conn.commit()
    for p in photos:
        try:
            (_uploads(client) / p).unlink()
        except FileNotFoundError:
            pass

    r = client.post(
        "/maintenance/import",
        files={"file": ("backup.zip", io.BytesIO(backup), "application/zip")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "imported=1" in r.headers["location"]

    with client.app_module.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM boxes").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        photos = [r[0] for r in conn.execute("SELECT photo FROM items").fetchall()]
    for p in photos:
        assert (_uploads(client) / p).exists(), f"upload {p} not restored"


def test_import_raw_db_file_replaces_db(client):
    """Uploading just a .db file (no zip wrapper) replaces the running DB."""
    # Build a known-good DB out of band using the running app's schema
    client.post("/boxes", data={"name": "Original"})

    # Snapshot the DB to bytes (like a user's local stash.db)
    db_bytes = client.app_module.DB_PATH.read_bytes()

    # Mutate state so we can confirm replacement
    client.post("/boxes", data={"name": "Should be wiped"})
    with client.app_module.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM boxes").fetchone()[0] == 2

    r = client.post(
        "/maintenance/import",
        files={"file": ("stash.db", io.BytesIO(db_bytes), "application/octet-stream")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with client.app_module.db() as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM boxes").fetchall()]
    assert names == ["Original"]


def test_import_creates_backup_of_existing_db(client):
    """The current DB is preserved as stash.db.bak-<timestamp> before replacement."""
    client.post("/boxes", data={"name": "Pre-import"})
    db_bytes = client.app_module.DB_PATH.read_bytes()

    db_path = client.app_module.DB_PATH
    backups_before = list(db_path.parent.glob(f"{db_path.name}.bak-*"))

    client.post(
        "/maintenance/import",
        files={"file": ("stash.db", io.BytesIO(db_bytes), "application/octet-stream")},
    )

    backups_after = list(db_path.parent.glob(f"{db_path.name}.bak-*"))
    assert len(backups_after) == len(backups_before) + 1


def test_export_includes_floorplans_and_background_art(client):
    """Pin coverage for every file-bearing column beyond items.photo: a
    location floorplan, a floor floorplan, and a box's generated
    background art must all ride along in the backup zip."""
    import secrets

    _ingest_and_assign(client)

    # 1. Background art on the box (the bytes are written directly to disk
    #    by the /generate-art handler; mirror that here without invoking
    #    the model).
    art_name = f"art-{secrets.token_hex(8)}.jpg"
    (_uploads(client) / art_name).write_bytes(_fake_jpg())

    # 2. Floorplan on a location.
    loc_floorplan = f"{secrets.token_hex(8)}.jpg"
    (_uploads(client) / loc_floorplan).write_bytes(_fake_jpg())

    # 3. Floorplan on a floor (multi-floor support added after the original
    #    backup tests were written).
    floor_floorplan = f"{secrets.token_hex(8)}.jpg"
    (_uploads(client) / floor_floorplan).write_bytes(_fake_jpg())

    with client.app_module.db() as conn:
        conn.execute("UPDATE boxes SET background_art = ? WHERE id = 1", (art_name,))
        cur = conn.execute(
            "INSERT INTO locations (name, floorplan) VALUES ('home', ?)",
            (loc_floorplan,),
        )
        loc_id = cur.lastrowid
        conn.execute(
            "INSERT INTO floors (location_id, name, floorplan, sort_order) "
            "VALUES (?, 'main', ?, 0)",
            (loc_id, floor_floorplan),
        )
        conn.commit()

    r = client.get("/maintenance/export")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())

    for expected in (art_name, loc_floorplan, floor_floorplan):
        assert f"uploads/{expected}" in names, f"{expected} missing from export"


def test_import_round_trip_restores_floorplans_and_background_art(client):
    """Round-trip: backup → wipe → restore brings back the floorplan and
    background-art files alongside the DB rows that point at them."""
    import secrets

    _ingest_and_assign(client)

    art_name = f"art-{secrets.token_hex(8)}.jpg"
    (_uploads(client) / art_name).write_bytes(_fake_jpg())
    loc_floorplan = f"{secrets.token_hex(8)}.jpg"
    (_uploads(client) / loc_floorplan).write_bytes(_fake_jpg())
    floor_floorplan = f"{secrets.token_hex(8)}.jpg"
    (_uploads(client) / floor_floorplan).write_bytes(_fake_jpg())

    with client.app_module.db() as conn:
        conn.execute("UPDATE boxes SET background_art = ? WHERE id = 1", (art_name,))
        cur = conn.execute(
            "INSERT INTO locations (name, floorplan) VALUES ('home', ?)",
            (loc_floorplan,),
        )
        loc_id = cur.lastrowid
        conn.execute(
            "INSERT INTO floors (location_id, name, floorplan, sort_order) "
            "VALUES (?, 'main', ?, 0)",
            (loc_id, floor_floorplan),
        )
        conn.commit()

    backup = client.get("/maintenance/export").content

    # Wipe DB rows + the files on disk so the restore has actual work to do.
    with client.app_module.db() as conn:
        conn.execute("UPDATE boxes SET background_art = NULL")
        conn.execute("DELETE FROM floors")
        conn.execute("DELETE FROM locations")
        conn.commit()
    for name in (art_name, loc_floorplan, floor_floorplan):
        try:
            (_uploads(client) / name).unlink()
        except FileNotFoundError:
            pass

    r = client.post(
        "/maintenance/import",
        files={"file": ("backup.zip", io.BytesIO(backup), "application/zip")},
        follow_redirects=False,
    )
    assert r.status_code == 303

    # All three files come back on disk, and the DB rows pointing at them
    # are restored too.
    for name in (art_name, loc_floorplan, floor_floorplan):
        assert (_uploads(client) / name).exists(), f"{name} not restored"

    with client.app_module.db() as conn:
        assert conn.execute(
            "SELECT background_art FROM boxes WHERE id = 1"
        ).fetchone()["background_art"] == art_name
        assert conn.execute(
            "SELECT floorplan FROM locations WHERE name = 'home'"
        ).fetchone()["floorplan"] == loc_floorplan
        assert conn.execute(
            "SELECT floorplan FROM floors WHERE name = 'main'"
        ).fetchone()["floorplan"] == floor_floorplan


def test_import_rejects_non_sqlite_file(client):
    r = client.post(
        "/maintenance/import",
        files={"file": ("evil.txt", io.BytesIO(b"not a database"), "text/plain")},
    )
    assert r.status_code == 400


def test_import_rejects_zip_without_stash_db(client):
    """A zip that doesn't carry stash.db is not a stash backup."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hello")
    r = client.post(
        "/maintenance/import",
        files={"file": ("bad.zip", io.BytesIO(buf.getvalue()), "application/zip")},
    )
    assert r.status_code == 400


def test_import_rejects_invalid_sqlite_with_correct_header(client):
    """Bytes that *start with* the SQLite header but aren't a real DB still get rejected."""
    fake = b"SQLite format 3\x00" + b"\x00" * 4096
    r = client.post(
        "/maintenance/import",
        files={"file": ("fake.db", io.BytesIO(fake), "application/octet-stream")},
    )
    assert r.status_code == 400


def test_import_zip_larger_than_photo_upload_limit(client):
    """Backups can exceed MAX_UPLOAD_BYTES (the photo cap) — only MAX_IMPORT_BYTES
    should bound them. Confirms the streaming path doesn't reuse the wrong limit."""
    client.post("/boxes", data={"name": "Box"})
    db_bytes = client.app_module.DB_PATH.read_bytes()

    # Pad the zip with an uncompressible blob bigger than the per-photo cap so we
    # exercise the streaming path. ZIP_STORED means the bytes hit disk uncompressed.
    padding_size = client.app_module.MAX_UPLOAD_BYTES + 1024 * 1024
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("stash.db", db_bytes)
        zf.writestr("padding.bin", b"\x00" * padding_size)

    r = client.post(
        "/maintenance/import",
        files={"file": ("backup.zip", io.BytesIO(buf.getvalue()), "application/zip")},
        follow_redirects=False,
    )
    assert r.status_code == 303, f"unexpected: {r.status_code} {r.text[:300]}"


def test_import_rejects_uploads_above_import_limit(client, monkeypatch):
    """A file beyond MAX_IMPORT_BYTES must still 413, not crash mid-stream."""
    monkeypatch.setattr(client.app_module, "MAX_IMPORT_BYTES", 4096)
    payload = b"SQLite format 3\x00" + b"x" * 8192
    r = client.post(
        "/maintenance/import",
        files={"file": ("big.db", io.BytesIO(payload), "application/octet-stream")},
    )
    assert r.status_code == 413


def test_version_endpoint_returns_running_version(client):
    """The maintenance page polls this to detect a successful watchtower update."""
    r = client.get("/maintenance/version")
    assert r.status_code == 200
    payload = r.json()
    assert "version" in payload
    assert "git_sha" in payload
    assert payload["version"] == client.app_module.VERSION


def test_import_zip_skips_path_traversal_entries(client):
    """A malicious zip with `uploads/../evil` must not write outside UPLOAD_DIR."""
    # Build a minimal valid backup zip
    client.post("/boxes", data={"name": "Box"})
    db_bytes = client.app_module.DB_PATH.read_bytes()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("stash.db", db_bytes)
        zf.writestr("uploads/../escaped.txt", b"pwn")
        zf.writestr("uploads/legit.jpg", _fake_jpg())

    r = client.post(
        "/maintenance/import",
        files={"file": ("backup.zip", io.BytesIO(buf.getvalue()), "application/zip")},
    )
    assert r.status_code == 200  # followed redirect to /maintenance
    uploads = _uploads(client)
    assert (uploads / "legit.jpg").exists()
    assert not (uploads.parent / "escaped.txt").exists()
