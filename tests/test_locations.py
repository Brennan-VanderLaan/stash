"""Tests for the locations / floors / rooms model and the floorplan editor."""

import io
from PIL import Image


def _fake_jpg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), color=(20, 60, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _setup_location_with_floor(client, location_name: str = "House", floor_name: str = "Main"):
    """Create a location, add one floor, and upload a floorplan to it.
    Returns (location_id, floor_id) for chaining further setup."""
    client.post("/locations", data={"name": location_name})
    loc_id = client.app_module.db().execute(
        "SELECT id FROM locations WHERE name = ?", (location_name,)
    ).fetchone()["id"]
    client.post(f"/locations/{loc_id}/floors", data={"name": floor_name})
    floor_id = client.app_module.db().execute(
        "SELECT id FROM floors WHERE location_id = ? AND name = ?",
        (loc_id, floor_name),
    ).fetchone()["id"]
    client.post(
        f"/floors/{floor_id}/floorplan",
        files={"image": ("plan.jpg", io.BytesIO(_fake_jpg()), "image/jpeg")},
    )
    return loc_id, floor_id


# ── Migration: legacy text locations → rooms ─────────────────────────

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
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.5, "h": 0.5})
    client.app_module.migrate_db()
    with client.app_module.db() as conn:
        loc_count = conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0]
        floor_count = conn.execute("SELECT COUNT(*) FROM floors").fetchone()[0]
        room_count = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
    assert loc_count == 1
    assert floor_count == 1
    assert room_count == 1


# ── Migration: legacy floorplans → floors ────────────────────────────

def test_legacy_locations_with_floorplans_get_default_floor(tmp_path, monkeypatch):
    """A pre-multi-floor DB had locations.floorplan + rooms.location_id directly.
    The new migration must create a 'Main floor' under each such location and
    point the existing rooms at it."""
    import importlib
    import sqlite3
    import sys
    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("STASH_DB", str(db_path))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))

    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE locations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL, floorplan TEXT, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
        "CREATE TABLE rooms (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "location_id INTEGER NOT NULL, name TEXT NOT NULL, "
        "x REAL DEFAULT 0, y REAL DEFAULT 0, w REAL DEFAULT 0, h REAL DEFAULT 0, "
        "color TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
        "CREATE TABLE boxes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT, location TEXT, notes TEXT, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
    )
    conn.execute("INSERT INTO locations (name, floorplan) VALUES ('House', 'plan.jpg')")
    conn.execute(
        "INSERT INTO rooms (location_id, name, x, y, w, h, color) "
        "VALUES (1, 'Kitchen', 0.1, 0.1, 0.3, 0.3, '#4ade80')",
    )
    conn.execute(
        "INSERT INTO rooms (location_id, name, x, y, w, h, color) "
        "VALUES (1, 'Garage', 0.5, 0.5, 0.4, 0.4, '#60a5fa')",
    )
    conn.commit()
    conn.close()

    if "app" in sys.modules:
        del sys.modules["app"]
    import app
    importlib.reload(app)

    with app.db() as conn:
        floors = conn.execute("SELECT * FROM floors").fetchall()
        rooms = conn.execute("SELECT name, floor_id FROM rooms ORDER BY name").fetchall()
    assert len(floors) == 1
    assert floors[0]["name"] == "Main floor"
    assert floors[0]["floorplan"] == "plan.jpg"
    assert all(r["floor_id"] == floors[0]["id"] for r in rooms), \
        "all legacy rooms should be linked to the auto-created floor"


# ── Locations CRUD (no floorplan-on-location anymore) ────────────────

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


def test_delete_location_cascades_to_floors_and_rooms(client):
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/locations/1/delete", data={"confirm": "House"})
    with client.app_module.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM floors").fetchone()[0] == 0


def test_delete_location_unassigns_boxes(client):
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Mugs", "room_id": "1"})
    client.post("/locations/1/delete", data={"confirm": "House"})
    with client.app_module.db() as conn:
        row = conn.execute("SELECT room_id FROM boxes WHERE name = 'Mugs'").fetchone()
    assert row["room_id"] is None, "box should remain but be unassigned"


