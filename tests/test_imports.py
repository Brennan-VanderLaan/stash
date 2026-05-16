"""Bulk-import flow.  Currently registered source: Encircle.

The tests below split into three concerns:

1. **Parser robustness** — the Encircle-specific header map and
   value cleaners survive the known wire quirks (``"Receipt"`` in
   a price column, ``P7Y`` in a warranty column, ``Upc:`` embedded
   in notes, header drift like ``"Model #"`` vs ``"Model Number"``).
2. **Executor mechanics** — items land in a per-import Location,
   rooms map 1:1, the existing loose-box helper gets called, the
   Undo cascade-deletes everything.
3. **Route contracts** — operator-less flow: anyone with a tenant
   can import; non-tenant 403s; malformed file 400s; the Undo
   route refuses to delete a non-import Location.
"""
from __future__ import annotations

import csv
import io

import pytest

from dao import imports as dao_imports


# ── Parser tests (encircle source) ──────────────────────────────────


def _csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode("utf-8")


def test_encircle_csv_maps_canonical_headers() -> None:
    """The canonical Encircle CSV — items, rooms, brand, etc. —
    binds every column to its internal field name."""
    data = _csv([
        ["Name", "Manufacturer", "Model Number", "Serial Number",
         "Room", "Quantity", "Notes"],
        ["Dishwasher", "Bosch", "SHE3ARF6UC/21", "FD940900406",
         "Kitchen", "1", "Quiet"],
    ])
    result = dao_imports.parse_encircle_csv(data)
    assert result.total_rows == 1
    assert len(result.items) == 1
    item = result.items[0]
    assert item["item_name"] == "Dishwasher"
    assert item["brand"] == "Bosch"
    assert item["model_number"] == "SHE3ARF6UC/21"
    assert item["serial_number"] == "FD940900406"
    assert item["room"] == "Kitchen"
    assert item["quantity"] == "1"
    assert item["notes"] == "Quiet"


def test_encircle_csv_handles_header_drift() -> None:
    """Header strings vary in the wild — "Model #" vs "Model Number",
    "Qty" vs "Quantity", trailing colons, mixed case.  All bind
    to the same internal field."""
    data = _csv([
        ["NAME", "Brand", "Model #", "qty", "SERIAL no", "Room:"],
        ["Lamp", "IKEA", "FOO123", "2", "X1", "Living Room"],
    ])
    result = dao_imports.parse_encircle_csv(data)
    item = result.items[0]
    assert item["item_name"] == "Lamp"
    assert item["brand"] == "IKEA"
    assert item["model_number"] == "FOO123"
    assert item["quantity"] == "2"
    assert item["serial_number"] == "X1"
    assert item["room"] == "Living Room"


def test_encircle_csv_skips_receipt_literal_in_price_column() -> None:
    """HomeBox discussion #1065 documents Encircle exporting the
    literal string "Receipt" in numeric purchase-price cells when
    the actual price is on a paper receipt.  Don't try to coerce
    that to a float — drop it silently."""
    data = _csv([
        ["Name", "Purchase Price"],
        ["Dishwasher", "Receipt"],
        ["Vitamix", "539.96"],
        ["Microwave", "$202.00"],
        ["Faucet", ""],
    ])
    result = dao_imports.parse_encircle_csv(data)
    prices = [r.get("purchase_price", "") for r in result.items]
    assert prices == ["", "539.96", "202.00", ""]


def test_encircle_csv_humanises_iso_warranty_durations() -> None:
    """``P7Y`` → "7 years"; ``P2Y3M`` → "2 years 3 months"; hand-
    typed warranties pass through unchanged."""
    data = _csv([
        ["Name", "Warranty Duration"],
        ["Vitamix", "P7Y"],
        ["Microwave", "P6M"],
        ["Toaster", "P2Y3M"],
        ["Toaster oven", "5 years"],
        ["Old kettle", "lifetime"],
        ["No data", ""],
    ])
    result = dao_imports.parse_encircle_csv(data)
    warranties = [r.get("warranty_duration", "") for r in result.items]
    assert warranties == [
        "7 years", "6 months", "2 years 3 months",
        "5 years", "lifetime", "",
    ]


def test_encircle_csv_drops_empty_and_nameless_rows() -> None:
    """Rows with no item_name get dropped (placeholders Encircle
    sometimes emits); fully-empty rows also drop."""
    data = _csv([
        ["Name", "Room"],
        ["Real item", "Kitchen"],
        ["", "Kitchen"],            # nameless — drop
        ["", ""],                   # entirely empty — drop
        ["Another real item", ""],
    ])
    result = dao_imports.parse_encircle_csv(data)
    names = [r["item_name"] for r in result.items]
    assert names == ["Real item", "Another real item"]


