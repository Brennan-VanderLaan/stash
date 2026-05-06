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
    # Notes are now the on-label description; location is intentionally dropped
    # to keep the printed label dead simple.
    assert "Mugs and small appliances" in svg
    assert "Garage shelf B" not in svg
    # Box ID badge is rendered as `#1` so you can reference boxes verbally.
    assert ">#1<" in svg
    # QR is rendered as a path, so the encoded string never appears as text.
    assert "stash:box:1" not in svg


def test_single_label_404_for_unknown_box(client):
    assert client.get("/boxes/999/label.svg").status_code == 404


def test_labels_page_lists_boxes(client):
    client.post("/boxes", data={"name": "Box A"})
    client.post("/boxes", data={"name": "Box B"})
    page = client.get("/labels").text
    assert "Box A" in page
    assert "Box B" in page
    assert "box_ids" in page  # checkboxes present


def test_sheet_svg_all_boxes(client):
    client.post("/boxes", data={"name": "Alpha", "location": "Room 1"})
    client.post("/boxes", data={"name": "Bravo", "location": "Room 2"})
    r = client.get("/labels/sheet.svg")
    assert r.status_code == 200
    assert "image/svg+xml" in r.headers["content-type"]
    svg = r.text
    assert "Alpha" in svg
    assert "Bravo" in svg
    # Avery 5163 sheet dimensions
    assert "215.9mm" in svg
    assert "279.4mm" in svg


def test_sheet_svg_selected_boxes(client):
    client.post("/boxes", data={"name": "Alpha"})
    client.post("/boxes", data={"name": "Bravo"})
    client.post("/boxes", data={"name": "Charlie"})
    r = client.get("/labels/sheet.svg?box_ids=1&box_ids=3")
    svg = r.text
    assert "Alpha" in svg
    assert "Charlie" in svg
    assert "Bravo" not in svg


def test_sheet_pads_with_blanks(client):
    client.post("/boxes", data={"name": "Only one"})
    r = client.get("/labels/sheet.svg")
    svg = r.text
    assert "Only one" in svg
    # 3 empty slots (4 per sheet - 1 box) should have dashed placeholder rects
    assert svg.count("stroke-dasharray") == 3


def test_long_name_fits_label(client):
    long_name = "Box of interesting crap for project xyz pt 2"
    client.post("/boxes", data={"name": long_name, "notes": "miscellany"})
    r = client.get("/boxes/1/label.svg")
    assert r.status_code == 200
    svg = r.text
    assert long_name in svg
    assert "miscellany" in svg
    # Confirm the ID badge format is the bare `#1` style, not `ID: 1`.
    assert "ID:" not in svg
    assert ">#1<" in svg


def test_qr_payload_uses_public_url_when_set():
    import labels
    # With a public URL, scanning the code lands on the live box page.
    assert labels._qr_data_for_box(7, "https://stash.example.com") == \
        "https://stash.example.com/boxes/7"
    # Trailing slashes on the configured URL should not double up.
    assert labels._qr_data_for_box(7, "https://stash.example.com/") == \
        "https://stash.example.com/boxes/7"
    # Without one (local dev), fall back to the custom scheme so it's obvious
    # the labels aren't print-ready.
    assert labels._qr_data_for_box(7, "") == "stash:box:7"


def test_sheet_uses_notes_not_location(client):
    client.post("/boxes", data={
        "name": "Toolbox", "location": "shed", "notes": "drill bits and tape",
    })
    svg = client.get("/labels/sheet.svg").text
    assert "drill bits and tape" in svg
    assert "shed" not in svg


def test_label_escapes_special_chars(client):
    client.post("/boxes", data={"name": "Tom & Jerry's <box>"})
    r = client.get("/boxes/1/label.svg")
    svg = r.text
    assert "&amp;" in svg
    assert "&lt;box&gt;" in svg
    assert "Tom & Jerry" not in svg  # raw & would be invalid SVG
