"""Tests for the redesigned faceted search endpoint."""

import io
from PIL import Image


def _real_jpg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), color=(20, 60, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _seed(client):
    """Build a small fixture: 2 locations, 3 rooms, 4 boxes, ~10 items."""
    client.post("/locations", data={"name": "House"})
    client.post("/locations/1/floors", data={"name": "Ground"})
    client.post("/floors/1/rooms", data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/floors/1/rooms", data={"name": "Bedroom", "x": 0.4, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/locations", data={"name": "Storage unit"})
    client.post("/locations/2/floors", data={"name": "Main"})
    client.post("/floors/2/rooms", data={"name": "Aisle 1", "x": 0, "y": 0, "w": 0.3, "h": 0.3})

    client.post("/boxes", data={"name": "Kitchen Box", "room_id": "1"})  # Kitchen / House
    client.post("/boxes", data={"name": "Bedroom Box", "room_id": "2"})  # Bedroom / House
    client.post("/boxes", data={"name": "Holiday Box", "room_id": "3"})  # Aisle 1 / Storage
    client.post("/boxes", data={"name": "Misc Box"})                     # unassigned

    # Items
    client.post("/boxes/1/items", data={"name": "red mug", "notes": "ceramic"})
    client.post("/boxes/1/items", data={"name": "blue plate", "tags": "fragile"})
    client.post(
        "/boxes/1/items", data={"name": "kettle"},
        files={"photo": ("k.jpg", io.BytesIO(_real_jpg()), "image/jpeg")},
    )
    client.post("/boxes/2/items", data={"name": "shirts"})
    client.post("/boxes/2/items", data={"name": "socks"})
    client.post("/boxes/3/items", data={"name": "ornaments", "tags": "fragile"})
    client.post("/boxes/3/items", data={"name": "lights"})
    client.post("/boxes/4/items", data={"name": "random thing"})


# ── Filter facets ────────────────────────────────────────────────────

def test_search_no_filters_returns_all_items_grouped(client):
    _seed(client)
    page = client.get("/search").text
    assert "8</strong> item" in page  # total count visible
    assert "4</strong> box" in page
    # Each box should appear as a group header
    for box in ("Kitchen Box", "Bedroom Box", "Holiday Box", "Misc Box"):
        assert f">{box}</a>" in page


def _result_box_names(page: str) -> list[str]:
    """Pull the box names that appear as RESULT GROUP headers (not in filter
    dropdowns). The headers wrap the box name in a search-group-box anchor."""
    import re
    return re.findall(r'class="search-group-box"[^>]*>([^<]+)</a>', page)


def test_search_filter_by_location(client):
    _seed(client)
    page = client.get("/search?location_id=1").text
    box_names = _result_box_names(page)
    assert "Kitchen Box" in box_names
    assert "Bedroom Box" in box_names
    assert "Holiday Box" not in box_names
    assert "Misc Box" not in box_names  # no room assignment, doesn't count for location


def test_search_filter_by_room(client):
    _seed(client)
    page = client.get("/search?room_id=1").text
    box_names = _result_box_names(page)
    assert "Kitchen Box" in box_names
    assert "Bedroom Box" not in box_names


def test_search_filter_by_box(client):
    _seed(client)
    page = client.get("/search?box_id=2").text
    assert "Bedroom Box" in page
    assert "shirts" in page
    assert "socks" in page
    assert "kettle" not in page


def test_search_filter_by_tag(client):
    _seed(client)
    page = client.get("/search?tag=fragile").text
    assert "blue plate" in page
    assert "ornaments" in page
    assert "red mug" not in page


def test_search_has_photo_only(client):
    _seed(client)
    page = client.get("/search?has_photo=1").text
    assert "kettle" in page  # only one with a photo
    assert "red mug" not in page
    assert "shirts" not in page


def test_search_text_query_matches_name_and_notes(client):
    _seed(client)
    name_hit = client.get("/search?q=mug").text
    assert "red mug" in name_hit
    notes_hit = client.get("/search?q=ceramic").text
    assert "red mug" in notes_hit  # ceramic is in the notes


def test_search_combined_filters_intersect(client):
    _seed(client)
    # Tag fragile AND in House → only blue plate
    page = client.get("/search?tag=fragile&location_id=1").text
    assert "blue plate" in page
    assert "ornaments" not in page  # ornaments is in Storage unit


# ── JSON API for "Load more" ─────────────────────────────────────────

def test_search_json_response_for_ajax_clients(client):
    _seed(client)
    r = client.get(
        "/search?q=mug",
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["total_items"] == 1
    assert payload["total_boxes"] == 1
    assert len(payload["groups"]) == 1
    g = payload["groups"][0]
    assert g["box_name"] == "Kitchen Box"
    assert g["items"][0]["name"] == "red mug"
    assert payload["has_more"] is False


def test_search_pagination_offset_works(client, monkeypatch):
    _seed(client)
    monkeypatch.setattr(client.app_module, "SEARCH_PAGE_SIZE", 3)
    first = client.get(
        "/search",
        headers={"Accept": "application/json"},
    ).json()
    assert len(_collect_items(first["groups"])) == 3
    assert first["has_more"] is True
    assert first["page_loaded"] == 3

    second = client.get(
        "/search?offset=3",
        headers={"Accept": "application/json"},
    ).json()
    assert len(_collect_items(second["groups"])) == 3
    assert second["page_loaded"] == 6


def _collect_items(groups):
    return [it for g in groups for it in g["items"]]


# ── Result presentation ─────────────────────────────────────────────

def test_search_groups_items_by_box_in_order(client):
    _seed(client)
    page = client.get("/search?q=").text
    # Anchor on the actual result group header (not the filter dropdown
    # which also contains all box names) so position comparisons are
    # against the grouped output only.
    def header_pos(name: str) -> int:
        marker = f'class="search-group-box" >{name}</a>'  # nb: extra space
        idx = page.find(marker)
        if idx == -1:
            # template emits with a space before > because of multiline attrs
            import re
            m = re.search(rf'class="search-group-box"[^>]*>{name}</a>', page)
            return m.start() if m else -1
        return idx

    bedroom_h = header_pos("Bedroom Box")
    kitchen_h = header_pos("Kitchen Box")
    red_mug = page.find('>\n            red mug') if '>\n            red mug' in page else page.find('red mug')
    shirts = page.find("shirts")
    # Bedroom comes before Kitchen alphabetically within "House", so the
    # Bedroom group renders first; "shirts" is one of its items and must
    # appear after its header but before the Kitchen group starts.
    assert bedroom_h >= 0 and kitchen_h >= 0
    assert bedroom_h < shirts < kitchen_h
    assert kitchen_h < red_mug


def test_search_active_filter_chips_render(client):
    _seed(client)
    page = client.get("/search?q=mug&tag=fragile&has_photo=1").text
    assert 'data-clear-filter="q"' in page
    assert 'data-clear-filter="tag"' in page
    assert 'data-clear-filter="has_photo"' in page


def test_search_empty_state_distinguishes_no_filters_vs_no_match(client):
    """Two distinct empty states: 'no filters yet, type something' vs 'your
    filters are active but nothing matched'. The mascot copy should differ."""
    fresh = client.get("/search").text
    # Use different paragraph copy for "no filters" vs "no results"
    assert "Pick a filter" in fresh or "Search across" in fresh

    _seed(client)
    no_match = client.get("/search?q=zzzzzzz").text
    assert "Nothing matched" in no_match or "No items matched" in no_match
