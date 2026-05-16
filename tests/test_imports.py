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


# ── Image extraction (XLSX embedded + media ZIP) ────────────────────


def _png_bytes(color: tuple = (255, 0, 0), size: tuple = (32, 32)) -> bytes:
    """Make a tiny solid-colour PNG for embedding into test fixtures."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def _xlsx_with_embedded_image(
    rows: list[list[str]],
    image_at: dict[int, bytes],
) -> bytes:
    """Build an XLSX fixture with rows of text + images anchored to
    specific 0-based row indices.  Mirrors what an Encircle export
    looks like: header at row 0, items + images at rows 1+.
    Returns the bytes ready to feed to ``parse_encircle_xlsx``."""
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage

    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    for row_idx, png in image_at.items():
        # openpyxl needs the image data as a BytesIO + a cell anchor.
        img = XLImage(io.BytesIO(png))
        # Anchor at column A of the target row.  Spreadsheet rows are
        # 1-based in Excel addressing, so row_idx (0-based) → "A{N+1}".
        ws.add_image(img, f"A{row_idx + 1}")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_encircle_xlsx_extracts_embedded_images(client):
    """XLSX path: an image anchored to a data row's first cell ends
    up on the corresponding parsed item under ``_image_bytes``.
    Other rows without images don't pick up phantom data."""
    png = _png_bytes()
    xlsx = _xlsx_with_embedded_image(
        rows=[
            ["Name", "Room"],          # row 0 — header
            ["Dishwasher", "Kitchen"],  # row 1 — gets the image
            ["Lamp", "Living Room"],    # row 2 — no image
        ],
        image_at={1: png},
    )
    result = client.app_module.dao_imports.parse_encircle_xlsx(xlsx)
    assert len(result.items) == 2
    by_name = {i["item_name"]: i for i in result.items}
    assert "_image_bytes" in by_name["Dishwasher"]
    assert by_name["Dishwasher"]["_image_bytes"] == png
    assert "_image_bytes" not in by_name["Lamp"]


def test_execute_import_saves_xlsx_embedded_image_via_photo_pipeline(client):
    """End-to-end XLSX → execute: the embedded image lands on
    disk as an encrypted blob with a real filename, and the
    created item's ``photo`` column points at it."""
    actor = _make_actor(client)
    png = _png_bytes(color=(0, 200, 0))
    xlsx = _xlsx_with_embedded_image(
        rows=[
            ["Name", "Room"],
            ["Dishwasher", "Kitchen"],
        ],
        image_at={1: png},
    )
    result = client.app_module.dao_imports.parse_encircle_xlsx(xlsx)
    client.app_module.dao_imports.execute_import(
        actor, result.items, source="encircle",
    )
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT photo FROM items WHERE name = 'Dishwasher'"
        ).fetchone()
    assert row["photo"], "item.photo not set"
    assert row["photo"].endswith(".jpg")
    blob = (client.app_module.UPLOAD_DIR
            / str(actor.tenant_id) / row["photo"])
    assert blob.exists()
    # On-disk bytes are encrypted — must not equal the cleartext
    # PNG (or the JPEG re-encode, but the simpler check is enough).
    assert png not in blob.read_bytes()


def test_encircle_media_zip_attaches_photos_by_room_and_name(client):
    """ZIP companion path: a media ZIP with ``Kitchen/Dishwasher.jpg``
    binds to the parsed item whose room is "Kitchen" and whose
    item_name starts with "Dishwasher"."""
    import zipfile
    parsed_result = client.app_module.dao_imports.ParseResult(
        items=[
            {"item_name": "Dishwasher", "room": "Kitchen"},
            {"item_name": "Lamp", "room": "Living Room"},
            {"item_name": "Mystery item", "room": "Garage"},
        ],
    )
    zip_buf = io.BytesIO()
    dishwasher_png = _png_bytes(color=(255, 0, 0))
    lamp_png = _png_bytes(color=(0, 0, 255))
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("Kitchen/Dishwasher.jpg", dishwasher_png)
        zf.writestr("Kitchen/Dishwasher_receipt.jpg", b"ignored")
        zf.writestr("Living Room/Lamp_bedside.jpg", lamp_png)
        # Garage/* missing — Mystery item gets nothing.
    attached = client.app_module.dao_imports.attach_encircle_media_zip(
        parsed_result, zip_buf.getvalue(),
    )
    assert attached == 2
    by_name = {i["item_name"]: i for i in parsed_result.items}
    assert by_name["Dishwasher"]["_image_bytes"] == dishwasher_png
    assert by_name["Lamp"]["_image_bytes"] == lamp_png
    assert "_image_bytes" not in by_name["Mystery item"]


def test_encircle_media_zip_does_not_overwrite_xlsx_embedded_photo(client):
    """Items that already have a primary photo from the XLSX path
    keep it — the ZIP only fills in gaps.  Preserves the "ZIP is
    secondary, XLSX is primary" assumption when both are present
    in the same upload."""
    xlsx_png = _png_bytes(color=(100, 100, 100))
    zip_png = _png_bytes(color=(200, 200, 200))
    parsed_result = client.app_module.dao_imports.ParseResult(
        items=[
            {
                "item_name": "Dishwasher", "room": "Kitchen",
                "_image_bytes": xlsx_png,
            },
        ],
    )
    import zipfile
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("Kitchen/Dishwasher.jpg", zip_png)
    attached = client.app_module.dao_imports.attach_encircle_media_zip(
        parsed_result, zip_buf.getvalue(),
    )
    assert attached == 0  # no slot to fill — keep the XLSX one
    assert parsed_result.items[0]["_image_bytes"] == xlsx_png


def test_encircle_media_zip_skips_receipt_and_datatag_photos(client):
    """``Kitchen/Dishwasher_receipt.jpg`` is not a primary photo —
    Stash's current single-photo column shouldn't get the receipt
    image bound as the item's hero shot.  Multi-photo support is
    on the V3+ roadmap."""
    parsed_result = client.app_module.dao_imports.ParseResult(
        items=[{"item_name": "Dishwasher", "room": "Kitchen"}],
    )
    import zipfile
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("Kitchen/Dishwasher_receipt.jpg", _png_bytes())
        zf.writestr("Kitchen/Dishwasher_datatag.jpg", _png_bytes())
    attached = client.app_module.dao_imports.attach_encircle_media_zip(
        parsed_result, zip_buf.getvalue(),
    )
    assert attached == 0
    assert "_image_bytes" not in parsed_result.items[0]


def test_import_post_accepts_media_zip_and_attaches_photos(client):
    """End-to-end via the HTTP route: upload CSV + paired media ZIP,
    verify the redirect carries ``photos=N`` and the created items
    have their photos pointed at encrypted blobs on disk."""
    import zipfile
    data = _csv([
        ["Name", "Room"],
        ["Dishwasher", "Kitchen"],
        ["Lamp", "Living Room"],
    ])
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("Kitchen/Dishwasher.jpg", _png_bytes(color=(10, 20, 30)))
        zf.writestr("Living Room/Lamp.jpg", _png_bytes(color=(40, 50, 60)))
    r = client.post(
        "/import",
        data={"source": "encircle"},
        files={
            "upload": ("export.csv", data, "text/csv"),
            "media_zip": ("photos.zip", zip_buf.getvalue(),
                          "application/zip"),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "photos=2" in r.headers["location"]
    # Both items have non-null photo filenames.
    with client.app_module.db() as conn:
        rows = conn.execute(
            "SELECT name, photo FROM items "
            "WHERE name IN ('Dishwasher', 'Lamp')"
        ).fetchall()
    assert all(row["photo"] for row in rows)
