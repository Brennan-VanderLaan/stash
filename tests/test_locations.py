"""Tests for the locations / floors / rooms model and the floorplan editor."""

import io
from PIL import Image


def _setup_location_and_floor_with_plan(client):
    """Stand up one location + one floor + one floorplan upload so
    the in-browser editor route has an image to render against."""
    loc_id, floor_id = _setup_location_with_floor(client)
    return loc_id, floor_id


def test_edit_image_renders_for_floor_with_floorplan(client):
    """``GET /floors/{id}/edit-image`` renders the Fabric.js editor
    when the floor has an existing floorplan to draw on."""
    _, fid = _setup_location_and_floor_with_plan(client)
    page = client.get(f"/floors/{fid}/edit-image").text
    assert 'id="floor-edit-canvas"' in page
    assert 'src="/static/vendor/fabric.min.js"' in page
    # Save target reuses the existing /floors/{id}/floorplan endpoint
    # so no new upload route is in play.
    assert f'"/floors/{fid}/floorplan"' in page


def test_edit_image_renders_blank_canvas_for_new_floor(client):
    """The editor doubles as the "create floorplan from scratch"
    surface: floors with no existing image still render the editor
    with a blank white canvas + a helper status line.  Save
    creates the floorplan via the same upload endpoint."""
    client.post("/locations", data={"name": "House"})
    client.post("/locations/1/floors", data={"name": "Main"})
    r = client.get("/floors/1/edit-image")
    assert r.status_code == 200
    page = r.text
    assert 'id="floor-edit-canvas"' in page
    # FLOORPLAN_URL is the JS-side toggle for "load image vs blank
    # canvas".  When the floor has no plan, the template emits
    # ``const FLOORPLAN_URL = null;``.
    assert "FLOORPLAN_URL = null" in page


def test_location_page_offers_browser_draw_when_no_floorplan(client):
    """When a fresh floor has no plan, the upload card now offers
    both "draw in the browser" + "upload an image" CTAs.  The
    user shouldn't be forced through a paint detour just to start."""
    client.post("/locations", data={"name": "House"})
    client.post("/locations/1/floors", data={"name": "Main"})
    page = client.get("/locations/1?floor=1&edit=1").text
    assert 'href="/floors/1/edit-image"' in page
    assert "Draw floorplan in the browser" in page


def test_edit_image_404_for_unknown_floor(client):
    r = client.get("/floors/999/edit-image", follow_redirects=False)
    assert r.status_code == 404


def test_location_page_links_to_editor(client):
    """The "Edit this floorplan in the browser" CTA on the floor
    view points at the new route."""
    loc_id, fid = _setup_location_and_floor_with_plan(client)
    page = client.get(f"/locations/{loc_id}?floor={fid}&edit=1").text
    assert f'href="/floors/{fid}/edit-image"' in page


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

def test_personal_tenant_backfill_for_existing_data(tmp_path, monkeypatch):
    """The first multi-tenancy migration takes a single-user DB with no
    tenant rows and folds every existing record into a Personal tenant,
    using STASH_BOOTSTRAP_MEMBER_EMAIL (preferred) or the first entry of
    STASH_ALLOWED_EMAILS as the sole maintainer.  Idempotent on second
    run."""
    import importlib
    import sqlite3
    import sys

    db_path = tmp_path / "single.db"
    monkeypatch.setenv("STASH_DB", str(db_path))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.setenv("STASH_BOOTSTRAP_MEMBER_EMAIL", "live@example.com")

    # Pre-multi-tenancy schema with one box + one item, simulating an
    # existing stash about to upgrade.
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE boxes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL, location TEXT, notes TEXT, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
        "CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "box_id INTEGER NOT NULL, name TEXT NOT NULL, notes TEXT, "
        "photo TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
    )
    conn.execute("INSERT INTO boxes (name) VALUES ('Garage')")
    conn.execute("INSERT INTO items (box_id, name) VALUES (1, 'drill')")
    conn.commit()
    conn.close()

    if "app" in sys.modules:
        del sys.modules["app"]
    import app
    importlib.reload(app)

    with app.db() as conn:
        tenants = [dict(r) for r in conn.execute("SELECT * FROM tenants").fetchall()]
        members = [dict(r) for r in conn.execute("SELECT * FROM tenant_members").fetchall()]
        boxes = [dict(r) for r in conn.execute("SELECT id, name, tenant_id FROM boxes").fetchall()]
        items = [dict(r) for r in conn.execute("SELECT id, name, tenant_id FROM items").fetchall()]

    assert len(tenants) == 1
    assert tenants[0]["name"] == "Personal"
    assert tenants[0]["plan"] == "pro"
    assert len(members) == 1
    assert members[0]["email"] == "live@example.com"
    assert members[0]["role"] == "maintainer"
    assert all(b["tenant_id"] == tenants[0]["id"] for b in boxes)
    assert all(i["tenant_id"] == tenants[0]["id"] for i in items)

    # Idempotent: a second migrate_db run leaves the tenant + member alone.
    app.migrate_db()
    with app.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tenant_members").fetchone()[0] == 1


