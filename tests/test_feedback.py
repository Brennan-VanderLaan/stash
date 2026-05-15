"""In-app feedback widget — submit + operator triage queue."""

from __future__ import annotations

import base64
import json


def _b64_jpeg_data_url(payload: bytes = b"fake-image-bytes") -> str:
    """Tiny ``data:image/jpeg;base64,…`` payload mimicking what the
    widget POSTs after html2canvas → toDataURL."""
    return "data:image/jpeg;base64," + base64.b64encode(payload).decode()


def test_submit_feedback_creates_row(client):
    r = client.post(
        "/feedback",
        data={
            "body": "the print preview loses my selection",
            "source_url": "https://example.com/labels",
            "user_agent": "TestAgent/1.0",
            "viewport_w": "1920",
            "viewport_h": "1080",
        },
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["ok"] is True
    fb_id = payload["feedback_id"]
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT body, tenant_id, actor_email, screenshot, "
            "       source_url, viewport_w, viewport_h, status "
            "FROM feedback WHERE id = ?",
            (fb_id,),
        ).fetchone()
    assert row["body"] == "the print preview loses my selection"
    assert row["tenant_id"] == client.test_tenant_id
    assert row["status"] == "open"
    assert row["screenshot"] is None
    assert row["source_url"] == "https://example.com/labels"
    assert row["viewport_w"] == 1920


def test_submit_feedback_with_screenshot_writes_encrypted(client):
    """When the widget attaches a screenshot the bytes get tenant-
    encrypted on disk so a cross-tenant leak is impossible."""
    data_url = _b64_jpeg_data_url(b"\xff\xd8\xff\xe0fake")
    r = client.post(
        "/feedback",
        data={"body": "screenshot demo", "screenshot_data": data_url},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200, r.text
    fb_id = r.json()["feedback_id"]
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT screenshot, tenant_id FROM feedback WHERE id = ?",
            (fb_id,),
        ).fetchone()
    assert row["screenshot"], "screenshot filename not stored"
    name = row["screenshot"]
    tenant_dir = (client.app_module.UPLOAD_DIR
                  / str(row["tenant_id"]) / name)
    assert tenant_dir.exists(), "screenshot blob not written"
    # Stored bytes must not equal the plaintext (i.e., encrypted).
    assert tenant_dir.read_bytes() != b"\xff\xd8\xff\xe0fake"


def test_submit_feedback_rejects_empty_body(client):
    r = client.post("/feedback", data={"body": "  "})
    assert r.status_code == 400


def test_submit_feedback_caps_body_length(client):
    huge = "x" * 10_000
    r = client.post(
        "/feedback", data={"body": huge},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    fb_id = r.json()["feedback_id"]
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT length(body) AS n FROM feedback WHERE id = ?",
            (fb_id,),
        ).fetchone()
    # Body got truncated to the configured max so a runaway paste
    # can't pollute the queue.
    assert row["n"] == 4000


def test_submit_feedback_ignores_oversize_screenshot(client):
    """A 2 MB screenshot data URL must drop the screenshot silently
    rather than fail the whole submit — feedback is more valuable
    than the attached image."""
    huge = _b64_jpeg_data_url(b"x" * 2_000_000)
    r = client.post(
        "/feedback",
        data={"body": "huge shot", "screenshot_data": huge},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    fb_id = r.json()["feedback_id"]
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT screenshot FROM feedback WHERE id = ?", (fb_id,),
        ).fetchone()
    assert row["screenshot"] is None


def test_feedback_widget_renders_for_tenant_member(client):
    """The floating button should be present in the rendered HTML
    when the actor has an active tenant."""
    page = client.get("/").text
    assert 'id="feedback-launcher"' in page
    assert 'id="feedback-dialog"' in page


def test_admin_feedback_queue_lists_rows(client, monkeypatch):
    """Operator's /admin shows submitted feedback with status pills
    and action buttons."""
    client.post(
        "/feedback", data={"body": "first issue"},
        headers={"Accept": "application/json"},
    )
    client.post(
        "/feedback", data={"body": "second issue"},
        headers={"Accept": "application/json"},
    )
    # _OPERATOR_EMAILS is materialised at module import time, so
    # patch the resolved set rather than the env var.
    monkeypatch.setattr(
        client.app_module, "_OPERATOR_EMAILS",
        frozenset({"test@example.com"}),
    )
    page = client.get("/admin").text
    assert "first issue" in page
    assert "second issue" in page
    assert "Feedback queue" in page


