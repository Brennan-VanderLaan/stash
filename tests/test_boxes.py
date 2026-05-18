import io


def _make_item(client, box_id, name="thing", tags=""):
    client.post(
        f"/boxes/{box_id}/items",
        data={"name": name, "tags": tags},
        files={"photo": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")},
    )


def test_edit_box_updates_fields(client):
    """The edit form persists name + notes.  Free-text ``location``
    is gone since feedback #78 (room picker is the only source of
    truth for where a box lives), so the form no longer has that
    field — POSTing a ``location`` value is silently ignored."""
    client.post("/boxes", data={"name": "Old name"})
    r = client.post(
        "/boxes/1/edit",
        data={"name": "Kitchen #1", "notes": "fragile"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    detail = client.get("/boxes/1").text
    assert "Kitchen #1" in detail
    assert "fragile" in detail


def test_box_edit_renders_two_step_room_picker(client):
    """Feedback #71: the flat <select> for the room field on
    /boxes/{id}'s edit form became a two-step picker — pick the
    location, then the room.  When there's only one location, the
    location step is skipped.  Pin the picker markup so a refactor
    doesn't silently fall back to the old <select>."""
    # Seed: one location, one floor, two rooms.
    from dao import Actor, locations as dao_locations, floors as dao_floors
    actor = Actor(
        email=client.test_email, tenant_id=client.test_tenant_id,
        role="maintainer", is_operator=False,
        memberships=((client.test_tenant_id, "maintainer"),),
    )
    loc_id = dao_locations.create(actor, "Home")
    floor_id = dao_floors.create(actor, loc_id, "Ground floor")
    with client.app_module.db() as conn:
        conn.execute(
            "INSERT INTO rooms (tenant_id, location_id, floor_id, name) "
            "VALUES (?, ?, ?, ?)",
            (client.test_tenant_id, loc_id, floor_id, "Kitchen"),
        )
        conn.execute(
            "INSERT INTO rooms (tenant_id, location_id, floor_id, name) "
            "VALUES (?, ?, ?, ?)",
            (client.test_tenant_id, loc_id, floor_id, "Living room"),
        )
        conn.commit()
    client.post("/boxes", data={"name": "B1"})
    page = client.get("/boxes/1").text
    # Picker container + hidden input.
    assert 'data-room-picker' in page
    assert 'data-room-picker-input' in page
    # Single-location → location step is hidden, room chips show
    # directly.  We rendered ``data-single-location="1"``.
    assert 'data-single-location="1"' in page
    # Both rooms render as chips.
    assert "Kitchen" in page
    assert "Living room" in page
    # The old <select name="room_id"> is GONE.
    assert '<select name="room_id">' not in page


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


def test_location_field_removed_from_box_edit_form(client):
    """Feedback #78 retired the free-text ``location`` input on the
    box edit form.  The room picker is now the only way to set
    where a box lives — no datalist + no free-text input.  Pin
    both the absence of the input AND the absence of the
    known-locations datalist so a future refactor doesn't bring
    them back."""
    client.post("/boxes", data={"name": "A"})
    page = client.get("/boxes/1").text
    assert 'name="location"' not in page
    assert 'id="known-locations"' not in page


def test_edit_item_updates_name_and_notes(client):
    """Feedback #78: the item-detail dialog rendered the name as
    static text — there was no way to rename a misclassified
    item.  POST /items/{id}/edit accepts ``name`` + ``notes``,
    updates the row, and 303s back to the box page with the
    item anchor so the dialog re-opens in flow."""
    client.post("/boxes", data={"name": "Kitchen"})
    _make_item(client, 1, "wodden spatla")

    r = client.post(
        "/items/1/edit",
        data={"name": "wooden spatula", "notes": "use for nonstick"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/boxes/1#item-1"

    page = client.get("/boxes/1").text
    assert "wooden spatula" in page
    assert "use for nonstick" in page
    assert "wodden spatla" not in page


def test_edit_item_rejects_empty_name(client):
    """An empty name would orphan the item visually — surface 400."""
    client.post("/boxes", data={"name": "Kitchen"})
    _make_item(client, 1, "spatula")
    r = client.post("/items/1/edit", data={"name": "  ", "notes": ""})
    assert r.status_code == 400


def test_edit_item_404_for_missing(client):
    r = client.post("/items/999/edit", data={"name": "ghost"})
    assert r.status_code == 404


def test_item_dialog_renders_inline_rename_form(client):
    """The dialog body must carry the edit form — both the name
    input (so the user can fix AI misclassifications) and the
    Save button.  Pins feedback #78 against template regressions."""
    client.post("/boxes", data={"name": "Kitchen"})
    _make_item(client, 1, "spatula")
    page = client.get("/boxes/1").text
    assert 'action="/items/1/edit"' in page
    assert 'name="name"' in page
    assert 'name="notes"' in page


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
    # ``type="checkbox"`` lives in the <noscript> fallback so JS-off
    # clients still complete an audit in one POST.
    assert 'type="checkbox"' in page


# ── Tinder-style swipe audit ────────────────────────────────────────


def test_audit_start_stamps_session(client):
    """``POST /boxes/{id}/audit/start`` writes
    last_audit_started_at so the per-item swipe endpoints know
    which items belong to the current session."""
    client.post("/boxes", data={"name": "Closet"})
    _make_item(client, 1, "scarf")
    r = client.post("/boxes/1/audit/start",
                    headers={"Accept": "application/json"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT last_audit_started_at FROM boxes WHERE id = 1"
        ).fetchone()
    assert row["last_audit_started_at"], "session timestamp not set"


def test_audit_mark_present_advances_last_seen(client):
    """``/audit/items/{id}/present`` stamps ``items.last_seen_at``
    so the resume query skips it on the next page load."""
    client.post("/boxes", data={"name": "Closet"})
    _make_item(client, 1, "scarf")
    client.post("/boxes/1/audit/start",
                headers={"Accept": "application/json"})
    r = client.post("/boxes/1/audit/items/1/present",
                    headers={"Accept": "application/json"})
    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True
    assert payload["remaining"] == 0  # only item, marked present
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT last_seen_at FROM items WHERE id = 1"
        ).fetchone()
    assert row["last_seen_at"], "last_seen_at not stamped"


def test_audit_mark_missing_moves_to_queue(client):
    """``/audit/items/{id}/missing`` extracts the item to the sort
    queue with provenance preserved + carries tags over."""
    client.post("/boxes", data={"name": "Garage"})
    _make_item(client, 1, "drill", tags="tools, room:garage")
    client.post("/boxes/1/audit/start",
                headers={"Accept": "application/json"})
    r = client.post("/boxes/1/audit/items/1/missing",
                    headers={"Accept": "application/json"})
    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True
    assert "pending_id" in payload
    # Item is gone from the box.
    with client.app_module.db() as conn:
        items = conn.execute(
            "SELECT id FROM items WHERE box_id = 1"
        ).fetchall()
        assert items == []
        # Pending row carries provenance + the original tags.
        pending = conn.execute(
            "SELECT name, previous_box_name FROM pending_items"
        ).fetchone()
        assert pending["name"] == "drill"
        assert pending["previous_box_name"] == "Garage"
        tag_count = conn.execute(
            "SELECT COUNT(*) FROM pending_item_tags"
        ).fetchone()[0]
        assert tag_count == 2  # both tags carried over


def test_audit_resume_skips_already_audited(client):
    """Reloading mid-session: items the user already swiped right
    on don't reappear in the deck, but items they haven't touched
    do."""
    client.post("/boxes", data={"name": "Drawer"})
    _make_item(client, 1, "fork")
    _make_item(client, 1, "spoon")
    _make_item(client, 1, "knife")
    client.post("/boxes/1/audit/start",
                headers={"Accept": "application/json"})
    # Confirm "fork" (id 1) — the other two should still be remaining.
    client.post("/boxes/1/audit/items/1/present",
                headers={"Accept": "application/json"})
    page = client.get("/boxes/1/audit").text
    # The remaining-cards deck should carry spoon + knife only.
    deck_idx = page.find('data-audit-deck')
    actions_idx = page.find('data-audit-actions')
    assert deck_idx >= 0 and actions_idx > deck_idx
    deck_html = page[deck_idx:actions_idx]
    assert "spoon" in deck_html
    assert "knife" in deck_html
    # "fork" only appears in the no-JS <noscript> fallback list,
    # not in the active swipe deck.
    assert "fork" not in deck_html


def test_audit_finish_clears_session_and_stamps_box(client):
    """``/audit/finish`` writes last_audited_at and clears the
    session start so a future Start resets cleanly."""
    client.post("/boxes", data={"name": "Pantry"})
    _make_item(client, 1, "rice")
    client.post("/boxes/1/audit/start",
                headers={"Accept": "application/json"})
    r = client.post("/boxes/1/audit/finish",
                    headers={"Accept": "application/json"})
    assert r.status_code == 200
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT last_audited_at, last_audit_started_at "
            "FROM boxes WHERE id = 1"
        ).fetchone()
    assert row["last_audited_at"], "last_audited_at not stamped"
    assert row["last_audit_started_at"] is None, "session not cleared"


def test_audit_swipe_endpoints_reject_cross_tenant(client):
    """Forged item-id pointing at someone else's box must 404 —
    the JOIN guard on the SQL keeps the swipe endpoints from
    leaking cross-tenant."""
    client.post("/boxes", data={"name": "Closet"})
    _make_item(client, 1, "scarf")
    r = client.post("/boxes/1/audit/items/999/present",
                    headers={"Accept": "application/json"})
    assert r.status_code == 404
    r = client.post("/boxes/1/audit/items/999/missing",
                    headers={"Accept": "application/json"})
    assert r.status_code == 404