def test_filesystem_migration_relocates_and_encrypts_legacy_cleartext(tmp_path, monkeypatch):
    """Pre-phase-2 cleartext photos in UPLOAD_DIR's flat root must be
    relocated to UPLOAD_DIR/{tenant_id}/{name} and re-written as
    encrypted blobs on the first migrate_db that sees them.  Idempotent
    on subsequent runs."""
    import importlib
    import sqlite3
    import sys

    db_path = tmp_path / "single.db"
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setenv("STASH_DB", str(db_path))
    monkeypatch.setenv("STASH_UPLOADS", str(upload_dir))
    monkeypatch.setenv("STASH_BOOTSTRAP_MEMBER_EMAIL", "live@example.com")

    # Pre-multi-tenancy schema with one item pointing at a cleartext
    # photo file in the flat root.
    cleartext = b"this is the photo's plaintext bytes"
    cleartext_path = upload_dir / "abcdef0123.jpg"
    cleartext_path.write_bytes(cleartext)

    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE boxes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL, location TEXT, notes TEXT, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
        "CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "box_id INTEGER NOT NULL, name TEXT NOT NULL, notes TEXT, "
        "photo TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
    )
    conn.execute("INSERT INTO boxes (name) VALUES ('Garage')")
    conn.execute("INSERT INTO items (box_id, name, photo) VALUES (1, 'drill', 'abcdef0123.jpg')")
    conn.commit()
    conn.close()

    if "app" in sys.modules:
        del sys.modules["app"]
    import app
    importlib.reload(app)

    # The original cleartext file is gone from the flat root, replaced
    # by an encrypted blob in tenant 1's directory.
    assert not cleartext_path.exists(), "cleartext file survived migration"
    encrypted_path = upload_dir / "1" / "abcdef0123.jpg"
    assert encrypted_path.exists(), "file did not relocate into tenant subdir"
    on_disk = encrypted_path.read_bytes()
    assert on_disk.startswith(app.vault.ENCRYPTED_MARKER), \
        "relocated file is not encrypted"
    assert cleartext not in on_disk, "cleartext bytes still present in ciphertext"

    # And the cleartext can still be retrieved through the decrypt path.
    decrypted = app._decrypt_for(1, on_disk)
    assert decrypted == cleartext

    # Idempotent: a second migrate_db run leaves the encrypted file as-is.
    mtime_before = encrypted_path.stat().st_mtime
    app.migrate_db()
    assert encrypted_path.read_bytes() == on_disk
    # mtime check is best-effort (filesystem timestamp resolution varies).


