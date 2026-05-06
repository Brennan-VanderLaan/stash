"""Tests for the locations + rooms model and the floorplan editor endpoints."""

import io
from PIL import Image


def _fake_jpg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), color=(20, 60, 30)).save(buf, format="JPEG")
    return buf.getvalue()


# ── Migration ────────────────────────────────────────────────────────

def test_legacy_location_strings_migrate_to_rooms(tmp_path, monkeypatch):
    """Boxes created before locations existed had a free-text `location` column.
    The migration must convert each unique string into a Room under a single
    'Default location' and backfill boxes.room_id."""
    import importlib
    import sqlite3
    import sys
    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("STASH_DB", str(db_path))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))

    # Hand-build the pre-migration schema and seed legacy rows
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE boxes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL, location TEXT, notes TEXT, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
    )
    conn.execute("INSERT INTO boxes (name, location) VALUES ('A', 'Bedroom')")
    conn.execute("INSERT INTO boxes (name, location) VALUES ('B', 'Bedroom')")
    conn.execute("INSERT INTO boxes (name, location) VALUES ('C', 'Hallway')")
    conn.execute("INSERT INTO boxes (name, location) VALUES ('D', NULL)")
    conn.commit()
    conn.close()

    if "app" in sys.modules:
        del sys.modules["app"]
    import app
    importlib.reload(app)

    with app.db() as conn:
        locs = conn.execute("SELECT * FROM locations").fetchall()
        rooms = conn.execute("SELECT * FROM rooms ORDER BY name").fetchall()
        boxes = conn.execute("SELECT id, name, room_id FROM boxes ORDER BY name").fetchall()

    assert len(locs) == 1, "expected a single 'Default location' to be created"
    room_names = [r["name"] for r in rooms]
    assert sorted(room_names) == ["Bedroom", "Hallway"]
    name_to_room = {r["name"]: r["id"] for r in rooms}

    by_name = {b["name"]: b for b in boxes}
    assert by_name["A"]["room_id"] == name_to_room["Bedroom"]
    assert by_name["B"]["room_id"] == name_to_room["Bedroom"]
    assert by_name["C"]["room_id"] == name_to_room["Hallway"]
    assert by_name["D"]["room_id"] is None  # NULL legacy stays unassigned


def test_migration_idempotent(client):
    """Running migrate_db a second time on an already-migrated DB must not
    create duplicate locations or rooms."""
    client.post("/locations", data={"name": "House"})
    client.post("/locations/1/rooms", data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.5, "h": 0.5})
    client.app_module.migrate_db()
    with client.app_module.db() as conn:
        loc_count = conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0]
        room_count = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
    assert loc_count == 1
    assert room_count == 1


# ── Locations CRUD ───────────────────────────────────────────────────

