import pytest


# ── Single-cell label ──────────────────────────────────────────────


def test_single_label_svg_downloads(client):
    client.post("/boxes", data={
        "name": "Kitchen #1", "location": "Garage shelf B",
        "notes": "Mugs and small appliances",
    })
    r = client.get("/boxes/1/label.svg")
    assert r.status_code == 200
    assert "image/svg+xml" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    svg = r.text
    assert "<svg" in svg
    assert "Kitchen #1" in svg
    # Notes are the on-label description; location is intentionally
    # dropped to keep the printed label dead simple.
    assert "Mugs and small appliances" in svg
    assert "Garage shelf B" not in svg
    # Box ID badge is rendered as `#1` so you can reference boxes verbally.
    assert ">#1<" in svg
    # QR is rendered as a path, so the encoded string never appears as text.
    assert "stash:box:1" not in svg


def test_single_label_404_for_unknown_box(client):
    assert client.get("/boxes/999/label.svg").status_code == 404


def test_single_label_respects_persisted_orientation(client):
    """Default landscape; flipping label_orientation to portrait
    rotates the rendered cell content 90° within the same
    physical cell."""
    client.post("/boxes", data={"name": "Portrait Box"})
    landscape = client.get("/boxes/1/label.svg").text
    assert "rotate(90" not in landscape

    # Flip to portrait via the new endpoint.
    r = client.post(
        "/boxes/1/label-orientation",
        data={"orientation": "portrait"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)

    portrait = client.get("/boxes/1/label.svg").text
    # Portrait rendering rotates the inner content; a 90° rotation
    # appears in the SVG transform list.
    assert "rotate(90" in portrait


def test_label_orientation_rejects_garbage(client):
    client.post("/boxes", data={"name": "x"})
    r = client.post(
        "/boxes/1/label-orientation",
        data={"orientation": "diagonal"},
        follow_redirects=False,
    )
    assert r.status_code == 400


# ── Avery format registry ──────────────────────────────────────────


def test_avery_registry_defaults_to_5523():
    import labels
    assert labels.DEFAULT_FORMAT_SKU == "5523"
    fmt = labels.get_format(None)
    assert fmt.sku == "5523"
    assert fmt.cols == 2 and fmt.rows == 5
    assert fmt.labels_per_page == 10


def test_avery_registry_unknown_falls_back_to_default():
    import labels
    fmt = labels.get_format("does_not_exist")
    assert fmt.sku == "5523"


def test_avery_registry_resolves_known_skus():
    import labels
    f5160 = labels.get_format("5160")
    assert f5160.cols == 3 and f5160.rows == 10
    assert f5160.labels_per_page == 30
    f5164 = labels.get_format("5164")
    assert f5164.cols == 2 and f5164.rows == 3
    assert f5164.labels_per_page == 6


def test_cell_xy_marches_columns_then_rows():
    """Cell index 0 is top-left, then we fill row by row.  Math
    is the load-bearing piece — a typo here would print every
    label on top of itself."""
    import labels
    fmt = labels.get_format("5523")
    x0, y0 = fmt.cell_xy(0)
    x1, y1 = fmt.cell_xy(1)   # next column, same row
    x2, y2 = fmt.cell_xy(2)   # back to col 0, second row
    assert x0 == fmt.margin_left_mm
    assert x1 == fmt.margin_left_mm + fmt.label_w_mm + fmt.col_gap_mm
    assert y1 == y0
    assert x2 == x0
    assert y2 > y0


# ── Labels page renders ────────────────────────────────────────────


def test_labels_page_lists_boxes(client):
    client.post("/boxes", data={"name": "Box A"})
    client.post("/boxes", data={"name": "Box B"})
    page = client.get("/labels").text
    assert "Box A" in page
    assert "Box B" in page
    assert "box_ids" in page  # checkboxes present


def test_labels_page_carries_format_choice(client):
    """``?format=…`` must thread through into the print + PDF
    URLs the page renders so a user picking 5160 from the
    dropdown gets a 5160-shaped print job."""
    client.post("/boxes", data={"name": "Alpha"})
    page = client.get("/labels?format=5160").text
    assert "Avery 5160" in page
    assert "30 per sheet" in page
    assert "format=5160" in page


# ── PDF (Cairo) ────────────────────────────────────────────────────


def _require_cairo_runtime():
    """cairosvg's ``import`` itself raises OSError when libcairo
    isn't on the box (e.g. Windows dev).  pytest.importorskip
    only catches ImportError, so we have to do this manually.
    The Linux container has libcairo2 installed via apt — these
    tests skip locally and run there."""
    try:
        import cairosvg  # noqa: F401
        import pypdf     # noqa: F401
    except (ImportError, OSError) as e:
        pytest.skip(f"cairosvg/libcairo unavailable: {e}")
    try:
        cairosvg.svg2pdf(
            bytestring=b'<svg xmlns="http://www.w3.org/2000/svg" '
                       b'width="1mm" height="1mm"></svg>',
        )
    except OSError as e:
        pytest.skip(f"libcairo not loadable: {e}")


def test_sheet_pdf_default_format_5523(client):
    """11 boxes at format 5523 (10 per sheet) → 2 PDF pages."""
    _require_cairo_runtime()
    for i in range(11):
        client.post("/boxes", data={"name": f"Box {i:02d}"})
    r = client.get("/labels/sheet.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-"
    import io
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(r.content))
    assert len(reader.pages) == 2


def test_sheet_pdf_format_param_changes_layout(client):
    """Same 11 boxes at 5160 (30 per sheet) → 1 page."""
    _require_cairo_runtime()
    for i in range(11):
        client.post("/boxes", data={"name": f"Box {i:02d}"})
    r = client.get("/labels/sheet.pdf?format=5160")
    assert r.status_code == 200
    import io
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(r.content))
    assert len(reader.pages) == 1


