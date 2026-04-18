import io
from unittest.mock import patch
from PIL import Image

from vision import DetectedItem


def _make_test_image(w=200, h=200) -> bytes:
    """Create a simple test image."""
    img = Image.new("RGB", (w, h), color="red")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_ingest_stores_bboxes(client):
    client.post("/boxes", data={"name": "Box A"})
    detected = [
        DetectedItem(name="spatula", description="wooden", bbox=[100, 200, 500, 800]),
        DetectedItem(name="mug", description="ceramic", bbox=None),
    ]
    with patch("app.vision.detect_items", return_value=detected):
        client.post(
            "/ingest",
            files={"photo": ("pile.jpg", io.BytesIO(_make_test_image()), "image/jpeg")},
        )

    queue = client.get("/queue").text
    assert "spatula" in queue
    assert "mug" in queue
    # Bbox values should be in hidden form fields for the spatula item
    assert 'value="100"' in queue  # bbox_y_min
    assert 'value="200"' in queue  # bbox_x_min


def test_assign_crops_photo_when_bbox_present(client, tmp_path):
    client.post("/boxes", data={"name": "Kitchen"})
    test_img = _make_test_image(400, 400)

    detected = [
        DetectedItem(name="spatula", description="wooden", bbox=[0, 0, 500, 500]),
    ]
    with patch("app.vision.detect_items", return_value=detected):
        client.post(
            "/ingest",
            files={"photo": ("pile.jpg", io.BytesIO(test_img), "image/jpeg")},
        )

    # Assign — should auto-crop
    client.post("/queue/1/assign", data={"box_id": "1", "name": "spatula"})

    # The item in the box should have a DIFFERENT photo than the original
    # (a cropped version was saved)
    import sys
    app_mod = sys.modules["app"]
    with app_mod.db() as conn:
        item = conn.execute("SELECT photo FROM items WHERE id = 1").fetchone()
        pending_photos = conn.execute(
            "SELECT photo FROM ingest_jobs"
        ).fetchall()

    # Cropped photo should exist and be smaller than the original
    from pathlib import Path
    cropped_path = Path(app_mod.UPLOAD_DIR) / item["photo"]
    assert cropped_path.exists()
    cropped_img = Image.open(cropped_path)
    assert cropped_img.width == 200  # 500/1000 * 400
    assert cropped_img.height == 200


def test_assign_uses_original_when_no_bbox(client):
    client.post("/boxes", data={"name": "Kitchen"})
    detected = [
        DetectedItem(name="mug", description="ceramic", bbox=None),
    ]
    with patch("app.vision.detect_items", return_value=detected):
        client.post(
            "/ingest",
            files={"photo": ("pile.jpg", io.BytesIO(_make_test_image()), "image/jpeg")},
        )

    client.post("/queue/1/assign", data={"box_id": "1", "name": "mug"})

    import sys
    app_mod = sys.modules["app"]
    with app_mod.db() as conn:
        item = conn.execute("SELECT photo FROM items WHERE id = 1").fetchone()
        job = conn.execute("SELECT photo FROM ingest_jobs WHERE id = 1").fetchone()
    # Same photo — no crop
    assert item["photo"] == job["photo"]


def test_manual_crop_overrides_bbox(client, tmp_path):
    """Manually submitted crop coords take priority over DB bbox."""
    client.post("/boxes", data={"name": "Kitchen"})
    test_img = _make_test_image(400, 400)

    detected = [
        DetectedItem(name="spatula", description="wooden", bbox=[0, 0, 500, 500]),
    ]
    with patch("app.vision.detect_items", return_value=detected):
        client.post(
            "/ingest",
            files={"photo": ("pile.jpg", io.BytesIO(test_img), "image/jpeg")},
        )

    # Submit with manual crop that's different from the bbox
    client.post("/queue/1/assign", data={
        "box_id": "1", "name": "spatula",
        "crop_y_min": "0", "crop_x_min": "0",
        "crop_y_max": "250", "crop_x_max": "250",
    })

    import sys
    app_mod = sys.modules["app"]
    with app_mod.db() as conn:
        item = conn.execute("SELECT photo FROM items WHERE id = 1").fetchone()

    from pathlib import Path
    cropped_img = Image.open(Path(app_mod.UPLOAD_DIR) / item["photo"])
    # Manual crop: 250/1000 * 400 = 100px
    assert cropped_img.width == 100
    assert cropped_img.height == 100


