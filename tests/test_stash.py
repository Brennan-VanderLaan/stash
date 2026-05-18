import io
from pathlib import Path


def test_index_empty(client):
    """A brand-new tenant lands on the Get started card, not the
    bare turtle empty state.  The card walks them through the
    photo-first magic flow rather than nudging them to create a
    box manually."""
    r = client.get("/home")
    assert r.status_code == 200
    assert "Get started" in r.text
    assert "Take a photo of something" in r.text
    # The active CTA on step 1 points at /ingest.
    assert 'href="/ingest"' in r.text
    # Bare turtle empty state should NOT show here — the Get started
    # card carries the message.  It returns once the user has at
    # least uploaded a photo (intermediate state).
    assert "No boxes yet" not in r.text


def test_index_get_started_collapses_once_complete(client):
    """When the tenant has at least one photo + box + item-in-box,
    the Get started card disappears and the celebratory single-line
    "You're set up" replaces it."""
    # Seed: create a box and an item attached to it (photo + filed).
    client.post("/boxes", data={"name": "First box"})
    with client.app_module.db() as conn:
        conn.execute(
            "INSERT INTO items (tenant_id, box_id, name, photo) "
            "VALUES (?, 1, 'thing', 'fake.jpg')",
            (client.test_tenant_id,),
        )
        conn.commit()
    r = client.get("/home")
    assert r.status_code == 200
    # The interactive checklist is gone.
    assert 'data-testid="getting-started"' not in r.text
    # The "you're set up" line replaces it.
    assert "You're set up" in r.text


def test_index_get_started_shows_step_2_after_photo(client):
    """Once a photo has been uploaded (queue has a pending row),
    step 1 ticks and step 2 becomes the active CTA pointing at
    the queue."""
    with client.app_module.db() as conn:
        conn.execute(
            "INSERT INTO pending_items "
            "(tenant_id, name, photo) "
            "VALUES (?, 'thing', 'fake.jpg')",
            (client.test_tenant_id,),
        )
        conn.commit()
    r = client.get("/home")
    assert r.status_code == 200
    # Step 1 done (the active CTA on step 1 — the /ingest button —
    # is gone since photo is taken).
    assert "Sort it in the queue" in r.text
    # Step 2 is now the active step → CTA points to /queue.
    assert 'href="/queue"' in r.text


