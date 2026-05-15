import io
from unittest.mock import patch
from vision import DetectedItem


def _box(client, name="Box A"):
    client.post("/boxes", data={"name": name})


def _item(client, box_id, name="thing", tags=""):
    client.post(
        f"/boxes/{box_id}/items",
        data={"name": name, "tags": tags},
        files={"photo": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")},
    )


def test_add_item_with_tags(client):
    _box(client)
    _item(client, 1, "spatula", "kitchen, costa-rica")
    page = client.get("/boxes/1").text
    assert "kitchen" in page
    assert "costa-rica" in page


def test_add_item_with_key_value_tag(client):
    _box(client)
    _item(client, 1, "drill", "serial:ABC123, power-tools")
    page = client.get("/boxes/1").text
    assert "serial:ABC123" in page
    assert "power-tools" in page


def test_add_tag_to_existing_item(client):
    _box(client)
    _item(client, 1, "mug")
    r = client.post("/items/1/tags", data={"tag": "fragile"}, follow_redirects=False)
    assert r.status_code == 303
    assert "fragile" in client.get("/boxes/1").text


def test_remove_tag_from_item(client):
    _box(client)
    _item(client, 1, "mug", "fragile")
    r = client.post("/items/1/tags/1/delete", follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/boxes/1").text
    # The tag span with remove button should be gone. The tag name may linger
    # in the datalist (autocomplete), but not as a displayed tag on the item.
    assert "tag-list" not in page  # no tags left on any item


def test_bulk_tag_box_attaches_to_every_item(client):
    """``POST /boxes/{id}/tag-all`` attaches one tag to every item
    currently in the box.  Empty boxes are a no-op; partial inserts
    can't happen (single transaction) so all three items must carry
    the tag after the call."""
    _box(client)
    _item(client, 1, "spatula")
    _item(client, 1, "whisk")
    _item(client, 1, "pan")
    r = client.post("/boxes/1/tag-all", data={"tag": "kitchen"},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/boxes/1").text
    # Each item gets its own per-tag delete-form whose action
    # contains the tag_id — count those to confirm every item now
    # carries the freshly-created tag (id 1).
    assert page.count("/tags/1/delete") == 3


def test_bulk_tag_box_accepts_multiple_tags(client):
    """Comma-separated list applies every entry to every item, same
    parsing as the single-item form."""
    _box(client)
    _item(client, 1, "drill")
    _item(client, 1, "sander")
    r = client.post(
        "/boxes/1/tag-all",
        data={"tag": "tools, room:garage"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/boxes/1").text
    # Both tags ('tools' and 'room') get tag rows; each of the two
    # items has both tags after the bulk apply — so 2 delete-forms
    # per tag_id.
    assert page.count("/tags/1/delete") == 2
    assert page.count("/tags/2/delete") == 2


def test_bulk_tag_box_returns_json_for_ajax(client):
    _box(client)
    _item(client, 1, "a")
    _item(client, 1, "b")
    r = client.post(
        "/boxes/1/tag-all",
        data={"tag": "shared"},
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["ok"] is True
    assert payload["box_id"] == 1
    assert payload["tagged"] == 2
    assert payload["tags"] == ["shared"]


def test_bulk_tag_box_rejects_missing_tag(client):
    _box(client)
    _item(client, 1, "thing")
    r = client.post("/boxes/1/tag-all", data={"tag": "  "},
                    follow_redirects=False)
    assert r.status_code == 400


def test_bulk_tag_box_404_for_other_tenant(client):
    """A forged box_id pointing at someone else's box must 404, not
    silently no-op (which would mask the bug from the user)."""
    _box(client)
    _item(client, 1, "thing")
    r = client.post("/boxes/999/tag-all", data={"tag": "x"},
                    follow_redirects=False)
    assert r.status_code == 404


def test_tags_case_insensitive(client):
    _box(client)
    _item(client, 1, "a", "Kitchen")
    _item(client, 1, "b", "kitchen")
    tags_page = client.get("/tags").text
    # Should be one tag, not two
    assert tags_page.count(">Kitchen<") + tags_page.count(">kitchen<") == 1


def test_tags_browse_page_shows_counts(client):
    _box(client)
    _item(client, 1, "a", "electronics")
    _item(client, 1, "b", "electronics, fragile")
    page = client.get("/tags").text
    assert "electronics" in page
    assert "2 items" in page
    assert "fragile" in page
    assert "1 item" in page


def test_search_by_name(client):
    _box(client)
    _item(client, 1, "wooden spatula")
    _item(client, 1, "ceramic mug")
    page = client.get("/search?q=spatula").text
    assert "wooden spatula" in page
    assert "ceramic mug" not in page


def test_search_by_tag(client):
    _box(client, "Kitchen")
    _item(client, 1, "spatula", "cooking")
    _item(client, 1, "hammer", "tools")
    page = client.get("/search?tag=cooking").text
    assert "spatula" in page
    assert "hammer" not in page


def test_search_by_name_and_tag(client):
    _box(client)
    _item(client, 1, "red mug", "fragile")
    _item(client, 1, "red plate", "fragile")
    _item(client, 1, "red ball", "toys")
    page = client.get("/search?q=red&tag=fragile").text
    assert "red mug" in page
    assert "red plate" in page
    assert "red ball" not in page


def test_search_shows_box_link(client):
    _box(client, "Kitchen")
    _item(client, 1, "spatula")
    page = client.get("/search?q=spatula").text
    assert "Kitchen" in page
    assert "/boxes/1" in page


def test_tags_preserved_through_audit(client):
    _box(client, "Old box")
    _item(client, 1, "tagged_thing", "electronics, serial:X99")
    # Audit with nothing found → moves to queue
    client.post("/boxes/1/audit", data={})
    queue = client.get("/queue").text
    assert "tagged_thing" in queue

    # Assign to a new box
    client.post("/boxes", data={"name": "New box"})
    client.post("/queue/1/assign", data={"box_id": "2", "name": "tagged_thing"})

    # Tags survived the round-trip
    page = client.get("/boxes/2").text
    assert "electronics" in page
    assert "serial:X99" in page


def test_queue_assign_creates_new_tags(client):
    """Typing new tag names in the queue sort card creates them on assign."""
    client.post("/boxes", data={"name": "Kitchen"})
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="spatula", description="wooden")
    ]):
        client.post("/ingest", files={"photos": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")})

    client.post("/queue/1/assign", data={
        "box_id": "1", "name": "spatula",
        "tags": "kitchen, brand-new-tag, serial:XYZ",
    })

    page = client.get("/boxes/1").text
    assert "kitchen" in page
    assert "brand-new-tag" in page
    assert "serial:XYZ" in page


def test_tags_preserved_through_queue_assign(client):
    _box(client, "Dest box")
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="widget", description="a widget")
    ]):
        client.post("/ingest", files={"photos": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")})

    # Manually add a tag to the pending item via DB (simulate future vision tag support)
    import sys
    app_mod = sys.modules["app"]
    with app_mod.db() as conn:
        tag_id = app_mod.ensure_tag(conn, client.test_tenant_id, "auto-detected")
        conn.execute(
            "INSERT INTO pending_item_tags (pending_item_id, tag_id, tenant_id) "
            "VALUES (?, ?, ?)",
            (1, tag_id, client.test_tenant_id),
        )
        conn.commit()

    client.post("/queue/1/assign", data={"box_id": "1", "name": "widget"})
    page = client.get("/boxes/1").text
    assert "auto-detected" in page


def test_tags_autocomplete_endpoint(client):
    _box(client)
    _item(client, 1, "a", "electronics, fragile, embedded")
    r = client.get("/tags/autocomplete?q=e")
    data = r.json()
    assert "electronics" in data
    assert "embedded" in data
    assert "fragile" not in data