def test_encircle_csv_reports_unmapped_headers() -> None:
    """Columns the parser doesn't know how to bind end up in
    ``unmapped_headers`` so the UI can warn the operator without
    failing the import."""
    data = _csv([
        ["Name", "Room", "Mystery Column", "Another Mystery"],
        ["Item", "Kitchen", "x", "y"],
    ])
    result = dao_imports.parse_encircle_csv(data)
    assert "Mystery Column" in result.unmapped_headers
    assert "Another Mystery" in result.unmapped_headers


def test_encircle_csv_bom_tolerant() -> None:
    """Encircle's web-app CSV path emits UTF-8 with a BOM; the
    parser uses ``utf-8-sig`` so the BOM doesn't end up baked
    into the first header string."""
    body = b"\xef\xbb\xbfName,Room\nItem,Kitchen\n"
    result = dao_imports.parse_encircle_csv(body)
    assert result.items[0]["item_name"] == "Item"
    assert result.items[0]["room"] == "Kitchen"


def test_format_item_notes_skips_empty_fields() -> None:
    """A sparse row doesn't produce notes full of empty
    ``Brand:``-style sentinels — only present fields land."""
    notes = dao_imports.format_item_notes({
        "brand": "Bosch",
        "model_number": "",
        "serial_number": "ABC123",
        "purchase_price": "539.96",
        "purchase_vendor": "Lowes",
        "purchase_date": "2014-11-28",
        "warranty_duration": "7 years",
        "notes": "Original purchase receipt in file.",
    })
    assert "Brand: Bosch" in notes
    assert "Serial: ABC123" in notes
    assert "Purchase: Lowes · 2014-11-28 · $539.96" in notes
    assert "Warranty: 7 years" in notes
    assert "Original purchase receipt in file." in notes
    assert "Model:" not in notes  # empty field — excluded


# ── Dispatcher tests ────────────────────────────────────────────────


def test_parse_dispatcher_rejects_unknown_source() -> None:
    with pytest.raises(ValueError):
        dao_imports.parse("nope", "x.csv", b"a,b\n1,2\n")


def test_parse_dispatcher_rejects_unsupported_extension() -> None:
    with pytest.raises(ValueError):
        dao_imports.parse("encircle", "x.pdf", b"%PDF-1.4")


def test_parse_dispatcher_rejects_oversize_upload() -> None:
    big = b"a,b\n" + (b"1,2\n" * (dao_imports.MAX_IMPORT_BYTES))
    with pytest.raises(ValueError):
        dao_imports.parse("encircle", "x.csv", big)


def test_parse_dispatcher_stamps_source() -> None:
    """The returned ParseResult carries the source key it was
    dispatched against, so the executor + UI can render
    source-aware copy ("Imported from Encircle (…)")."""
    data = _csv([["Name"], ["Item"]])
    result = dao_imports.parse("encircle", "x.csv", data)
    assert result.source == "encircle"


# ── Executor tests ──────────────────────────────────────────────────


def test_execute_import_creates_location_rooms_loose_boxes_items(client):
    """The full end-to-end create — verifies every layer of the
    auto-created hierarchy lands as expected."""
    items = [
        {"item_name": "Dishwasher", "brand": "Bosch", "room": "Kitchen"},
        {"item_name": "Lamp", "brand": "IKEA", "room": "Living Room"},
        {"item_name": "Pan", "brand": "Lodge", "room": "Kitchen"},
    ]
    actor = _make_actor(client)
    result = client.app_module.dao_imports.execute_import(
        actor, items, source="encircle",
    )
    assert result["item_count"] == 3
    assert result["room_count"] == 2
    assert result["location_name"].startswith("Imported from Encircle")
    with client.app_module.db() as conn:
        loc = conn.execute(
            "SELECT id, name FROM locations WHERE id = ?",
            (result["location_id"],),
        ).fetchone()
        rooms = conn.execute(
            "SELECT name FROM rooms WHERE location_id = ? ORDER BY name",
            (result["location_id"],),
        ).fetchall()
        boxes = conn.execute(
            "SELECT b.name, b.is_loose FROM boxes b "
            "JOIN rooms r ON r.id = b.room_id "
            "WHERE r.location_id = ? ORDER BY r.name",
            (result["location_id"],),
        ).fetchall()
        items_rows = conn.execute(
            "SELECT i.name, b.id AS box_id, r.name AS room_name "
            "FROM items i "
            "JOIN boxes b ON b.id = i.box_id "
            "JOIN rooms r ON r.id = b.room_id "
            "WHERE r.location_id = ? ORDER BY i.name",
            (result["location_id"],),
        ).fetchall()
    assert loc["name"] == result["location_name"]
    assert [r["name"] for r in rooms] == ["Kitchen", "Living Room"]
    # Each room got exactly one loose box.
    assert len(boxes) == 2
    assert all(b["is_loose"] == 1 for b in boxes)
    assert {i["name"] for i in items_rows} == {"Dishwasher", "Lamp", "Pan"}


