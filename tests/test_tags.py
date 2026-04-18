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


def test_tags_preserved_through_queue_assign(client):
    _box(client, "Dest box")
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="widget", description="a widget")
    ]):
        client.post("/ingest", files={"photo": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")})

    # Manually add a tag to the pending item via DB (simulate future vision tag support)
    import sys
    app_mod = sys.modules["app"]
    with app_mod.db() as conn:
        tag_id = app_mod.ensure_tag(conn, "auto-detected")
        conn.execute(
            "INSERT INTO pending_item_tags (pending_item_id, tag_id) VALUES (?, ?)",
            (1, tag_id),
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