def test_admin_set_feedback_status(client, monkeypatch):
    """Operator can flip a row to accepted; resolved_by stamps."""
    client.post(
        "/feedback", data={"body": "fix this"},
        headers={"Accept": "application/json"},
    )
    # _OPERATOR_EMAILS is materialised at module import time, so
    # patch the resolved set rather than the env var.
    monkeypatch.setattr(
        client.app_module, "_OPERATOR_EMAILS",
        frozenset({"test@example.com"}),
    )
    r = client.post(
        "/admin/feedback/1/status", data={"status": "accepted"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT status, resolved_by FROM feedback WHERE id = 1"
        ).fetchone()
    assert row["status"] == "accepted"
    assert row["resolved_by"] == "test@example.com"


def test_admin_feedback_status_rejects_unknown(client, monkeypatch):
    client.post(
        "/feedback", data={"body": "x"},
        headers={"Accept": "application/json"},
    )
    # _OPERATOR_EMAILS is materialised at module import time, so
    # patch the resolved set rather than the env var.
    monkeypatch.setattr(
        client.app_module, "_OPERATOR_EMAILS",
        frozenset({"test@example.com"}),
    )
    r = client.post(
        "/admin/feedback/1/status", data={"status": "weird"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_admin_feedback_screenshot_decrypts(client, monkeypatch):
    """Operator-only screenshot fetch round-trips through the tenant
    DEK so the image arrives as plaintext bytes."""
    plaintext = b"\xff\xd8\xff\xe0jpeg-bytes-here"
    data_url = _b64_jpeg_data_url(plaintext)
    client.post(
        "/feedback",
        data={"body": "with shot", "screenshot_data": data_url},
        headers={"Accept": "application/json"},
    )
    # _OPERATOR_EMAILS is materialised at module import time, so
    # patch the resolved set rather than the env var.
    monkeypatch.setattr(
        client.app_module, "_OPERATOR_EMAILS",
        frozenset({"test@example.com"}),
    )
    r = client.get("/admin/feedback/1/screenshot")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == plaintext


def test_admin_feedback_export_json(client, monkeypatch):
    """JSON export carries every feedback row + an exported_at
    timestamp + a count.  Used by the operator's offline triage
    flow (paste into a chat with an AI assistant)."""
    client.post(
        "/feedback", data={"body": "needs fixing"},
        headers={"Accept": "application/json"},
    )
    client.post(
        "/feedback", data={"body": "another one"},
        headers={"Accept": "application/json"},
    )
    monkeypatch.setattr(
        client.app_module, "_OPERATOR_EMAILS",
        frozenset({"test@example.com"}),
    )
    r = client.get("/admin/feedback/export?format=json")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    assert "attachment" in r.headers["content-disposition"]
    payload = json.loads(r.content)
    assert payload["count"] == 2
    assert payload["exported_by"] == "test@example.com"
    bodies = sorted(fb["body"] for fb in payload["feedback"])
    assert bodies == ["another one", "needs fixing"]


def test_admin_feedback_export_csv(client, monkeypatch):
    client.post(
        "/feedback", data={"body": "spreadsheet please"},
        headers={"Accept": "application/json"},
    )
    monkeypatch.setattr(
        client.app_module, "_OPERATOR_EMAILS",
        frozenset({"test@example.com"}),
    )
    r = client.get("/admin/feedback/export?format=csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    body = r.content.decode()
    assert body.splitlines()[0].startswith("id,status,tenant_id")
    assert "spreadsheet please" in body


def test_admin_feedback_export_status_filter(client, monkeypatch):
    client.post(
        "/feedback", data={"body": "first"},
        headers={"Accept": "application/json"},
    )
    client.post(
        "/feedback", data={"body": "second"},
        headers={"Accept": "application/json"},
    )
    monkeypatch.setattr(
        client.app_module, "_OPERATOR_EMAILS",
        frozenset({"test@example.com"}),
    )
    client.post(
        "/admin/feedback/1/status", data={"status": "done"},
        follow_redirects=False,
    )
    r = client.get("/admin/feedback/export?status=open&format=json")
    payload = json.loads(r.content)
    assert payload["count"] == 1
    assert payload["feedback"][0]["body"] == "second"


def test_admin_feedback_export_404_for_non_operator(client):
    r = client.get("/admin/feedback/export")
    assert r.status_code == 404  # opaque, per /admin convention


def test_non_operator_cannot_view_feedback_screenshot(client):
    """A regular member must not be able to read other tenants'
    screenshots through /admin (also a 404 — the /admin family
    returns 404 not 403 to keep the surface opaque)."""
    plaintext = b"hello"
    data_url = _b64_jpeg_data_url(plaintext)
    client.post(
        "/feedback",
        data={"body": "x", "screenshot_data": data_url},
        headers={"Accept": "application/json"},
    )
    # No STASH_OPERATOR_EMAILS set → 404 (opaque) per existing
    # /admin convention.
    r = client.get("/admin/feedback/1/screenshot")
    assert r.status_code == 404
