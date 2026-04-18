import io
from pathlib import Path


def test_index_empty(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "No boxes yet" in r.text


def test_create_box_appears_on_index(client):
    r = client.post(
        "/boxes",
        data={"name": "Kitchen #1", "location": "Garage B", "notes": "fragile"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"

    r = client.get("/")
    assert "Kitchen #1" in r.text
    assert "Garage B" in r.text
    assert "0 items" in r.text


def test_box_detail_404_for_missing(client):
    assert client.get("/boxes/999").status_code == 404


def test_add_item_without_photo(client):
    client.post("/boxes", data={"name": "Box A"})
    r = client.post(
        "/boxes/1/items",
        data={"name": "spatula", "notes": "wooden"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/boxes/1"

    detail = client.get("/boxes/1").text
    assert "spatula" in detail
    assert "wooden" in detail
    assert "Contents (1)" in detail


def test_add_item_with_photo_persists_and_serves(client, tmp_path):
    client.post("/boxes", data={"name": "Box A"})
    fake_jpg = b"\xff\xd8\xff\xe0fakejpegbytes"
    r = client.post(
        "/boxes/1/items",
        data={"name": "mug", "notes": ""},
        files={"photo": ("mug.jpg", io.BytesIO(fake_jpg), "image/jpeg")},
        follow_redirects=False,
    )
    assert r.status_code == 303

    upload_dir = Path(client.app_module.UPLOAD_DIR)
    saved = list(upload_dir.glob("*.jpg"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == fake_jpg

    detail = client.get("/boxes/1").text
    assert f"/uploads/{saved[0].name}" in detail

    r = client.get(f"/uploads/{saved[0].name}")
    assert r.status_code == 200
    assert r.content == fake_jpg


def test_uploads_404_for_missing(client):
    assert client.get("/uploads/nope.jpg").status_code == 404


def test_item_count_on_index(client):
    client.post("/boxes", data={"name": "Box A"})
    client.post("/boxes/1/items", data={"name": "x"})
    client.post("/boxes/1/items", data={"name": "y"})
    assert "2 items" in client.get("/").text


def test_delete_item_removes_row_and_photo(client):
    client.post("/boxes", data={"name": "Box A"})
    client.post(
        "/boxes/1/items",
        data={"name": "mug"},
        files={"photo": ("m.jpg", io.BytesIO(b"abc"), "image/jpeg")},
    )
    upload_dir = Path(client.app_module.UPLOAD_DIR)
    photo = next(upload_dir.glob("*.jpg"))

    r = client.post("/items/1/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/boxes/1"
    assert not photo.exists()
    assert "Contents (0)" in client.get("/boxes/1").text


def test_delete_box_cascades_items_and_photos(client):
    client.post("/boxes", data={"name": "Box A"})
    client.post(
        "/boxes/1/items",
        data={"name": "mug"},
        files={"photo": ("m.jpg", io.BytesIO(b"abc"), "image/jpeg")},
    )
    upload_dir = Path(client.app_module.UPLOAD_DIR)
    photo = next(upload_dir.glob("*.jpg"))

    r = client.post("/boxes/1/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert not photo.exists()
    assert client.get("/boxes/1").status_code == 404


def test_add_item_to_missing_box_404(client):
    assert client.post("/boxes/42/items", data={"name": "x"}).status_code == 404