def test_personal_tenant_falls_back_to_allowed_emails(tmp_path, monkeypatch):
    """Existing single-user deploys have STASH_ALLOWED_EMAILS set, not
    STASH_BOOTSTRAP_MEMBER_EMAIL.  The migration falls back to the first
    entry of the former so an upgrade doesn't require new env config."""
    import importlib
    import sqlite3
    import sys

    db_path = tmp_path / "single.db"
    monkeypatch.setenv("STASH_DB", str(db_path))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.delenv("STASH_BOOTSTRAP_MEMBER_EMAIL", raising=False)
    monkeypatch.setenv("STASH_ALLOWED_EMAILS", "first@example.com,second@example.com")

    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE boxes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL, location TEXT, notes TEXT, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
    )
    conn.execute("INSERT INTO boxes (name) VALUES ('A')")
    conn.commit()
    conn.close()

    if "app" in sys.modules:
        del sys.modules["app"]
    import app
    importlib.reload(app)

    with app.db() as conn:
        members = [dict(r) for r in conn.execute("SELECT * FROM tenant_members").fetchall()]
    assert len(members) == 1
    assert members[0]["email"] == "first@example.com"


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


def test_locations_index_uses_floor_floorplan_when_legacy_column_empty(client):
    """A new location's floorplan lives on its floors, not on the legacy
    locations.floorplan column.  The index card has to look there too,
    otherwise a fully-floorplanned location displays "no floorplan"."""
    loc_id, _ = _setup_location_with_floor(client, "Townhouse", "First floor")
    # Sanity: locations.floorplan is NULL — the file is on the floor row.
    with client.app_module.db() as conn:
        loc_row = conn.execute(
            "SELECT floorplan FROM locations WHERE id = ?", (loc_id,)
        ).fetchone()
        floor_row = conn.execute(
            "SELECT floorplan FROM floors WHERE location_id = ?", (loc_id,)
        ).fetchone()
    assert loc_row["floorplan"] is None
    assert floor_row["floorplan"] is not None

    page = client.get("/locations").text
    assert "Townhouse" in page
    assert "no floorplan" not in page
    # The floor's floorplan filename should be the one rendered as the
    # preview thumb on the index card.
    assert f"/thumbs/{floor_row['floorplan']}" in page


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
    assert (client.app_module.UPLOAD_DIR / str(client.test_tenant_id) / plan).exists()


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
    assert not (client.app_module.UPLOAD_DIR / str(client.test_tenant_id) / first).exists()


def test_floorplan_files_are_protected_from_orphan_cleanup(client):
    """Maintenance cleanup must treat floor floorplans as referenced."""
    loc_id, floor_id = _setup_location_with_floor(client)
    with client.app_module.db() as conn:
        plan = conn.execute("SELECT floorplan FROM floors WHERE id = ?", (floor_id,)).fetchone()[0]
    client.app_module._run_orphan_cleanup()
    assert (client.app_module.UPLOAD_DIR / str(client.test_tenant_id) / plan).exists(), \
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

def test_box_create_with_room_id_links_room_only(client):
    """Feedback #78: free-text ``boxes.location`` is retired.  When
    a room is picked, the room link is the sole source of truth —
    boxes.location stays empty."""
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Garage", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Tools", "room_id": "1"})
    with client.app_module.db() as conn:
        row = conn.execute("SELECT room_id, location FROM boxes WHERE name = 'Tools'").fetchone()
    assert row["room_id"] == 1
    assert (row["location"] or "") == ""


def test_box_edit_can_unassign_room(client):
    """Clearing room_id leaves the box with no room link and an
    empty location (#78 — no fallback free-text)."""
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Garage", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Tools", "room_id": "1"})
    client.post("/boxes/1/edit", data={"name": "Tools", "room_id": ""})
    with client.app_module.db() as conn:
        row = conn.execute("SELECT room_id, location FROM boxes WHERE id = 1").fetchone()
    assert row["room_id"] is None
    assert (row["location"] or "") == ""


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


