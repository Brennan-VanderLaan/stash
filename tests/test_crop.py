"""Tests for crop pipeline: Gemini bbox → Cropper.js coords → PIL crop → saved file.

Uses a 4-quadrant colored test image to verify the correct region is cropped:
  ┌────────────┬────────────┐
  │  RED       │  GREEN     │
  │  (0,0)     │  (200,0)   │
  │            │            │
  ├────────────┼────────────┤
  │  BLUE      │  YELLOW    │
  │  (0,200)   │  (200,200) │
  │            │            │
  └────────────┴────────────┘
"""

import io
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from vision import DetectedItem


RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
YELLOW = (255, 255, 0)


def _make_quadrant_image(size=400) -> bytes:
    """Create a 4-color quadrant image: TL=red, TR=green, BL=blue, BR=yellow."""
    img = Image.new("RGB", (size, size))
    half = size // 2
    for x in range(size):
        for y in range(size):
            if x < half and y < half:
                img.putpixel((x, y), RED)
            elif x >= half and y < half:
                img.putpixel((x, y), GREEN)
            elif x < half and y >= half:
                img.putpixel((x, y), BLUE)
            else:
                img.putpixel((x, y), YELLOW)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_exif_rotated_image() -> bytes:
    """Create a 400x200 image with EXIF orientation tag (rotate 90° CW).
    Raw pixels: wide landscape, but EXIF says display as tall portrait.
    Cropper.js will see a 200x400 portrait image after EXIF rotation."""
    import struct
    img = Image.new("RGB", (400, 200))
    # Left half red, right half green (in raw orientation)
    for x in range(400):
        for y in range(200):
            img.putpixel((x, y), RED if x < 200 else GREEN)
    # Add EXIF orientation = 6 (90° CW rotation)
    from PIL.ExifTags import Base as ExifBase
    import piexif
    exif_dict = {"0th": {piexif.ImageIFD.Orientation: 6}}
    exif_bytes = piexif.dump(exif_dict)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif_bytes)
    return buf.getvalue()


def _get_dominant_color(img: Image.Image) -> tuple:
    """Get the most common color in a small image."""
    colors = img.getcolors(maxcolors=img.width * img.height)
    return max(colors, key=lambda c: c[0])[1]


def _setup_pending(client, img_bytes, items, fmt="image/png"):
    """Helper: ingest an image and create pending items."""
    with patch("app.vision.detect_items", return_value=items):
        client.post(
            "/ingest",
            files={"photo": ("pile.png", io.BytesIO(img_bytes), fmt)},
        )


