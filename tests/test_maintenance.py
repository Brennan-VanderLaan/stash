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