def test_move_box_to_room_endpoint_reassigns(client):
    """Floorplan drag-and-drop hits POST /boxes/{id}/move-to-room. Empty
    room_id clears the assignment; valid room_id reassigns the box.
    Feedback #78 retired the free-text ``boxes.location`` field — the
    room link is now the sole source of truth, so we only assert on
    room_id."""
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Garage", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Kitchen", "x": 0.4, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Tools", "room_id": "1"})

    r = client.post(
        "/boxes/1/move-to-room",
        data={"room_id": "2"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["room_id"] == 2
    with client.app_module.db() as conn:
        row = conn.execute("SELECT room_id, location FROM boxes WHERE id = 1").fetchone()
    assert row["room_id"] == 2

    # Empty clears it
    r = client.post(
        "/boxes/1/move-to-room",
        data={"room_id": ""},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    with client.app_module.db() as conn:
        row = conn.execute("SELECT room_id, location FROM boxes WHERE id = 1").fetchone()
    assert row["room_id"] is None


def test_box_color_override_persists_and_falls_back(client):
    """Boxes can override the room color via /boxes/{id}/edit. Empty value
    clears the override (falls back to room color). Off-palette values are
    silently rejected to NULL."""
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Garage", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Tools", "room_id": "1"})

    # Set a palette color
    client.post("/boxes/1/edit", data={
        "name": "Tools", "room_id": "1", "color": "#fbbf24",
    })
    with client.app_module.db() as conn:
        c = conn.execute("SELECT color FROM boxes WHERE id = 1").fetchone()[0]
    assert c == "#fbbf24"

    # Clear it back to inherit
    client.post("/boxes/1/edit", data={
        "name": "Tools", "room_id": "1", "color": "",
    })
    with client.app_module.db() as conn:
        c = conn.execute("SELECT color FROM boxes WHERE id = 1").fetchone()[0]
    assert c is None

    # Off-palette gets nulled
    client.post("/boxes/1/edit", data={
        "name": "Tools", "room_id": "1", "color": "#abc123",
    })
    with client.app_module.db() as conn:
        c = conn.execute("SELECT color FROM boxes WHERE id = 1").fetchone()[0]
    assert c is None


def test_floorplan_tile_uses_box_color_when_set(client):
    """When a box has its own color, the floorplan tile uses it instead
    of the room color. Tile inline style sets --tile-color."""
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Garage", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Tools", "room_id": "1"})
    client.post("/boxes/1/edit", data={
        "name": "Tools", "room_id": "1", "color": "#f87171",
    })
    page = client.get(f"/locations/{loc_id}").text
    # The room color is the auto-assigned palette entry (room 1 → #4ade80);
    # the box override should win on the tile.
    assert "--tile-color: #f87171" in page


def test_floorplan_tiles_emit_audit_and_created_dates(client):
    """Higher zoom tiers reveal box dates. The data has to be in the HTML
    even at base zoom — CSS hides them until zoom-tier ≥ 2."""
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Garage", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Tools", "room_id": "1"})
    # Trigger an audit so last_audited_at is non-null
    client.post(f"/boxes/1/audit", data={"found": "1"})  # no items, but stamps the box

    page = client.get(f"/locations/{loc_id}").text
    assert 'class="room-box-tile-detail"' in page
    # Created date is always present
    import re
    assert re.search(r'class="room-box-tile-meta">\s*\+ \d{4}-\d{2}-\d{2}', page)