# ── Floors CRUD ──────────────────────────────────────────────────────

def test_create_floor_appends_with_correct_sort_order(client):
    client.post("/locations", data={"name": "House"})
    client.post("/locations/1/floors", data={"name": "Ground floor"})
    client.post("/locations/1/floors", data={"name": "2nd floor"})
    client.post("/locations/1/floors", data={"name": "Basement"})
    with client.app_module.db() as conn:
        rows = conn.execute(
            "SELECT name, sort_order FROM floors ORDER BY sort_order"
        ).fetchall()
    names = [r["name"] for r in rows]
    assert names == ["Ground floor", "2nd floor", "Basement"]
    # sort_order assignment is monotonic
    assert [r["sort_order"] for r in rows] == [0, 1, 2]


def test_floor_tabs_render_on_location_page(client):
    client.post("/locations", data={"name": "House"})
    client.post("/locations/1/floors", data={"name": "Ground"})
    client.post("/locations/1/floors", data={"name": "Attic"})
    page = client.get("/locations/1").text
    assert "Ground" in page
    assert "Attic" in page
    assert "floor-tab" in page


def test_select_specific_floor_via_query_param(client):
    client.post("/locations", data={"name": "House"})
    client.post("/locations/1/floors", data={"name": "Ground"})
    client.post("/locations/1/floors", data={"name": "Attic"})
    # Defaulting to first floor
    page = client.get("/locations/1").text
    assert 'class="floor-tab active"' in page
    # Asking for the second floor explicitly
    page = client.get("/locations/1?floor=2").text
    # Active tab text should now be "Attic"
    assert 'class="floor-tab active"\n       role="tab">\n      Attic' in page or "Attic</a>" in page


def test_rename_floor(client):
    loc_id, floor_id = _setup_location_with_floor(client, floor_name="Ground")
    client.post(f"/floors/{floor_id}", data={"name": "Ground floor"})
    with client.app_module.db() as conn:
        name = conn.execute("SELECT name FROM floors WHERE id = ?", (floor_id,)).fetchone()[0]
    assert name == "Ground floor"


def test_delete_floor_cascades_rooms(client):
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post(f"/floors/{floor_id}/delete")
    with client.app_module.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM floors").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0


# ── Floorplan upload (per floor now) ─────────────────────────────────

def test_upload_floorplan_to_floor_saves_image(client):
    loc_id, floor_id = _setup_location_with_floor(client)
    with client.app_module.db() as conn:
        plan = conn.execute("SELECT floorplan FROM floors WHERE id = ?", (floor_id,)).fetchone()[0]
    assert plan
    assert (client.app_module.UPLOAD_DIR / plan).exists()


def test_replace_floor_floorplan_cleans_up_old_file(client):
    loc_id, floor_id = _setup_location_with_floor(client)
    with client.app_module.db() as conn:
        first = conn.execute("SELECT floorplan FROM floors WHERE id = ?", (floor_id,)).fetchone()[0]
    client.post(
        f"/floors/{floor_id}/floorplan",
        files={"image": ("b.jpg", io.BytesIO(_fake_jpg()), "image/jpeg")},
    )
    with client.app_module.db() as conn:
        second = conn.execute("SELECT floorplan FROM floors WHERE id = ?", (floor_id,)).fetchone()[0]
    assert first != second
    assert not (client.app_module.UPLOAD_DIR / first).exists()


def test_floorplan_files_are_protected_from_orphan_cleanup(client):
    """Maintenance cleanup must treat floor floorplans as referenced."""
    loc_id, floor_id = _setup_location_with_floor(client)
    with client.app_module.db() as conn:
        plan = conn.execute("SELECT floorplan FROM floors WHERE id = ?", (floor_id,)).fetchone()[0]
    client.post("/maintenance/cleanup")
    assert (client.app_module.UPLOAD_DIR / plan).exists(), \
        "cleanup deleted referenced floorplan"