def _get_item_photo(client, item_id=1):
    """Helper: read an assigned item's photo as a PIL Image."""
    import sys
    app_mod = sys.modules["app"]
    with app_mod.db() as conn:
        row = conn.execute(
            "SELECT photo, source_photo FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    photo_path = Path(app_mod.UPLOAD_DIR) / row["photo"]
    return Image.open(photo_path), row


# ── Quadrant crop tests ──────────────────────────────────────────────

def test_crop_top_left_quadrant_is_red(client):
    """Cropping the top-left quadrant (0-500, 0-500) should yield a red image."""
    client.post("/boxes", data={"name": "Box"})
    _setup_pending(client, _make_quadrant_image(), [
        DetectedItem(name="thing", description="d", bbox=[0, 0, 500, 500]),
    ])
    client.post("/queue/1/assign", data={"box_id": "1", "name": "thing"})
    img, _ = _get_item_photo(client)
    assert img.width == 200 and img.height == 200
    assert _get_dominant_color(img) == RED


def test_crop_top_right_quadrant_is_green(client):
    client.post("/boxes", data={"name": "Box"})
    _setup_pending(client, _make_quadrant_image(), [
        DetectedItem(name="thing", description="d", bbox=[0, 500, 500, 1000]),
    ])
    client.post("/queue/1/assign", data={"box_id": "1", "name": "thing"})
    img, _ = _get_item_photo(client)
    assert _get_dominant_color(img) == GREEN


def test_crop_bottom_left_quadrant_is_blue(client):
    client.post("/boxes", data={"name": "Box"})
    _setup_pending(client, _make_quadrant_image(), [
        DetectedItem(name="thing", description="d", bbox=[500, 0, 1000, 500]),
    ])
    client.post("/queue/1/assign", data={"box_id": "1", "name": "thing"})
    img, _ = _get_item_photo(client)
    assert _get_dominant_color(img) == BLUE


def test_crop_bottom_right_quadrant_is_yellow(client):
    client.post("/boxes", data={"name": "Box"})
    _setup_pending(client, _make_quadrant_image(), [
        DetectedItem(name="thing", description="d", bbox=[500, 500, 1000, 1000]),
    ])
    client.post("/queue/1/assign", data={"box_id": "1", "name": "thing"})
    img, _ = _get_item_photo(client)
    assert _get_dominant_color(img) == YELLOW


# ── Manual crop overrides ────────────────────────────────────────────

def test_manual_crop_overrides_gemini_bbox(client):
    """Form-submitted crop coords override the stored Gemini bbox."""
    client.post("/boxes", data={"name": "Box"})
    # Gemini bbox says top-left (red), but user manually selects bottom-right (yellow)
    _setup_pending(client, _make_quadrant_image(), [
        DetectedItem(name="thing", description="d", bbox=[0, 0, 500, 500]),
    ])
    client.post("/queue/1/assign", data={
        "box_id": "1", "name": "thing",
        "crop_y_min": "500", "crop_x_min": "500",
        "crop_y_max": "1000", "crop_x_max": "1000",
    })
    img, _ = _get_item_photo(client)
    assert _get_dominant_color(img) == YELLOW


def test_skip_crop_preserves_full_image(client):
    """skip_crop=1 bypasses all crop logic."""
    client.post("/boxes", data={"name": "Box"})
    _setup_pending(client, _make_quadrant_image(), [
        DetectedItem(name="thing", description="d", bbox=[0, 0, 500, 500]),
    ])
    client.post("/queue/1/assign", data={
        "box_id": "1", "name": "thing", "skip_crop": "1",
    })
    img, row = _get_item_photo(client)
    assert img.width == 400 and img.height == 400
    assert row["photo"] == row["source_photo"]


# ── Source photo preservation ────────────────────────────────────────

def test_source_photo_preserved_after_crop(client):
    client.post("/boxes", data={"name": "Box"})
    _setup_pending(client, _make_quadrant_image(), [
        DetectedItem(name="thing", description="d", bbox=[0, 0, 500, 500]),
    ])
    client.post("/queue/1/assign", data={"box_id": "1", "name": "thing"})
    _, row = _get_item_photo(client)
    assert row["source_photo"] is not None
    assert row["photo"] != row["source_photo"]
    import sys
    assert (Path(sys.modules["app"].UPLOAD_DIR) / row["source_photo"]).exists()


def test_no_bbox_means_no_crop_and_source_equals_photo(client):
    client.post("/boxes", data={"name": "Box"})
    _setup_pending(client, _make_quadrant_image(), [
        DetectedItem(name="thing", description="d", bbox=None),
    ])
    client.post("/queue/1/assign", data={"box_id": "1", "name": "thing"})
    _, row = _get_item_photo(client)
    assert row["photo"] == row["source_photo"]


# ── Re-crop ──────────────────────────────────────────────────────────

def test_recrop_changes_crop_region(client):
    client.post("/boxes", data={"name": "Box"})
    _setup_pending(client, _make_quadrant_image(), [
        DetectedItem(name="thing", description="d", bbox=[0, 0, 500, 500]),
    ])
    client.post("/queue/1/assign", data={"box_id": "1", "name": "thing"})
    img1, _ = _get_item_photo(client)
    assert _get_dominant_color(img1) == RED

    # Re-crop to bottom-right (yellow)
    client.post("/items/1/recrop", data={
        "crop_y_min": "500", "crop_x_min": "500",
        "crop_y_max": "1000", "crop_x_max": "1000",
    })
    img2, _ = _get_item_photo(client)
    assert _get_dominant_color(img2) == YELLOW


def test_recrop_revert_restores_full_image(client):
    client.post("/boxes", data={"name": "Box"})
    _setup_pending(client, _make_quadrant_image(), [
        DetectedItem(name="thing", description="d", bbox=[0, 0, 500, 500]),
    ])
    client.post("/queue/1/assign", data={"box_id": "1", "name": "thing"})
    _, row_cropped = _get_item_photo(client)
    assert row_cropped["photo"] != row_cropped["source_photo"]

    # Revert via skip_crop
    client.post("/items/1/recrop", data={"skip_crop": "1"})
    img, row_reverted = _get_item_photo(client)
    assert row_reverted["photo"] == row_reverted["source_photo"]
    assert img.width == 400 and img.height == 400


def test_recrop_page_accessible_and_shows_source(client):
    client.post("/boxes", data={"name": "Box"})
    _setup_pending(client, _make_quadrant_image(), [
        DetectedItem(name="thing", description="d", bbox=[0, 0, 500, 500]),
    ])
    client.post("/queue/1/assign", data={"box_id": "1", "name": "thing"})
    page = client.get("/items/1/recrop").text
    assert "Adjust crop" in page
    assert "Current crop" in page
    assert "Revert to full image" in page


def test_revert_button_visible_on_cropped_items(client):
    client.post("/boxes", data={"name": "Box"})
    _setup_pending(client, _make_quadrant_image(), [
        DetectedItem(name="thing", description="d", bbox=[0, 0, 500, 500]),
    ])
    client.post("/queue/1/assign", data={"box_id": "1", "name": "thing"})
    page = client.get("/boxes/1").text
    assert "Revert to original" in page
    assert "Re-crop" in page


def test_revert_button_not_shown_when_uncropped(client):
    client.post("/boxes", data={"name": "Box"})
    _setup_pending(client, _make_quadrant_image(), [
        DetectedItem(name="thing", description="d", bbox=None),
    ])
    client.post("/queue/1/assign", data={"box_id": "1", "name": "thing"})
    page = client.get("/boxes/1").text
    assert "Revert to original" not in page


# ── EXIF rotation ────────────────────────────────────────────────────

def test_exif_rotated_image_crops_correctly(client):
    """Phone photos with EXIF rotation should crop to match what the user sees,
    not the raw pixel grid."""
    try:
        import piexif
    except ImportError:
        import pytest
        pytest.skip("piexif not installed")

    client.post("/boxes", data={"name": "Box"})
    img_bytes = _make_exif_rotated_image()
    # After EXIF orientation 6 (90° CW), 400x200 landscape → 200x400 portrait.
    # 90° CW: left→top, right→bottom. So: top=RED, bottom=GREEN.
    _setup_pending(client, img_bytes, [
        # Crop top half of the displayed portrait: should be RED
        DetectedItem(name="top", description="d", bbox=[0, 0, 500, 1000]),
    ], fmt="image/jpeg")
    client.post("/queue/1/assign", data={"box_id": "1", "name": "top"})
    img_top, _ = _get_item_photo(client)
    # Allow slight JPEG compression color drift (254 vs 255)
    r, g, b = _get_dominant_color(img_top)
    assert r > 200 and g < 50 and b < 50, f"Expected RED, got ({r},{g},{b})"

    # Also verify: without EXIF transpose, cropping the "top half" of the RAW
    # 400x200 landscape would give a mix of red+green (full-width horizontal strip).
    # The fact that we get pure red proves EXIF rotation is being applied.


# ── Ingest bbox storage ─────────────────────────────────────────────

def test_ingest_stores_bboxes_in_hidden_fields(client):
    _setup_pending(client, _make_quadrant_image(), [
        DetectedItem(name="spatula", description="wooden", bbox=[100, 200, 500, 800]),
        DetectedItem(name="mug", description="ceramic", bbox=None),
    ])
    queue = client.get("/queue").text
    assert "spatula" in queue
    assert "mug" in queue
    assert 'value="100"' in queue  # bbox y_min
    assert 'value="200"' in queue  # bbox x_min
