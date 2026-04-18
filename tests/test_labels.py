def test_single_label_svg_downloads(client):
    client.post("/boxes", data={"name": "Kitchen #1", "location": "Garage shelf B"})
    r = client.get("/boxes/1/label.svg")
    assert r.status_code == 200
    assert "image/svg+xml" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    svg = r.text
    assert "<svg" in svg
    assert "Kitchen #1" in svg
    assert "Garage shelf B" in svg
    assert "stash:box:1" not in svg  # QR data is encoded as a path, not raw text
    assert "stash:box:1" not in svg  # QR encodes it, not displayed as text


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
    client.post("/boxes", data={"name": long_name, "location": "Garage shelf B"})
    r = client.get("/boxes/1/label.svg")
    assert r.status_code == 200
    svg = r.text
    assert long_name in svg
    assert "Garage shelf B" in svg
    assert "ID:" not in svg


def test_label_escapes_special_chars(client):
    client.post("/boxes", data={"name": "Tom & Jerry's <box>"})
    r = client.get("/boxes/1/label.svg")
    svg = r.text
    assert "&amp;" in svg
    assert "&lt;box&gt;" in svg
    assert "Tom & Jerry" not in svg  # raw & would be invalid SVG