# ── Rooms CRUD ───────────────────────────────────────────────────────

def test_create_room_under_floor_assigns_color(client):
    loc_id, floor_id = _setup_location_with_floor(client)
    r = client.post(
        f"/floors/{floor_id}/rooms",
        data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["color"].startswith("#")
    assert payload["name"] == "Kitchen"


def test_room_create_clamps_oversized_coordinates(client):
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(
        f"/floors/{floor_id}/rooms",
        data={"name": "Big", "x": -0.2, "y": 0.5, "w": 5, "h": 1.2},
    )
    with client.app_module.db() as conn:
        row = conn.execute("SELECT x, y, w, h FROM rooms WHERE id = 1").fetchone()
    assert 0 <= row["x"] <= 1
    assert 0 <= row["y"] <= 1
    assert 0 <= row["w"] <= 1
    assert 0 <= row["h"] <= 1


def test_edit_room_updates_geometry_and_name(client):
    """The editor's drag-to-move and resize-handles fire POST /rooms/{id} with
    new geometry. Verify it's persisted as expected."""
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
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


def test_edit_room_persists_color_from_palette(client):
    """Color picker in the room edit modal sends a palette-validated color
    via the same /rooms/{id} endpoint. Must accept it and reject hexes
    outside the palette."""
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3})

    # Set a palette-valid color via the edit endpoint
    r = client.post(
        "/rooms/1",
        data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3, "color": "#f87171"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["color"] == "#f87171"

    # An off-palette color is rejected silently — keep the last good color.
    r = client.post(
        "/rooms/1",
        data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3, "color": "#abc123"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["color"] == "#f87171"


def test_room_geometry_can_be_moved_without_renaming(client):
    """The drag-to-move flow resends the existing name + new x/y. The endpoint
    must accept that as a no-op rename plus a geometry update."""
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post(
        "/rooms/1",
        data={"name": "Kitchen", "x": 0.5, "y": 0.5, "w": 0.3, "h": 0.3},
        headers={"Accept": "application/json"},
    )
    with client.app_module.db() as conn:
        row = conn.execute("SELECT name, x, y FROM rooms WHERE id = 1").fetchone()
    assert row["name"] == "Kitchen"
    assert abs(row["x"] - 0.5) < 1e-6
    assert abs(row["y"] - 0.5) < 1e-6


def test_delete_room_unassigns_boxes(client):
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Mugs", "room_id": "1"})
    client.post("/rooms/1/delete", headers={"Accept": "application/json"})
    with client.app_module.db() as conn:
        row = conn.execute("SELECT room_id FROM boxes WHERE name = 'Mugs'").fetchone()
    assert row["room_id"] is None


def test_room_distinct_colors(client):
    loc_id, floor_id = _setup_location_with_floor(client)
    seen = set()
    for i in range(4):
        r = client.post(
            f"/floors/{floor_id}/rooms",
            data={"name": f"Room {i}", "x": 0, "y": 0, "w": 0.1, "h": 0.1},
            headers={"Accept": "application/json"},
        )
        seen.add(r.json()["color"])
    assert len(seen) == 4


def test_rooms_on_different_floors_share_same_location_color_pool(client):
    """Color uniqueness is per-LOCATION (across floors), not per-floor — so
    rooms across two floors of the same house don't end up with clashing hues."""
    client.post("/locations", data={"name": "House"})
    client.post("/locations/1/floors", data={"name": "Ground"})
    client.post("/locations/1/floors", data={"name": "Attic"})
    with client.app_module.db() as conn:
        floor_rows = conn.execute("SELECT id, name FROM floors ORDER BY id").fetchall()
    floors_by_name = {f["name"]: f["id"] for f in floor_rows}

    seen = set()
    for floor_name in ("Ground", "Attic"):
        for i in range(3):
            r = client.post(
                f"/floors/{floors_by_name[floor_name]}/rooms",
                data={"name": f"{floor_name} {i}", "x": 0, "y": 0, "w": 0.1, "h": 0.1},
                headers={"Accept": "application/json"},
            )
            seen.add(r.json()["color"])
    assert len(seen) == 6, "expected 6 distinct colors across two floors of one location"


# ── Box ↔ room integration ──────────────────────────────────────────

def test_box_create_with_room_id_denormalizes_location(client):
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Garage", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Tools", "room_id": "1"})
    with client.app_module.db() as conn:
        row = conn.execute("SELECT room_id, location FROM boxes WHERE name = 'Tools'").fetchone()
    assert row["room_id"] == 1
    assert row["location"] == "Garage"