def test_create_location_redirects_to_detail(client):
    r = client.post("/locations", data={"name": "Main house"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/locations/1"


def test_locations_index_lists_all(client):
    client.post("/locations", data={"name": "Main house"})
    client.post("/locations", data={"name": "Storage unit"})
    page = client.get("/locations").text
    assert "Main house" in page
    assert "Storage unit" in page


def test_rename_location(client):
    client.post("/locations", data={"name": "House"})
    client.post("/locations/1", data={"name": "Big house"})
    page = client.get("/locations/1").text
    assert "Big house" in page


def test_delete_location_requires_name_confirm(client):
    client.post("/locations", data={"name": "Old place"})
    bad = client.post("/locations/1/delete", data={"confirm": "wrong"})
    assert bad.status_code == 400
    good = client.post("/locations/1/delete", data={"confirm": "Old place"}, follow_redirects=False)
    assert good.status_code == 303
    with client.app_module.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 0


def test_delete_location_cascades_to_rooms(client):
    client.post("/locations", data={"name": "House"})
    client.post("/locations/1/rooms", data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/locations/1/rooms", data={"name": "Bedroom", "x": 0.4, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/locations/1/delete", data={"confirm": "House"})
    with client.app_module.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0


def test_delete_location_unassigns_boxes(client):
    """Deleting a location must SET NULL on boxes.room_id, not orphan to a
    dangling FK or cascade-delete the boxes themselves."""
    client.post("/locations", data={"name": "House"})
    client.post("/locations/1/rooms", data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Mugs", "room_id": "1"})
    client.post("/locations/1/delete", data={"confirm": "House"})
    with client.app_module.db() as conn:
        row = conn.execute("SELECT room_id FROM boxes WHERE name = 'Mugs'").fetchone()
    assert row["room_id"] is None, "box should remain but be unassigned"


# ── Floorplan upload ─────────────────────────────────────────────────

def test_upload_floorplan_saves_image(client):
    client.post("/locations", data={"name": "House"})
    r = client.post(
        "/locations/1/floorplan",
        files={"image": ("plan.jpg", io.BytesIO(_fake_jpg()), "image/jpeg")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with client.app_module.db() as conn:
        plan = conn.execute("SELECT floorplan FROM locations WHERE id = 1").fetchone()[0]
    assert plan
    assert (client.app_module.UPLOAD_DIR / plan).exists()


def test_floorplan_upload_replaces_old_file(client):
    client.post("/locations", data={"name": "House"})
    client.post(
        "/locations/1/floorplan",
        files={"image": ("a.jpg", io.BytesIO(_fake_jpg()), "image/jpeg")},
    )
    with client.app_module.db() as conn:
        first = conn.execute("SELECT floorplan FROM locations WHERE id = 1").fetchone()[0]
    client.post(
        "/locations/1/floorplan",
        files={"image": ("b.jpg", io.BytesIO(_fake_jpg()), "image/jpeg")},
    )
    with client.app_module.db() as conn:
        second = conn.execute("SELECT floorplan FROM locations WHERE id = 1").fetchone()[0]
    assert first != second
    assert not (client.app_module.UPLOAD_DIR / first).exists(), \
        "old floorplan should have been cleaned up"


# ── Rooms CRUD ───────────────────────────────────────────────────────

def test_create_room_assigns_color(client):
    client.post("/locations", data={"name": "House"})
    r = client.post(
        "/locations/1/rooms",
        data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["color"].startswith("#")
    assert payload["name"] == "Kitchen"


def test_room_create_clamps_oversized_coordinates(client):
    """A drag that runs off the canvas can produce w > 1; clamping prevents
    nonsensical fractions getting persisted."""
    client.post("/locations", data={"name": "House"})
    client.post(
        "/locations/1/rooms",
        data={"name": "Big", "x": -0.2, "y": 0.5, "w": 5, "h": 1.2},
    )
    with client.app_module.db() as conn:
        row = conn.execute("SELECT x, y, w, h FROM rooms WHERE id = 1").fetchone()
    assert 0 <= row["x"] <= 1
    assert 0 <= row["y"] <= 1
    assert 0 <= row["w"] <= 1
    assert 0 <= row["h"] <= 1


def test_edit_room_updates_geometry_and_name(client):
    client.post("/locations", data={"name": "House"})
    client.post("/locations/1/rooms", data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post(
        "/rooms/1",
        data={"name": "Big Kitchen", "x": 0.1, "y": 0.1, "w": 0.4, "h": 0.5},
        headers={"Accept": "application/json"},
    )
    with client.app_module.db() as conn:
        row = conn.execute("SELECT name, x, w FROM rooms WHERE id = 1").fetchone()
    assert row["name"] == "Big Kitchen"
    assert abs(row["x"] - 0.1) < 1e-6
    assert abs(row["w"] - 0.4) < 1e-6


def test_delete_room_unassigns_boxes(client):
    client.post("/locations", data={"name": "House"})
    client.post("/locations/1/rooms", data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Mugs", "room_id": "1"})
    client.post("/rooms/1/delete", headers={"Accept": "application/json"})
    with client.app_module.db() as conn:
        row = conn.execute("SELECT room_id FROM boxes WHERE name = 'Mugs'").fetchone()
    assert row["room_id"] is None


def test_room_distinct_colors(client):
    """Sequential rooms in the same location should pull distinct colors from
    the palette so the floorplan stays visually parseable."""
    client.post("/locations", data={"name": "House"})
    seen = set()
    for i in range(4):
        r = client.post(
            "/locations/1/rooms",
            data={"name": f"Room {i}", "x": 0, "y": 0, "w": 0.1, "h": 0.1},
            headers={"Accept": "application/json"},
        )
        seen.add(r.json()["color"])
    assert len(seen) == 4, "expected 4 distinct colors among 4 rooms"


# ── Box ↔ room integration ──────────────────────────────────────────

def test_box_create_with_room_id_denormalizes_location(client):
    """Picking a room should auto-fill boxes.location with the room name so
    list views that don't JOIN still show the room."""
    client.post("/locations", data={"name": "House"})
    client.post("/locations/1/rooms", data={"name": "Garage", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Tools", "room_id": "1"})
    with client.app_module.db() as conn:
        row = conn.execute("SELECT room_id, location FROM boxes WHERE name = 'Tools'").fetchone()
    assert row["room_id"] == 1
    assert row["location"] == "Garage"


def test_box_edit_can_unassign_room(client):
    client.post("/locations", data={"name": "House"})
    client.post("/locations/1/rooms", data={"name": "Garage", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Tools", "room_id": "1"})
    client.post("/boxes/1/edit", data={"name": "Tools", "room_id": "", "location": "shed"})
    with client.app_module.db() as conn:
        row = conn.execute("SELECT room_id, location FROM boxes WHERE id = 1").fetchone()
    assert row["room_id"] is None
    assert row["location"] == "shed"


def test_box_detail_links_to_floorplan_with_room_anchor(client):
    client.post("/locations", data={"name": "House"})
    client.post("/locations/1/rooms", data={"name": "Garage", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Tools", "room_id": "1"})
    page = client.get("/boxes/1").text
    # The chip on the box detail should deep-link to the location with a
    # room anchor — that's what powers "see where this box is" UX.
    assert 'href="/locations/1#room-1"' in page
    assert "Garage" in page


def test_room_boxes_view_lists_assigned_boxes(client):
    client.post("/locations", data={"name": "House"})
    client.post("/locations/1/rooms", data={"name": "Garage", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Tools", "room_id": "1"})
    client.post("/boxes", data={"name": "Other"})  # not in any room
    page = client.get("/rooms/1/boxes").text
    assert "Tools" in page
    assert "Other" not in page


def test_location_detail_shows_rooms(client):
    client.post("/locations", data={"name": "House"})
    client.post(
        "/locations/1/floorplan",
        files={"image": ("plan.jpg", io.BytesIO(_fake_jpg()), "image/jpeg")},
    )
    client.post("/locations/1/rooms", data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/locations/1/rooms", data={"name": "Bedroom", "x": 0.4, "y": 0, "w": 0.3, "h": 0.3})
    page = client.get("/locations/1").text
    assert "Kitchen" in page
    assert "Bedroom" in page
    # Both rooms are rendered as rectangle overlays on the floorplan
    assert page.count('class="room-rect"') >= 2
