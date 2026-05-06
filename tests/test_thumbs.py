"""Tests for the /thumbs/{name} downscaling endpoint and orphan handling."""

import io
from pathlib import Path

from PIL import Image


def _real_jpg(width: int = 1600, height: int = 1200) -> bytes:
    """A genuinely-decodable JPEG so PIL can process it through save_photo_bytes
    and the thumb pipeline. The fake b'x' bytes used elsewhere fall through
    save_photo_bytes' decode-failure branch and skip thumb pre-generation."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(40, 80, 120)).save(buf, format="JPEG")
    return buf.getvalue()


def _add_box_with_photo(client) -> str:
    """Create a box, attach an item with a real JPEG, and return the photo
    filename. Uses save_photo_bytes via the standard form path so the new
    upload pre-generates its thumb."""
    client.post("/boxes", data={"name": "Box"})
    client.post(
        "/boxes/1/items",
        data={"name": "thing"},
        files={"photo": ("p.jpg", io.BytesIO(_real_jpg()), "image/jpeg")},
    )
    with client.app_module.db() as conn:
        return conn.execute("SELECT photo FROM items WHERE id = 1").fetchone()[0]


# ── Pre-generation at upload time ────────────────────────────────────

def test_upload_pregenerates_thumb(client):
    photo = _add_box_with_photo(client)
    thumb = client.app_module._thumb_path(photo)
    assert thumb.exists(), "thumb should be written alongside the source"


def test_thumb_is_smaller_than_source(client):
    photo = _add_box_with_photo(client)
    src = client.app_module.UPLOAD_DIR / photo
    thumb = client.app_module._thumb_path(photo)
    # The source 1600×1200 JPEG re-encodes to 2048-capped (no resize); thumb
    # caps at THUMB_MAX_DIM (320). Bytes-on-disk should be much smaller.
    assert thumb.stat().st_size < src.stat().st_size, \
        f"thumb {thumb.stat().st_size} should be smaller than source {src.stat().st_size}"


def test_thumb_max_dim_respected(client):
    photo = _add_box_with_photo(client)
    thumb = client.app_module._thumb_path(photo)
    with Image.open(thumb) as img:
        max_dim = max(img.size)
    assert max_dim <= client.app_module.THUMB_MAX_DIM, \
        f"thumb longest side {max_dim} exceeds {client.app_module.THUMB_MAX_DIM}"


# ── /thumbs endpoint ─────────────────────────────────────────────────

def test_thumb_endpoint_serves_thumb_jpg(client):
    photo = _add_box_with_photo(client)
    r = client.get(f"/thumbs/{photo}")
    assert r.status_code == 200
    assert r.headers["content-type"] in ("image/jpeg", "image/jpg")
    # Cached aggressively — filenames are content-hashed so it's safe.
    assert "max-age=31536000" in r.headers.get("cache-control", "")


def test_thumb_endpoint_lazy_generates_for_existing_photos(client):
    """Pre-existing photos (from before this feature shipped) won't have a
    thumb file. Hitting the endpoint must generate one on demand."""
    photo = _add_box_with_photo(client)
    thumb = client.app_module._thumb_path(photo)
    thumb.unlink()  # simulate the pre-thumb-feature state
    assert not thumb.exists()
    r = client.get(f"/thumbs/{photo}")
    assert r.status_code == 200
    assert thumb.exists(), "endpoint should have created the thumb on demand"


def test_thumb_endpoint_404_for_missing_source(client):
    r = client.get("/thumbs/does_not_exist.jpg")
    assert r.status_code == 404


def test_thumb_endpoint_rejects_path_traversal(client):
    """The same defense as /uploads/{name} — refuse names outside the upload
    alphabet so '..' and weird unicode never get filesystem-resolved."""
    r = client.get("/thumbs/..%2Fapp.py")
    assert r.status_code == 404
    r = client.get("/thumbs/../app.py")
    assert r.status_code == 404


def test_thumb_endpoint_falls_back_to_source_on_decode_failure(client):
    """If the source file isn't decodable (e.g. one of the test fake-jpg
    fixtures stored verbatim), the endpoint must still serve *something*
    rather than 500ing — the caller falls back to the raw upload."""
    # Use the photo-replace path which writes raw bytes when PIL can't decode
    client.post("/boxes", data={"name": "Box"})
    client.post(
        "/boxes/1/items",
        data={"name": "thing"},
        files={"photo": ("p.jpg", io.BytesIO(b"x" * 100), "image/jpeg")},
    )
    with client.app_module.db() as conn:
        photo = conn.execute("SELECT photo FROM items WHERE id = 1").fetchone()[0]
    r = client.get(f"/thumbs/{photo}")
    assert r.status_code == 200, "should serve the source as a fallback"


# ── Orphan handling ─────────────────────────────────────────────────

def test_thumb_deleted_when_source_orphan_cleaned(client):
    photo = _add_box_with_photo(client)
    thumb = client.app_module._thumb_path(photo)
    assert (client.app_module.UPLOAD_DIR / photo).exists()
    assert thumb.exists()

    # Deleting the item drops the only reference → source + thumb both go.
    client.post("/items/1/delete")
    assert not (client.app_module.UPLOAD_DIR / photo).exists()
    assert not thumb.exists(), "thumb leaked after source orphan cleanup"


def test_maintenance_cleanup_keeps_referenced_thumbs(client):
    """The orphan sweep walks UPLOAD_DIR and deletes anything not in the
    referenced-uploads set. Thumbs need to be in that set or every cleanup
    nukes them."""
    photo = _add_box_with_photo(client)
    thumb = client.app_module._thumb_path(photo)
    assert thumb.exists()

    client.post("/maintenance/cleanup")
    assert thumb.exists(), "cleanup deleted a referenced photo's thumb"


def test_maintenance_cleanup_removes_orphaned_thumbs(client):
    """Conversely, a thumb whose source is gone (e.g. half-cleaned-up state
    from a crash) is itself an orphan and the sweep should remove it."""
    photo = _add_box_with_photo(client)
    src = client.app_module.UPLOAD_DIR / photo
    thumb = client.app_module._thumb_path(photo)

    # Simulate a half-clean state — delete the source from disk only,
    # without going through the endpoint, leaving the thumb behind. Then
    # also break the DB row so the sweep treats it as orphaned.
    src.unlink()
    with client.app_module.db() as conn:
        conn.execute("UPDATE items SET photo = NULL, source_photo = NULL WHERE id = 1")
        conn.commit()

    client.post("/maintenance/cleanup")
    assert not thumb.exists(), "orphan thumb survived the cleanup sweep"
