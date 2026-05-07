import io
from unittest.mock import patch

from vision import DetectedItem, BoxMatch


def test_ingest_uploads_then_processes_in_background(client):
    detected = [
        DetectedItem(name="wooden spatula", description="long-handled wooden cooking spatula"),
        DetectedItem(name="ceramic mug", description="white ceramic coffee mug"),
    ]
    # Background tasks run after the response in TestClient — patch covers both
    with patch("app.vision.detect_items", return_value=detected):
        r = client.post(
            "/ingest",
            files={"photos": ("pile.jpg", io.BytesIO(b"\xff\xd8fakejpeg"), "image/jpeg")},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/ingest"

    # Job is done after background task ran; pending items landed in sort queue
    queue = client.get("/queue").text
    assert "wooden spatula" in queue
    assert "ceramic mug" in queue
    assert "sort-card" in queue  # cards rendered for each item


def test_ingest_failure_is_recorded_not_raised(client):
    with patch("app.vision.detect_items", side_effect=RuntimeError("API exploded")):
        r = client.post(
            "/ingest",
            files={"photos": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")},
            follow_redirects=False,
        )
    assert r.status_code == 303

    page = client.get("/ingest").text
    assert "failed" in page
    assert "API exploded" in page
    # No items leaked into the sort queue
    assert "Queue is empty" in client.get("/queue").text


def test_ingest_done_jobs_disappear_from_ingest_view(client):
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="thing", description="d")
    ]):
        client.post("/ingest", files={"photos": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")})

    page = client.get("/ingest").text
    # Done jobs are hidden from the ingest page (only pending/processing/failed shown)
    assert "badge-done" not in page
    assert "Processing" not in page  # no active jobs section


def test_ingest_retry_failed_job(client):
    """Failed jobs can be re-processed without re-uploading the photo."""
    with patch("app.vision.detect_items", side_effect=RuntimeError("transient")):
        client.post("/ingest", files={"photos": ("p.jpg", io.BytesIO(b"\xff\xd8bytes"), "image/jpeg")})
    assert "failed" in client.get("/ingest").text

    # Retry with vision now succeeding — pending item should land in queue
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="recovered", description="d")
    ]):
        r = client.post("/ingest/1/retry", follow_redirects=False)
    assert r.status_code == 303
    assert "recovered" in client.get("/queue").text
    # Job is no longer failed
    assert "failed" not in client.get("/ingest").text