def test_sheet_pdf_with_selection(client):
    _require_cairo_runtime()
    client.post("/boxes", data={"name": "Alpha"})
    client.post("/boxes", data={"name": "Bravo"})
    r = client.get("/labels/sheet.pdf?box_ids=1")
    assert r.status_code == 200
    import io
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(r.content))
    assert len(reader.pages) == 1


def test_sheet_pdf_filename_includes_format(client):
    _require_cairo_runtime()
    client.post("/boxes", data={"name": "x"})
    r = client.get("/labels/sheet.pdf?format=5164")
    assert "stash-labels-5164.pdf" in r.headers["content-disposition"]


# ── HTML print preview ─────────────────────────────────────────────


def test_print_page_paginates_with_breaks(client):
    """11 boxes at 5523 (10 per sheet) → 2 sheets in the print
    HTML, each in its own page-break-after div."""
    for i in range(11):
        client.post("/boxes", data={"name": f"Box {i}"})
    r = client.get("/labels/print")
    assert r.status_code == 200
    html = r.text
    assert html.count('class="sheet"') == 2
    assert "page-break-after" in html
    assert "11</strong> label" in html
    assert "<strong>2</strong> sheet" in html
    assert "Avery 5523" in html


def test_print_page_format_param_changes_pagination(client):
    """Same 11 boxes at 5160 (30 per sheet) → 1 sheet."""
    for i in range(11):
        client.post("/boxes", data={"name": f"Box {i}"})
    r = client.get("/labels/print?format=5160")
    html = r.text
    assert html.count('class="sheet"') == 1
    assert "Avery 5160" in html


def test_print_page_handles_empty_selection(client):
    client.post("/boxes", data={"name": "Solo"})
    r = client.get("/labels/print?box_ids=999")  # no real selection
    assert r.status_code == 200
    assert "No boxes selected" in r.text


# ── QR + content ──────────────────────────────────────────────────


def test_long_name_fits_label(client):
    long_name = "Box of interesting crap for project xyz pt 2"
    client.post("/boxes", data={"name": long_name, "notes": "miscellany"})
    r = client.get("/boxes/1/label.svg")
    assert r.status_code == 200
    svg = r.text
    assert long_name in svg
    assert "miscellany" in svg
    # ID badge format is the bare ``#1`` style, not ``ID: 1``.
    assert "ID:" not in svg
    assert ">#1<" in svg