def test_box_edit_can_unassign_room(client):
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Garage", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Tools", "room_id": "1"})
    client.post("/boxes/1/edit", data={"name": "Tools", "room_id": "", "location": "shed"})
    with client.app_module.db() as conn:
        row = conn.execute("SELECT room_id, location FROM boxes WHERE id = 1").fetchone()
    assert row["room_id"] is None
    assert row["location"] == "shed"


def test_box_detail_links_to_floorplan_with_room_anchor(client):
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Garage", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Tools", "room_id": "1"})
    page = client.get("/boxes/1").text
    assert 'href="/locations/1#room-1"' in page
    assert "Garage" in page


def test_room_boxes_view_lists_assigned_boxes(client):
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Garage", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Tools", "room_id": "1"})
    client.post("/boxes", data={"name": "Other"})
    page = client.get("/rooms/1/boxes").text
    assert "Tools" in page
    assert "Other" not in page


def test_location_detail_renders_room_overlays(client):
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Bedroom", "x": 0.4, "y": 0, "w": 0.3, "h": 0.3})
    page = client.get(f"/locations/{loc_id}").text
    assert "Kitchen" in page
    assert "Bedroom" in page
    assert page.count('class="room-rect"') >= 2


def test_floorplan_view_mode_renders_box_tiles_inside_rooms(client):
    """View-mode floorplan now shows each room's boxes as tappable tiles
    inside the room rectangle. Edit mode hides them so the user can drag
    rooms without tiles getting in the way."""
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Garage", "x": 0, "y": 0, "w": 0.4, "h": 0.4})
    client.post("/boxes", data={"name": "Tools", "room_id": "1"})
    client.post("/boxes", data={"name": "Camping gear", "room_id": "1"})

    view = client.get(f"/locations/{loc_id}").text
    assert 'class="room-box-tile"' in view
    assert ">Tools<" in view or "Tools</span>" in view
    assert "Camping gear" in view

    edit = client.get(f"/locations/{loc_id}?edit=1").text
    assert 'class="room-box-tile"' not in edit, \
        "tiles should be suppressed in edit mode so the user can move rooms freely"


def test_box_preview_endpoint_returns_compact_card(client):
    """Floorplan tile click hits this endpoint and stuffs the result into a
    modal. Returns a small partial with thumbnails + an "Open box" link."""
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Garage", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Tools", "room_id": "1"})
    r = client.get("/boxes/1/preview")
    assert r.status_code == 200
    body = r.text
    assert "Tools" in body
    assert 'href="/boxes/1"' in body
    # Label download is a sibling action
    assert "/boxes/1/label.svg" in body


def test_floorplan_viewport_carries_zoom_controls(client):
    """The new pan/zoom UX requires the viewport wrapper + the toolbar
    of +/−/fit buttons + the zoom label."""
    loc_id, floor_id = _setup_location_with_floor(client)
    page = client.get(f"/locations/{loc_id}").text
    assert 'class="floorplan-viewport"' in page
    assert 'data-zoom="+1"' in page
    assert 'data-zoom="-1"' in page
    assert 'data-zoom-fit' in page
    assert 'data-zoom-label' in page


def test_edit_mode_renders_resize_handles(client):
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Kitchen", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    page = client.get(f"/locations/{loc_id}?edit=1").text
    # Four corners → four handles per room
    for handle in ("handle-nw", "handle-ne", "handle-sw", "handle-se"):
        assert handle in page, f"missing {handle} in edit mode"