def test_ingest_retry_rejects_non_failed_job(client):
    """Can't retry a job that isn't in failed state."""
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="thing", description="d")
    ]):
        client.post("/ingest", files={"photos": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")})
    r = client.post("/ingest/1/retry", follow_redirects=False)
    assert r.status_code == 404


def test_ingest_dismiss_failed_job(client):
    with patch("app.vision.detect_items", side_effect=RuntimeError("nope")):
        client.post("/ingest", files={"photos": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")})

    r = client.post("/ingest/1/dismiss", follow_redirects=False)
    assert r.status_code == 303
    assert "Processing" not in client.get("/ingest").text  # no active jobs visible


def test_ingest_requires_photo(client):
    r = client.post("/ingest", files={})
    assert r.status_code in (400, 422)


def test_match_existing_box(client):
    client.post("/boxes", data={"name": "Kitchen utensils"})
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="spatula", description="wooden")
    ]):
        client.post("/ingest", files={"photos": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")})

    suggestion = BoxMatch(
        match="existing", box_id=1, reason="Spatulas are kitchen utensils."
    )
    with patch("vision.suggest_box", return_value=suggestion):
        r = client.post("/queue/1/match", follow_redirects=False)
    assert r.status_code == 303

    page = client.get("/queue").text
    assert "Kitchen utensils" in page
    assert "Spatulas are kitchen utensils." in page


def test_match_proposes_new_box(client):
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="ski boot", description="left ski boot, size 10")
    ]):
        client.post("/ingest", files={"photos": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")})

    suggestion = BoxMatch(
        match="new",
        new_box_name="Ski gear",
        new_box_location="Garage shelf C",
        reason="No existing box fits winter sports equipment.",
    )
    with patch("vision.suggest_box", return_value=suggestion):
        client.post("/queue/1/match")

    page = client.get("/queue").text
    assert "Ski gear" in page
    assert "No existing box fits" in page


def test_assign_to_existing_box(client):
    client.post("/boxes", data={"name": "Kitchen"})
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="spatula", description="wooden")
    ]):
        client.post("/ingest", files={"photos": ("p.jpg", io.BytesIO(b"jpegbytes"), "image/jpeg")})

    r = client.post(
        "/queue/1/assign",
        data={"box_id": "1", "name": "spatula", "description": "wooden"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    # Pending drained
    assert "Queue is empty" in client.get("/queue").text
    # Item landed in box with the photo attached
    box = client.get("/boxes/1").text
    assert "spatula" in box
    assert "wooden" in box
    assert "/uploads/" in box


def test_assign_saves_edits(client):
    """User can rename/redescribe an item before accepting it."""
    client.post("/boxes", data={"name": "Kitchen"})
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="ugly autoname", description="vague")
    ]):
        client.post("/ingest", files={"photos": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")})

    client.post("/queue/1/assign", data={
        "box_id": "1", "name": "Le Creuset spatula", "description": "silicone, red handle",
    })

    box = client.get("/boxes/1").text
    assert "Le Creuset spatula" in box
    assert "silicone, red handle" in box
    assert "ugly autoname" not in box


def test_assign_requires_box(client):
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="t", description="d")
    ]):
        client.post("/ingest", files={"photos": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")})

    r = client.post("/queue/1/assign", data={"name": "t"})
    assert r.status_code in (400, 422)


def test_queue_state_fingerprint_changes_on_edit(client):
    client.post("/boxes", data={"name": "Kitchen"})
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="thing", description="d")
    ]):
        client.post("/ingest", files={"photos": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")})

    fp1 = client.get("/queue/state").json()["fingerprint"]

    # Same state → same fingerprint
    assert client.get("/queue/state").json()["fingerprint"] == fp1

    # Mutating state → fingerprint changes (used by the polling JS to trigger reload)
    client.post("/queue/1/assign", data={"box_id": "1", "name": "thing"})
    fp2 = client.get("/queue/state").json()["fingerprint"]
    assert fp2 != fp1


def test_queue_items_fragment_returns_just_the_cards(client):
    """The /queue/items endpoint backs the page's real-time refresh — it
    returns a fragment of pending-item cards (no <html>/<body>) so the
    polling JS can splice individual items in/out of the page without a
    full reload."""
    client.post("/boxes", data={"name": "Box"})
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="alpha", description="d", bbox=[0, 0, 500, 500]),
        DetectedItem(name="beta", description="d"),
    ]):
        client.post("/ingest", files={"photos": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")})

    r = client.get("/queue/items")
    assert r.status_code == 200
    text = r.text

    # Each pending row produces a <form id="card-N">.  Both rows show up.
    assert 'id="card-1"' in text
    assert 'id="card-2"' in text
    assert "alpha" in text
    assert "beta" in text
    # Page chrome (header, base.html scaffolding, the Queue-is-empty
    # placeholder) should NOT be in the fragment.
    assert "<html" not in text.lower()
    assert "Queue is empty" not in text


def test_queue_items_fragment_drops_assigned(client):
    """After /queue/{id}/assign removes a pending row, the fragment no
    longer carries that card — the client-side diff uses this to prune
    vanished items without a page reload."""
    client.post("/boxes", data={"name": "Box"})
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="thing", description="d")
    ]):
        client.post("/ingest", files={"photos": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")})

    assert 'id="card-1"' in client.get("/queue/items").text
    client.post("/queue/1/assign", data={"box_id": "1", "name": "thing"})
    assert 'id="card-1"' not in client.get("/queue/items").text


def test_drop_pending_item(client):
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="thing", description="d")
    ]):
        client.post("/ingest", files={"photos": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")})

    r = client.post("/queue/1/delete", follow_redirects=False)
    assert r.status_code == 303
    assert "Queue is empty" in client.get("/queue").text