def test_qr_payload_uses_public_url_when_set():
    import labels
    assert labels._qr_data_for_box(7, "https://stash.example.com") == \
        "https://stash.example.com/boxes/7"
    assert labels._qr_data_for_box(7, "https://stash.example.com/") == \
        "https://stash.example.com/boxes/7"
    assert labels._qr_data_for_box(7, "") == "stash:box:7"


def test_label_escapes_special_chars(client):
    client.post("/boxes", data={"name": "Tom & Jerry's <box>"})
    r = client.get("/boxes/1/label.svg")
    svg = r.text
    assert "&amp;" in svg
    assert "&lt;box&gt;" in svg
    assert "Tom & Jerry" not in svg  # raw & would be invalid SVG


# ── Background art (Nano Banana 2) ─────────────────────────────────


def test_generate_art_endpoint_saves_and_links_image(client, monkeypatch):
    client.post("/boxes", data={"name": "Bedroom Clothing", "notes": "shirts and socks"})
    fake_jpg = _fake_jpg_bytes()

    def fake_gen(name, description="", items=None, item_photos=None):
        assert name == "Bedroom Clothing"
        assert "shirts" in description
        return fake_jpg

    monkeypatch.setattr(client.app_module.vision, "generate_label_art", fake_gen)

    r = client.post("/boxes/1/generate-art", follow_redirects=False)
    assert r.status_code == 303
    with client.app_module.db() as conn:
        row = conn.execute("SELECT background_art FROM boxes WHERE id = 1").fetchone()
    assert row["background_art"], "background_art column not set after generation"
    assert (client.app_module.UPLOAD_DIR / str(client.test_tenant_id)
            / row["background_art"]).exists()


def test_label_svg_embeds_background_art_when_set(client, monkeypatch):
    client.post("/boxes", data={"name": "Crochet Box"})
    monkeypatch.setattr(
        client.app_module.vision, "generate_label_art",
        lambda *args, **kwargs: _fake_jpg_bytes(),
    )
    client.post("/boxes/1/generate-art")

    svg = client.get("/boxes/1/label.svg").text
    # Embedded as a base64 data URI so the SVG is self-contained.
    assert "<image" in svg
    assert "data:image/jpeg;base64," in svg
    # Faded so QR + text remain readable, but visible after print.
    assert 'opacity="0.3"' in svg


def test_clear_art_drops_image_and_orphans_file(client, monkeypatch):
    client.post("/boxes", data={"name": "Coat Box"})
    monkeypatch.setattr(
        client.app_module.vision, "generate_label_art",
        lambda *args, **kwargs: _fake_jpg_bytes(),
    )
    client.post("/boxes/1/generate-art")
    with client.app_module.db() as conn:
        art = conn.execute("SELECT background_art FROM boxes WHERE id = 1").fetchone()[0]
    assert (client.app_module.UPLOAD_DIR / str(client.test_tenant_id) / art).exists()

    r = client.post("/boxes/1/clear-art", follow_redirects=False)
    assert r.status_code == 303
    with client.app_module.db() as conn:
        row = conn.execute("SELECT background_art FROM boxes WHERE id = 1").fetchone()
    assert row["background_art"] is None
    assert not (client.app_module.UPLOAD_DIR / str(client.test_tenant_id)
                / art).exists(), "orphan art file leaked"