def test_floorplan_tiles_emit_photo_mosaic_at_high_zoom(client):
    """At zoom-tier 3 each tile fills with item thumbnails. The HTML
    carries them at all zoom levels — CSS hides them at lower tiers.
    Each img also carries data-item-id + data-item-name so the
    drag-between-boxes handler has what it needs."""
    import io
    from PIL import Image
    loc_id, floor_id = _setup_location_with_floor(client)
    client.post(f"/floors/{floor_id}/rooms", data={"name": "Garage", "x": 0, "y": 0, "w": 0.3, "h": 0.3})
    client.post("/boxes", data={"name": "Tools", "room_id": "1"})
    # Add four items with photos so the server picks ⌈√4⌉ = 2 columns
    for i in range(4):
        buf = io.BytesIO()
        Image.new("RGB", (50, 50), color=(40, 80 + i*20, 30)).save(buf, format="JPEG")
        client.post(
            "/boxes/1/items",
            data={"name": f"item {i}"},
            files={"photo": (f"p{i}.jpg", io.BytesIO(buf.getvalue()), "image/jpeg")},
        )

    page = client.get(f"/locations/{loc_id}").text
    assert 'class="room-box-tile-mosaic"' in page
    assert 'data-photo-count="4"' in page
    # Server-computed --mosaic-cols is ⌈√4⌉ = 2
    assert '--mosaic-cols: 2' in page
    # Each img must carry the data attributes needed for item DnD
    import re
    mosaic = re.search(
        r'<span class="room-box-tile-mosaic"[^>]*>(.*?)</span>', page, re.S
    )
    assert mosaic
    body = mosaic.group(1)
    assert body.count('src="/thumbs/') == 4
    assert body.count('data-item-id=') == 4
    assert body.count('data-item-name=') == 4
    assert 'data-item-name="item 0"' in body


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


# ── Unassigned room rescue (feedback #76) ──────────────────────────


def test_attach_to_floor_dao_links_orphan_room(client):
    """A room with location_id set but floor_id NULL (orphan
    from the legacy free-text migration, or future AI-suggest
    edge case) becomes visible on the floorplan via
    dao_rooms.attach_to_floor."""
    loc_id, floor_id = _setup_location_with_floor(client)
    # Hand-create an orphan room: location_id set, floor_id NULL.
    with client.app_module.db() as conn:
        cur = conn.execute(
            "INSERT INTO rooms (location_id, name, tenant_id) "
            "VALUES (?, 'Orphan', ?)",
            (loc_id, client.test_tenant_id),
        )
        room_id = cur.lastrowid
        conn.commit()
    from dao import Actor, rooms as dao_rooms
    actor = Actor(
        email=client.test_email, tenant_id=client.test_tenant_id,
        role="maintainer", is_operator=False,
        memberships=((client.test_tenant_id, "maintainer"),),
    )
    returned_loc = dao_rooms.attach_to_floor(actor, room_id, floor_id)
    assert returned_loc == loc_id
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT floor_id, w, h FROM rooms WHERE id = ?",
            (room_id,),
        ).fetchone()
    assert row["floor_id"] == floor_id
    # Default coords give the room a visible rectangle, not 0×0.
    assert row["w"] > 0 and row["h"] > 0