def test_skip_crop_uses_full_image(client):
    """skip_crop=1 bypasses DB bbox — uses full source."""
    client.post("/boxes", data={"name": "Kitchen"})
    detected = [
        DetectedItem(name="spatula", description="wooden", bbox=[0, 0, 500, 500]),
    ]
    with patch("app.vision.detect_items", return_value=detected):
        client.post(
            "/ingest",
            files={"photo": ("pile.jpg", io.BytesIO(_make_test_image(200, 200)), "image/jpeg")},
        )
    client.post("/queue/1/assign", data={
        "box_id": "1", "name": "spatula", "skip_crop": "1",
    })
    import sys
    app_mod = sys.modules["app"]
    with app_mod.db() as conn:
        item = conn.execute("SELECT photo, source_photo FROM items WHERE id = 1").fetchone()
        job = conn.execute("SELECT photo FROM ingest_jobs WHERE id = 1").fetchone()
    assert item["photo"] == job["photo"]
    assert item["source_photo"] == job["photo"]


def test_assign_preserves_source_photo(client):
    """Cropped items store source_photo for future re-crop."""
    client.post("/boxes", data={"name": "Kitchen"})
    detected = [
        DetectedItem(name="spatula", description="wooden", bbox=[0, 0, 500, 500]),
    ]
    with patch("app.vision.detect_items", return_value=detected):
        client.post(
            "/ingest",
            files={"photo": ("pile.jpg", io.BytesIO(_make_test_image(400, 400)), "image/jpeg")},
        )
    client.post("/queue/1/assign", data={"box_id": "1", "name": "spatula"})
    import sys
    app_mod = sys.modules["app"]
    with app_mod.db() as conn:
        item = conn.execute("SELECT photo, source_photo FROM items WHERE id = 1").fetchone()
    assert item["photo"] != item["source_photo"]
    from pathlib import Path
    assert (Path(app_mod.UPLOAD_DIR) / item["source_photo"]).exists()


def test_recrop_page_loads(client):
    client.post("/boxes", data={"name": "Kitchen"})
    detected = [
        DetectedItem(name="spatula", description="wooden", bbox=[0, 0, 500, 500]),
    ]
    with patch("app.vision.detect_items", return_value=detected):
        client.post(
            "/ingest",
            files={"photo": ("pile.jpg", io.BytesIO(_make_test_image()), "image/jpeg")},
        )
    client.post("/queue/1/assign", data={"box_id": "1", "name": "spatula"})
    page = client.get("/items/1/recrop").text
    assert "Re-crop" in page
    assert "Source image" in page
    assert "Current crop" in page


def test_recrop_applies_new_crop(client):
    client.post("/boxes", data={"name": "Kitchen"})
    detected = [
        DetectedItem(name="spatula", description="wooden", bbox=[0, 0, 500, 500]),
    ]
    with patch("app.vision.detect_items", return_value=detected):
        client.post(
            "/ingest",
            files={"photo": ("pile.jpg", io.BytesIO(_make_test_image(400, 400)), "image/jpeg")},
        )
    client.post("/queue/1/assign", data={"box_id": "1", "name": "spatula"})
    import sys
    app_mod = sys.modules["app"]
    with app_mod.db() as conn:
        old_photo = conn.execute("SELECT photo FROM items WHERE id = 1").fetchone()["photo"]
    client.post("/items/1/recrop", data={
        "crop_y_min": "0", "crop_x_min": "0",
        "crop_y_max": "250", "crop_x_max": "250",
    })
    with app_mod.db() as conn:
        row = conn.execute("SELECT photo, source_photo FROM items WHERE id = 1").fetchone()
    assert row["photo"] != old_photo
    from pathlib import Path
    cropped = Image.open(Path(app_mod.UPLOAD_DIR) / row["photo"])
    assert cropped.width == 100  # 250/1000 * 400


def test_recrop_undo_reverts_to_source(client):
    client.post("/boxes", data={"name": "Kitchen"})
    detected = [
        DetectedItem(name="spatula", description="wooden", bbox=[0, 0, 500, 500]),
    ]
    with patch("app.vision.detect_items", return_value=detected):
        client.post(
            "/ingest",
            files={"photo": ("pile.jpg", io.BytesIO(_make_test_image(400, 400)), "image/jpeg")},
        )
    client.post("/queue/1/assign", data={"box_id": "1", "name": "spatula"})
    import sys
    app_mod = sys.modules["app"]
    with app_mod.db() as conn:
        source = conn.execute("SELECT source_photo FROM items WHERE id = 1").fetchone()["source_photo"]
    client.post("/items/1/recrop", data={"skip_crop": "1"})
    with app_mod.db() as conn:
        item = conn.execute("SELECT photo FROM items WHERE id = 1").fetchone()
    assert item["photo"] == source


def test_box_detail_shows_crop_links(client):
    client.post("/boxes", data={"name": "Kitchen"})
    detected = [
        DetectedItem(name="spatula", description="wooden", bbox=[0, 0, 500, 500]),
    ]
    with patch("app.vision.detect_items", return_value=detected):
        client.post(
            "/ingest",
            files={"photo": ("pile.jpg", io.BytesIO(_make_test_image()), "image/jpeg")},
        )
    client.post("/queue/1/assign", data={"box_id": "1", "name": "spatula"})
    page = client.get("/boxes/1").text
    assert "Re-crop" in page
    assert "/items/1/recrop" in page
