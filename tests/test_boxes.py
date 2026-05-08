import io


def _make_item(client, box_id, name="thing"):
    client.post(
        f"/boxes/{box_id}/items",
        data={"name": name},
        files={"photo": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")},
    )


def test_edit_box_updates_fields(client):
    client.post("/boxes", data={"name": "Old name", "location": "Garage"})
    r = client.post(
        "/boxes/1/edit",
        data={"name": "Kitchen #1", "location": "Garage shelf B", "notes": "fragile"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    detail = client.get("/boxes/1").text
    assert "Kitchen #1" in detail
    assert "Garage shelf B" in detail
    assert "fragile" in detail


def test_edit_box_requires_name(client):
    client.post("/boxes", data={"name": "Box A"})
    r = client.post("/boxes/1/edit", data={"name": "  "})
    assert r.status_code == 400


def test_edit_box_with_stale_if_match_returns_409(client):
    """Optimistic concurrency: if a tab tries to save a box edit using
    a ``version`` that no longer matches the row (because another tab
    or session already saved an edit), the route returns 409 instead
    of clobbering the newer write."""
    client.post("/boxes", data={"name": "Original"})
    # First edit succeeds — bumps the row's version from 1 to 2.
    r1 = client.post(
        "/boxes/1/edit",
        data={"name": "First edit", "if_match": "1"},
        follow_redirects=False,
    )
    assert r1.status_code == 303
    # Second edit posts with the now-stale version=1 token, which is what
    # a stale tab would still hold. The DAO must refuse it.
    r2 = client.post(
        "/boxes/1/edit",
        data={"name": "Second edit", "if_match": "1"},
        follow_redirects=False,
    )
    assert r2.status_code == 409
    # Original first-edit value is still in the DB — no silent overwrite.
    assert "First edit" in client.get("/boxes/1").text
    assert "Second edit" not in client.get("/boxes/1").text


def test_edit_box_without_if_match_is_last_write_wins(client):
    """The if_match field is optional — clients that don't send it
    (legacy forms, scripted clients) get the old last-write-wins
    semantic instead of a hard rejection.  This keeps the migration
    backwards-compatible."""
    client.post("/boxes", data={"name": "Original"})
    r1 = client.post(
        "/boxes/1/edit",
        data={"name": "First"},
        follow_redirects=False,
    )
    assert r1.status_code == 303
    r2 = client.post(
        "/boxes/1/edit",
        data={"name": "Second"},
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert "Second" in client.get("/boxes/1").text


def test_location_autocomplete_lists_distinct_locations(client):
    client.post("/boxes", data={"name": "A", "location": "Garage"})
    client.post("/boxes", data={"name": "B", "location": "Attic"})
    client.post("/boxes", data={"name": "C", "location": "Garage"})  # dup
    page = client.get("/boxes/1").text
    # datalist should have both unique locations
    assert "<datalist" in page
    assert 'value="Garage"' in page
    assert 'value="Attic"' in page


def test_move_single_item_between_boxes(client):
    client.post("/boxes", data={"name": "Source"})
    client.post("/boxes", data={"name": "Dest"})
    _make_item(client, 1, "spatula")

    r = client.post("/items/1/move", data={"box_id": "2"}, follow_redirects=False)
    assert r.status_code == 303
    # Redirect keeps the item anchor so the modal re-opens after the move
    assert r.headers["location"] == "/boxes/2#item-1"

    assert "spatula" not in client.get("/boxes/1").text
    assert "spatula" in client.get("/boxes/2").text


def test_move_item_to_unknown_box_400(client):
    client.post("/boxes", data={"name": "A"})
    _make_item(client, 1)
    r = client.post("/items/1/move", data={"box_id": "999"})
    assert r.status_code == 400


def test_bulk_move_items(client):
    client.post("/boxes", data={"name": "Source"})
    client.post("/boxes", data={"name": "Dest"})
    _make_item(client, 1, "alpha_item")
    _make_item(client, 1, "bravo_item")
    _make_item(client, 1, "charlie_item")

    r = client.post(
        "/boxes/1/move-items",
        data={"target_box_id": "2", "item_ids": ["1", "3"]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/boxes/2"

    src = client.get("/boxes/1").text
    dest = client.get("/boxes/2").text
    assert "bravo_item" in src
    assert "alpha_item" not in src
    assert "charlie_item" not in src
    assert "alpha_item" in dest
    assert "charlie_item" in dest


def test_audit_unchecked_items_move_to_sort_queue(client):
    client.post("/boxes", data={"name": "Kitchen"})
    _make_item(client, 1, "found_thing")
    _make_item(client, 1, "lost_thing")

    r = client.post(
        "/boxes/1/audit",
        data={"found": ["1"]},  # only item 1 ticked
        follow_redirects=False,
    )
    # Redirects to queue when something was extracted
    assert r.status_code == 303
    assert r.headers["location"] == "/queue"

    # Lost item is gone from the box, found item remains
    detail = client.get("/boxes/1").text
    assert "found_thing" in detail
    assert "lost_thing" not in detail
    assert "Last audited" in detail

    # Lost item landed in the sort queue with provenance
    queue = client.get("/queue").text
    assert "lost_thing" in queue
    assert "Kitchen" in queue  # previous_box_name shown


def test_audit_with_no_missing_returns_to_box(client):
    client.post("/boxes", data={"name": "Box A"})
    _make_item(client, 1, "thing")

    r = client.post(
        "/boxes/1/audit",
        data={"found": ["1"]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/boxes/1"
    # Item still there, last_seen stamped via Last audited line
    assert "thing" in client.get("/boxes/1").text


def test_audit_recovered_item_can_be_refiled(client):
    """Item dropped from one box during audit can be assigned to another from the queue."""
    client.post("/boxes", data={"name": "Old box"})
    client.post("/boxes", data={"name": "New box"})
    _make_item(client, 1, "wanderer")

    # Audit Old box, mark wanderer as missing → moves to queue
    client.post("/boxes/1/audit", data={})

    # Re-file from queue into New box
    client.post("/queue/1/assign", data={"box_id": "2", "name": "wanderer"})

    assert "wanderer" in client.get("/boxes/2").text
    assert "wanderer" not in client.get("/boxes/1").text


def test_audit_view_shows_items(client):
    client.post("/boxes", data={"name": "Box A"})
    _make_item(client, 1, "spatula")
    page = client.get("/boxes/1/audit").text
    assert "Audit: Box A" in page
    assert "spatula" in page
    assert 'type="checkbox"' in page