def test_attach_to_floor_route_redirects_to_edit_mode(client):
    """POST /rooms/{id}/attach-to-floor 303s back to the location
    page in edit mode so the operator can drag the just-moved
    room into shape."""
    loc_id, floor_id = _setup_location_with_floor(client)
    with client.app_module.db() as conn:
        cur = conn.execute(
            "INSERT INTO rooms (location_id, name, tenant_id) "
            "VALUES (?, 'Orphan', ?)",
            (loc_id, client.test_tenant_id),
        )
        room_id = cur.lastrowid
        conn.commit()
    r = client.post(
        f"/rooms/{room_id}/attach-to-floor",
        data={"floor_id": str(floor_id)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert f"/locations/{loc_id}" in r.headers["location"]
    assert "edit=1" in r.headers["location"]


def test_unassigned_rooms_section_renders_move_form(client):
    """The /locations/{id} page surfaces orphans + per-row
    move-to-floor form so the operator can rescue them without
    SQL."""
    loc_id, floor_id = _setup_location_with_floor(client)
    with client.app_module.db() as conn:
        conn.execute(
            "INSERT INTO rooms (location_id, name, tenant_id) "
            "VALUES (?, 'Orphan', ?)",
            (loc_id, client.test_tenant_id),
        )
        conn.commit()
    page = client.get(f"/locations/{loc_id}").text
    assert "Unassigned rooms" in page
    assert "Orphan" in page
    # The action POSTs to the new attach-to-floor endpoint.
    assert "/attach-to-floor" in page
    # Single-floor case → button shows "Move to {floor_name} →".
    assert "Move to Main" in page


# ── Auto-create suggested room (feedback #76) ──────────────────────


def test_create_suggested_box_auto_creates_room_when_single_floor(
    client,
):
    """When the AI suggests a brand-new box with a brand-new
    room location AND the tenant has exactly one floor, the
    create-suggested-box flow now materialises the room on that
    floor instead of dropping the AI's location text as
    free-text on boxes.location.

    Result: the box ends up linked to a real room, which is
    visible on the floorplan as a default-sized rectangle in
    the top-left.  Operator drags it into shape later."""
    loc_id, floor_id = _setup_location_with_floor(client)
    # Seed a pending item that the AI flagged as ``match='new'``
    # with a brand-new room name ("Garage").
    with client.app_module.db() as conn:
        cur = conn.execute(
            "INSERT INTO pending_items "
            "(name, photo, "
            " suggested_new_box_name, suggested_new_box_location, "
            " tenant_id) "
            "VALUES ('wrench', 'fake.jpg', "
            "        'Tools', 'Garage', ?)",
            (client.test_tenant_id,),
        )
        pending_id = cur.lastrowid
        conn.commit()
    r = client.post(
        f"/queue/{pending_id}/create-suggested-box",
        follow_redirects=False,
    )
    assert r.status_code == 303
    # A new room called "Garage" was auto-created on the single
    # floor.
    with client.app_module.db() as conn:
        room = conn.execute(
            "SELECT id, floor_id, location_id, w, h FROM rooms "
            "WHERE LOWER(name) = 'garage' AND tenant_id = ?",
            (client.test_tenant_id,),
        ).fetchone()
        # The new box points at it.
        box = conn.execute(
            "SELECT room_id, location FROM boxes "
            "WHERE LOWER(name) = 'tools' AND tenant_id = ?",
            (client.test_tenant_id,),
        ).fetchone()
    assert room is not None
    assert room["floor_id"] == floor_id
    assert room["location_id"] == loc_id
    assert room["w"] > 0 and room["h"] > 0
    assert box is not None
    assert box["room_id"] == room["id"]
    # Box's location text is cleared once the room link is set.
    assert (box["location"] or "") == ""


def test_create_suggested_box_no_room_when_multi_floor(client):
    """If the tenant has more than one floor, the AI-suggested
    new room is ambiguous (which floor?) so we skip the auto-create.
    Feedback #78 retired the free-text ``boxes.location`` fallback —
    the box is created without a room link and the user can use the
    room picker to assign it later."""
    loc_id, floor_id_1 = _setup_location_with_floor(client)
    # Add a second floor to make the auto-create heuristic
    # decline.
    r = client.post(
        f"/locations/{loc_id}/floors", data={"name": "Basement"},
    )
    assert r.status_code in (200, 303)
    with client.app_module.db() as conn:
        cur = conn.execute(
            "INSERT INTO pending_items "
            "(name, photo, "
            " suggested_new_box_name, suggested_new_box_location, "
            " tenant_id) "
            "VALUES ('hammer', 'fake.jpg', "
            "        'Tools', 'Workshop', ?)",
            (client.test_tenant_id,),
        )
        pending_id = cur.lastrowid
        conn.commit()
    client.post(
        f"/queue/{pending_id}/create-suggested-box",
        follow_redirects=False,
    )
    with client.app_module.db() as conn:
        room = conn.execute(
            "SELECT id FROM rooms "
            "WHERE LOWER(name) = 'workshop' AND tenant_id = ?",
            (client.test_tenant_id,),
        ).fetchone()
        box = conn.execute(
            "SELECT room_id, location FROM boxes "
            "WHERE LOWER(name) = 'tools' AND tenant_id = ?",
            (client.test_tenant_id,),
        ).fetchone()
    # No auto-created room.
    assert room is None
    # Box exists but has no room and no free-text location.
    assert box is not None
    assert box["room_id"] is None
    assert (box["location"] or "") == ""