def test_execute_import_packs_metadata_into_item_notes(client):
    """Brand / model / serial / warranty / purchase fields land in
    the item's notes (no first-class columns yet).  Search + audit
    reads against the merged notes blob find what the user
    expects."""
    items = [{
        "item_name": "Dishwasher",
        "brand": "Bosch",
        "model_number": "SHE3ARF6UC/21",
        "serial_number": "FD940900406",
        "purchase_vendor": "Lowes",
        "purchase_date": "2014-11-28",
        "purchase_price": "539.96",
        "warranty_duration": "7 years",
        "room": "Kitchen",
    }]
    actor = _make_actor(client)
    client.app_module.dao_imports.execute_import(
        actor, items, source="encircle",
    )
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT notes FROM items WHERE name = 'Dishwasher'"
        ).fetchone()
    notes = row["notes"]
    assert "Brand: Bosch" in notes
    assert "Model: SHE3ARF6UC/21" in notes
    assert "Serial: FD940900406" in notes
    assert "Purchase: Lowes · 2014-11-28 · $539.96" in notes
    assert "Warranty: 7 years" in notes


def test_undo_import_cascades(client):
    """Undo deletes the whole import: location → floors → rooms →
    boxes → items, leaving zero traces other than the audit log."""
    items = [{"item_name": "Item", "room": "Kitchen"}]
    actor = _make_actor(client)
    result = client.app_module.dao_imports.execute_import(
        actor, items, source="encircle",
    )
    location_id = result["location_id"]
    client.app_module.dao_imports.undo_import(actor, location_id)
    with client.app_module.db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM locations WHERE id = ?",
            (location_id,),
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM rooms WHERE location_id = ?",
            (location_id,),
        ).fetchone()["n"] == 0


def test_undo_import_refuses_non_import_location(client):
    """Defends against an operator typing a real Location's id into
    the undo URL — refuses to cascade-delete a Location whose name
    doesn't carry the IMPORTED_LOCATION_PREFIX."""
    actor = _make_actor(client)
    with client.app_module.db() as conn:
        cur = conn.execute(
            "INSERT INTO locations (name, tenant_id) VALUES ('Real Home', ?)",
            (client.test_tenant_id,),
        )
        real_id = cur.lastrowid
        conn.commit()
    with pytest.raises(client.app_module.dao_imports.NotFoundError):
        client.app_module.dao_imports.undo_import(actor, real_id)
    # The real location is still there.
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT id FROM locations WHERE id = ?", (real_id,),
        ).fetchone()
    assert row is not None


# ── Route tests ─────────────────────────────────────────────────────


def test_import_page_renders_with_source_dropdown(client):
    """The form lists every registered source — currently Encircle."""
    page = client.get("/import").text
    assert "Encircle" in page
    assert '<input type="file"' in page
    assert "accept=\".xlsx,.xlsm,.csv\"" in page


def test_import_post_creates_items_and_redirects_with_summary(client):
    """End-to-end: upload a CSV → 303 redirect → query string carries
    item + room counts so the page banner renders the summary."""
    data = _csv([
        ["Name", "Room", "Brand"],
        ["Dishwasher", "Kitchen", "Bosch"],
        ["Lamp", "Living Room", "IKEA"],
    ])
    r = client.post(
        "/import",
        data={"source": "encircle"},
        files={"upload": ("export.csv", data, "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert "/import?" in location
    assert "items=2" in location
    assert "rooms=2" in location


def test_import_undo_route_works(client):
    """POST /import/{id}/undo deletes the import + redirects back
    to /import with a clean slate."""
    data = _csv([["Name", "Room"], ["Dishwasher", "Kitchen"]])
    client.post(
        "/import",
        data={"source": "encircle"},
        files={"upload": ("export.csv", data, "text/csv")},
    )
    with client.app_module.db() as conn:
        loc_id = conn.execute(
            "SELECT id FROM locations "
            "WHERE name LIKE 'Imported from Encircle%'"
        ).fetchone()["id"]
    r = client.post(
        f"/import/{loc_id}/undo", follow_redirects=False,
    )
    assert r.status_code == 303
    with client.app_module.db() as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) AS n FROM locations WHERE id = ?",
            (loc_id,),
        ).fetchone()["n"]
    assert cnt == 0


def test_import_post_rejects_unknown_extension(client):
    """Uploading a PDF (or anything that isn't .csv/.xlsx) gets a
    400 with a friendly message — no silent partial import."""
    r = client.post(
        "/import",
        data={"source": "encircle"},
        files={"upload": ("x.pdf", b"%PDF-1.4\n", "application/pdf")},
    )
    assert r.status_code == 400


# ── Shared test helpers ─────────────────────────────────────────────


def _make_actor(client):
    """Build a maintainer Actor for the test tenant — mirrors the
    in-process suite's TEST_EMAIL convention."""
    from dao._base import Actor
    return Actor(
        email=client.test_email,
        tenant_id=client.test_tenant_id,
        role="maintainer",
        is_operator=False,
        memberships=((client.test_tenant_id, "maintainer"),),
        shares=(),
    )