def test_create_box_appears_on_index(client):
    """Box creation via the POST /boxes endpoint persists name +
    notes; the box appears on /home with its name and item count.
    The free-text ``location`` field is gone (feedback #78) — the
    room picker is the only way to set a box's room/location."""
    r = client.post(
        "/boxes",
        data={"name": "Kitchen #1", "notes": "fragile"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/home"

    r = client.get("/home")
    assert "Kitchen #1" in r.text
    assert "0 items" in r.text


def test_index_groups_boxes_by_room_and_location(client):
    """The boxes index buckets every card into a (location, room) section
    so a long list doesn't read as one undifferentiated grid.  Each
    bucket needs a header + count, and unassigned boxes go into their
    own group at the end."""
    # Two rooms in one location, plus a legacy free-text location, plus
    # a fully-unassigned box.
    client.post("/locations", data={"name": "Townhouse"})
    with client.app_module.db() as conn:
        loc_id = conn.execute(
            "SELECT id FROM locations WHERE name = 'Townhouse'"
        ).fetchone()["id"]
        # Two rooms under the same location.
        for room_name in ("Kitchen", "Garage"):
            conn.execute(
                "INSERT INTO rooms (location_id, name) VALUES (?, ?)",
                (loc_id, room_name),
            )
        conn.commit()
        kitchen_id = conn.execute(
            "SELECT id FROM rooms WHERE name = 'Kitchen'"
        ).fetchone()["id"]
        garage_id = conn.execute(
            "SELECT id FROM rooms WHERE name = 'Garage'"
        ).fetchone()["id"]

    client.post("/boxes", data={"name": "Knives", "room_id": str(kitchen_id)})
    client.post("/boxes", data={"name": "Plates", "room_id": str(kitchen_id)})
    client.post("/boxes", data={"name": "Bike tools", "room_id": str(garage_id)})
    # Feedback #78 retired the free-text ``location`` field — boxes
    # without a room render in the "Unassigned" bucket regardless
    # of any legacy ``location`` text.
    client.post("/boxes", data={"name": "Mystery box"})

    page = client.get("/home").text

    # Each bucket type renders at least once.  Two-box rooms show "2
    # boxes", single-box rooms "1 box", and the unassigned bucket
    # has the "Unassigned" header.
    assert "2 boxes" in page  # Kitchen has two
    assert "1 box" in page    # Garage / Unassigned each have one
    assert "Kitchen" in page
    assert "Garage" in page
    assert "Unassigned" in page

    # All four box names render.
    for name in ("Knives", "Plates", "Bike tools", "Mystery box"):
        assert name in page, f"{name} missing from grouped index"


def test_move_item_returns_json_for_ajax_clients(client):
    """Box-detail item DnD calls POST /items/{id}/move via fetch with
    Accept: application/json. Endpoint should return a JSON body, not a
    303 redirect that the browser would follow into the new box page."""
    client.post("/boxes", data={"name": "Box A"})
    client.post("/boxes", data={"name": "Box B"})
    client.post("/boxes/1/items", data={"name": "thing"})
    r = client.post(
        "/items/1/move",
        data={"box_id": "2"},
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["ok"] is True
    assert payload["item_id"] == 1
    assert payload["box_id"] == 2
    with client.app_module.db() as conn:
        b = conn.execute("SELECT box_id FROM items WHERE id = 1").fetchone()[0]
    assert b == 2


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
    assert "Contents (1)" in detail or "item-card" in detail  # item rendered


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

    # On disk the bytes are now encrypted (vault.encrypt_for_tenant) and
    # carry the ENCRYPTED_MARKER prefix; the cleartext only exists when
    # served through /uploads/{name}, which decrypts on the fly.  We
    # check both: (1) on-disk file exists under the tenant subdir and
    # is encrypted, (2) the served bytes match the input.
    upload_dir = Path(client.app_module.UPLOAD_DIR) / str(client.test_tenant_id)
    saved = list(upload_dir.glob("*.jpg"))
    assert len(saved) == 1
    on_disk = saved[0].read_bytes()
    assert on_disk.startswith(client.app_module.vault.ENCRYPTED_MARKER), \
        "photo was written cleartext to disk — encryption-at-rest broken"
    assert on_disk != fake_jpg

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
    assert "2 items" in client.get("/home").text


def test_delete_item_removes_row_and_photo(client):
    client.post("/boxes", data={"name": "Box A"})
    client.post(
        "/boxes/1/items",
        data={"name": "mug"},
        files={"photo": ("m.jpg", io.BytesIO(b"abc"), "image/jpeg")},
    )
    upload_dir = Path(client.app_module.UPLOAD_DIR) / str(client.test_tenant_id)
    photo = next(upload_dir.glob("*.jpg"))

    r = client.post("/items/1/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/boxes/1"
    assert not photo.exists()
    assert "No items yet" in client.get("/boxes/1").text


def test_delete_box_cascades_items_and_photos(client):
    client.post("/boxes", data={"name": "Box A"})
    client.post(
        "/boxes/1/items",
        data={"name": "mug"},
        files={"photo": ("m.jpg", io.BytesIO(b"abc"), "image/jpeg")},
    )
    upload_dir = Path(client.app_module.UPLOAD_DIR) / str(client.test_tenant_id)
    photo = next(upload_dir.glob("*.jpg"))

    r = client.post("/boxes/1/delete", data={"confirm": "Box A"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/home"
    assert not photo.exists()
    assert client.get("/boxes/1").status_code == 404


def test_delete_box_requires_name_confirmation(client):
    """Posting to delete without the correct name must be rejected."""
    client.post("/boxes", data={"name": "Precious"})
    assert client.post("/boxes/1/delete").status_code in (400, 422)
    assert client.post("/boxes/1/delete", data={"confirm": "wrong"}).status_code == 400
    assert client.get("/boxes/1").status_code == 200  # still there


def test_add_item_to_missing_box_404(client):
    assert client.post("/boxes/42/items", data={"name": "x"}).status_code == 404


def test_replace_item_photo(client):
    client.post("/boxes", data={"name": "Box A"})
    client.post(
        "/boxes/1/items",
        data={"name": "mug"},
        files={"photo": ("old.jpg", io.BytesIO(b"oldphoto"), "image/jpeg")},
    )
    upload_dir = Path(client.app_module.UPLOAD_DIR) / str(client.test_tenant_id)
    old_photo = next(upload_dir.glob("*.jpg"))

    r = client.post(
        "/items/1/replace-photo",
        files={"photo": ("new.jpg", io.BytesIO(b"newphoto"), "image/jpeg")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/boxes/1#item-1"

    with client.app_module.db() as conn:
        item = conn.execute("SELECT photo, source_photo FROM items WHERE id = 1").fetchone()
    # New photo saved and set as both photo and source_photo
    assert item["photo"] != old_photo.name
    assert item["photo"] == item["source_photo"]
    new_path = upload_dir / item["photo"]
    assert new_path.exists()
    # Verify cleartext via the served endpoint — disk bytes are
    # encrypted ciphertext after phase 2.
    assert client.get(f"/uploads/{item['photo']}").content == b"newphoto"


def test_replace_photo_shows_in_box_detail(client):
    client.post("/boxes", data={"name": "Box A"})
    client.post("/boxes/1/items", data={"name": "mug"})
    # Item has no photo initially — dialog still offers a replace button
    assert "/items/1/replace-photo" in client.get("/boxes/1").text

    client.post(
        "/items/1/replace-photo",
        files={"photo": ("pic.jpg", io.BytesIO(b"data"), "image/jpeg")},
    )
    page = client.get("/boxes/1").text
    assert "/uploads/" in page


def test_replace_photo_on_missing_item_404(client):
    r = client.post(
        "/items/999/replace-photo",
        files={"photo": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")},
    )
    assert r.status_code == 404