def test_generate_art_returns_json_for_ajax_clients(client, monkeypatch):
    client.post("/boxes", data={"name": "JSON Box"})
    monkeypatch.setattr(
        client.app_module.vision, "generate_label_art",
        lambda *args, **kwargs: _fake_jpg_bytes(),
    )
    r = client.post(
        "/boxes/1/generate-art",
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["ok"] is True
    assert payload["box_id"] == 1
    assert payload["background_art"]


def test_clear_art_returns_json_for_ajax_clients(client, monkeypatch):
    client.post("/boxes", data={"name": "JSON Box"})
    monkeypatch.setattr(
        client.app_module.vision, "generate_label_art",
        lambda *args, **kwargs: _fake_jpg_bytes(),
    )
    client.post("/boxes/1/generate-art")
    r = client.post(
        "/boxes/1/clear-art",
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    assert r.json()["background_art"] is None


def test_generate_art_threads_items_and_photos_into_prompt(client, monkeypatch):
    """The endpoint must pull items + their photo bytes and pass them to the
    generator so Nano Banana 2 grounds the output in real contents."""
    import io
    from PIL import Image
    from unittest.mock import patch
    from vision import DetectedItem

    client.post("/boxes", data={"name": "Closet"})
    photo = io.BytesIO()
    Image.new("RGB", (200, 200), (10, 100, 200)).save(photo, format="JPEG")
    photo_bytes = photo.getvalue()
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="red mug", description="ceramic", bbox=[0, 0, 500, 500]),
        DetectedItem(name="kettle", description="copper teakettle", bbox=[100, 100, 600, 600]),
    ]):
        client.post(
            "/ingest",
            files={"photos": ("p.jpg", io.BytesIO(photo_bytes), "image/jpeg")},
        )
    client.post("/queue/1/assign", data={"box_id": "1", "name": "red mug", "skip_crop": "1"})
    client.post("/queue/2/assign", data={"box_id": "1", "name": "kettle", "skip_crop": "1"})

    captured = {}
    def fake_gen(name, description="", items=None, item_photos=None):
        captured["name"] = name
        captured["items"] = items or []
        captured["item_photos"] = item_photos or []
        return _fake_jpg_bytes()
    monkeypatch.setattr(client.app_module.vision, "generate_label_art", fake_gen)

    r = client.post("/boxes/1/generate-art", follow_redirects=False)
    assert r.status_code == 303
    item_names = [it["name"] for it in captured["items"]]
    assert "red mug" in item_names
    assert "kettle" in item_names
    assert len(captured["item_photos"]) >= 1
    photo_bytes_passed, mime = captured["item_photos"][0]
    assert isinstance(photo_bytes_passed, bytes) and len(photo_bytes_passed) > 0
    assert mime.startswith("image/")


def test_parallel_art_generations_each_succeed(client, monkeypatch):
    client.post("/boxes", data={"name": "A"})
    client.post("/boxes", data={"name": "B"})
    monkeypatch.setattr(
        client.app_module.vision, "generate_label_art",
        lambda *args, **kwargs: _fake_jpg_bytes(),
    )
    r1 = client.post("/boxes/1/generate-art", headers={"Accept": "application/json"})
    r2 = client.post("/boxes/2/generate-art", headers={"Accept": "application/json"})
    assert r1.status_code == 200 and r2.status_code == 200
    with client.app_module.db() as conn:
        rows = conn.execute("SELECT id, background_art FROM boxes ORDER BY id").fetchall()
    assert all(row["background_art"] for row in rows), \
        f"expected both boxes to have art set, got {[(r['id'], r['background_art']) for r in rows]}"


def test_art_files_are_protected_from_orphan_cleanup(client, monkeypatch):
    """The maintenance cleanup sweep must treat background_art as referenced."""
    client.post("/boxes", data={"name": "Wedding Dress"})
    monkeypatch.setattr(
        client.app_module.vision, "generate_label_art",
        lambda *args, **kwargs: _fake_jpg_bytes(),
    )
    client.post("/boxes/1/generate-art")
    with client.app_module.db() as conn:
        art = conn.execute("SELECT background_art FROM boxes WHERE id = 1").fetchone()[0]

    client.post("/maintenance/cleanup")
    assert (client.app_module.UPLOAD_DIR / str(client.test_tenant_id)
            / art).exists(), "cleanup deleted referenced background art"


def _fake_jpg_bytes() -> bytes:
    """A real, decodable JPEG that vision.generate_label_art can stand in for."""
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (256, 144), color=(220, 200, 180)).save(buf, format="JPEG")
    return buf.getvalue()
