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
    page = client.get("/home").text
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


# ── Extended telemetry (page HTML, console log, perf timing) ──────────


def test_submit_feedback_persists_extended_telemetry(client):
    """Capture-this-page populates the new columns + the page HTML
    blob, all in the same submit."""
    r = client.post(
        "/feedback",
        data={
            "body": "the modal won't close",
            "page_html": "<html><body>hello</body></html>",
            "console_log": '[{"level":"error","msg":"boom"}]',
            "focused_selector": "#feedback-launcher",
            "scroll_x": "0",
            "scroll_y": "420",
            "page_title": "Locations · Stash",
            "color_scheme": "dark",
            "client_timestamp": "2026-05-16T05:54:01.123Z",
            "perf_timing": '{"ttfb_ms":42,"lcp_ms":900}',
        },
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200, r.text
    fb_id = r.json()["feedback_id"]
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT page_html, console_log, focused_selector, "
            "       scroll_x, scroll_y, page_title, color_scheme, "
            "       client_timestamp, perf_timing, tenant_id "
            "FROM feedback WHERE id = ?",
            (fb_id,),
        ).fetchone()
    # Page HTML lives encrypted on disk; the column holds the filename.
    assert row["page_html"], "page_html filename not stored"
    assert row["page_html"].endswith(".html.enc")
    blob = (client.app_module.UPLOAD_DIR
            / str(row["tenant_id"]) / row["page_html"])
    assert blob.exists()
    # Small fields land in columns directly.
    assert row["console_log"] == '[{"level":"error","msg":"boom"}]'
    assert row["focused_selector"] == "#feedback-launcher"
    assert row["scroll_x"] == 0 and row["scroll_y"] == 420
    assert row["page_title"] == "Locations · Stash"
    assert row["color_scheme"] == "dark"
    assert row["client_timestamp"] == "2026-05-16T05:54:01.123Z"
    assert row["perf_timing"] == '{"ttfb_ms":42,"lcp_ms":900}'


def test_submit_feedback_page_html_encrypted_on_disk(client):
    """Page HTML rides the tenant-encryption pipeline like
    screenshots — plaintext must never sit on disk."""
    plaintext = "<html><body>secret token tk_abc123</body></html>"
    r = client.post(
        "/feedback",
        data={"body": "x", "page_html": plaintext},
        headers={"Accept": "application/json"},
    )
    fb_id = r.json()["feedback_id"]
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT page_html, tenant_id FROM feedback WHERE id = ?",
            (fb_id,),
        ).fetchone()
    blob = (client.app_module.UPLOAD_DIR
            / str(row["tenant_id"]) / row["page_html"])
    # Disk bytes are NOT the cleartext.  (Cleartext substring check is
    # the cheapest way to confirm encryption without re-decrypting.)
    assert b"secret token tk_abc123" not in blob.read_bytes()


def test_submit_feedback_drops_oversize_page_html(client):
    """A 1 MB capture must be silently dropped — same contract as
    the oversize screenshot path: the feedback is more valuable
    than the attached payload."""
    huge = "x" * 1_000_000
    r = client.post(
        "/feedback",
        data={"body": "huge dom", "page_html": huge},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    fb_id = r.json()["feedback_id"]
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT page_html FROM feedback WHERE id = ?", (fb_id,),
        ).fetchone()
    assert row["page_html"] is None


def test_admin_feedback_page_html_round_trip(client, monkeypatch):
    """Operator can fetch the captured HTML as inert text/html with
    a CSP sandbox header so it can't actually execute."""
    plaintext = "<html><body><h1>captured</h1></body></html>"
    client.post(
        "/feedback",
        data={"body": "x", "page_html": plaintext},
        headers={"Accept": "application/json"},
    )
    monkeypatch.setattr(
        client.app_module, "_OPERATOR_EMAILS",
        frozenset({"test@example.com"}),
    )
    r = client.get("/admin/feedback/1/page_html")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # The sandbox header is what makes this safe to view — without
    # it the captured page could run scripts in the operator's
    # session origin.
    assert r.headers["content-security-policy"] == "sandbox"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in r.headers["content-disposition"]
    assert r.text == plaintext


def test_admin_feedback_page_html_404_for_non_operator(client):
    """Same opacity rule as the rest of /admin — non-operator → 404."""
    client.post(
        "/feedback",
        data={"body": "x", "page_html": "<html></html>"},
        headers={"Accept": "application/json"},
    )
    r = client.get("/admin/feedback/1/page_html")
    assert r.status_code == 404


# ── source column ────────────────────────────────────────────────────


def test_submit_feedback_defaults_source_to_user_widget(client):
    """Every row from the in-app POST /feedback path lands as
    ``source='user_widget'`` so the operator can later separate
    real-user submissions from agent-created MCP rows."""
    client.post(
        "/feedback", data={"body": "from the widget"},
        headers={"Accept": "application/json"},
    )
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT source FROM feedback WHERE body = 'from the widget'"
        ).fetchone()
    assert row["source"] == "user_widget"


def test_dao_create_with_explicit_source(client):
    """DAO ``create`` accepts ``source``; unknown values raise."""
    fb_id = client.app_module.dao_feedback.create(
        tenant_id=None, actor_email="op@example.com",
        body="sweep finding", source="mcp",
    )
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT source FROM feedback WHERE id = ?", (fb_id,),
        ).fetchone()
    assert row["source"] == "mcp"

    import pytest
    with pytest.raises(ValueError):
        client.app_module.dao_feedback.create(
            tenant_id=None, actor_email="op@example.com",
            body="invalid", source="bogus",
        )


# ── urgent flag (feedback #45) ──────────────────────────────────────


def test_set_urgent_persists_and_audit_logs(client):
    """DAO ``set_urgent`` flips the column + writes an audit_log row
    so an operator history shows who escalated which feedback when."""
    fb_id = client.app_module.dao_feedback.create(
        tenant_id=client.test_tenant_id, actor_email="x@example.com",
        body="urgent test",
    )
    client.app_module.dao_feedback.set_urgent(
        fb_id, True, operator_email="op@example.com",
    )
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT urgent FROM feedback WHERE id = ?", (fb_id,),
        ).fetchone()
        audit = conn.execute(
            "SELECT actor_email, action, target_id FROM audit_log "
            "WHERE target_kind = 'feedback' AND target_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (fb_id,),
        ).fetchone()
    assert row["urgent"] == 1
    assert audit["actor_email"] == "op@example.com"
    assert audit["action"] == "feedback.urgent"

    # Clearing the flag writes a distinct ``feedback.urgent.clear``
    # audit verb so the trail shows both directions.
    client.app_module.dao_feedback.set_urgent(
        fb_id, False, operator_email="op@example.com",
    )
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT urgent FROM feedback WHERE id = ?", (fb_id,),
        ).fetchone()
        audit = conn.execute(
            "SELECT action FROM audit_log "
            "WHERE target_kind = 'feedback' AND target_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (fb_id,),
        ).fetchone()
    assert row["urgent"] == 0
    assert audit["action"] == "feedback.urgent.clear"


def test_list_for_operator_sorts_urgent_first(client):
    """Operator queue reads return urgent rows ahead of non-urgent
    in each status bucket — the whole point of the flag."""
    a = client.app_module.dao_feedback.create(
        tenant_id=client.test_tenant_id, actor_email="x@example.com",
        body="older standard",
    )
    b = client.app_module.dao_feedback.create(
        tenant_id=client.test_tenant_id, actor_email="x@example.com",
        body="urgent middle",
    )
    c = client.app_module.dao_feedback.create(
        tenant_id=client.test_tenant_id, actor_email="x@example.com",
        body="newer standard",
    )
    client.app_module.dao_feedback.set_urgent(
        b, True, operator_email="op@example.com",
    )
    rows = client.app_module.dao_feedback.list_for_operator(status="open")
    bodies = [r["body"] for r in rows]
    # Urgent row first regardless of created_at order; non-urgent
    # rows fall in newest-first order beneath.
    assert bodies.index("urgent middle") < bodies.index("newer standard")
    assert bodies.index("urgent middle") < bodies.index("older standard")
    assert bodies.index("newer standard") < bodies.index("older standard")


def test_admin_urgent_route_toggles_flag(client, monkeypatch):
    """POST /admin/feedback/{id}/urgent flips the row's flag.
    Operator-only — non-operator gets a 404 (opacity rule)."""
    fb_id = client.app_module.dao_feedback.create(
        tenant_id=client.test_tenant_id, actor_email="x@example.com",
        body="route test",
    )
    monkeypatch.setattr(
        client.app_module, "_OPERATOR_EMAILS",
        frozenset({"test@example.com"}),
    )
    r = client.post(
        f"/admin/feedback/{fb_id}/urgent", data={"urgent": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT urgent FROM feedback WHERE id = ?", (fb_id,),
        ).fetchone()
    assert row["urgent"] == 1


def test_admin_urgent_route_404_for_non_operator(client):
    """Non-operators hit a 404 (not 403) per /admin opacity rule."""
    fb_id = client.app_module.dao_feedback.create(
        tenant_id=client.test_tenant_id, actor_email="x@example.com",
        body="x",
    )
    r = client.post(
        f"/admin/feedback/{fb_id}/urgent", data={"urgent": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 404


def test_admin_queue_renders_urgent_pill(client, monkeypatch):
    """An urgent-flagged row renders the 🔥 urgent pill in /admin."""
    fb_id = client.app_module.dao_feedback.create(
        tenant_id=client.test_tenant_id, actor_email="x@example.com",
        body="render test",
    )
    client.app_module.dao_feedback.set_urgent(
        fb_id, True, operator_email="op@example.com",
    )
    monkeypatch.setattr(
        client.app_module, "_OPERATOR_EMAILS",
        frozenset({"test@example.com"}),
    )
    page = client.get("/admin").text
    assert "pill-urgent" in page
    assert "kanban-card-urgent" in page


# ── Verification loop ────────────────────────────────────────────────


def _mk_open_feedback(client, body: str = "thing's broken") -> int:
    """Shorthand: create an open feedback row, return its id."""
    return client.app_module.dao_feedback.create(
        tenant_id=client.test_tenant_id,
        actor_email="user@example.com",
        body=body,
    )


def test_mark_needs_verification_stamps_commit_metadata(client):
    """``mark_needs_verification`` transitions the row + records
    the fix commit SHA + summary that the /admin kanban renders."""
    fb_id = _mk_open_feedback(client, "tag-suggest is broken")
    updated = client.app_module.dao_feedback.mark_needs_verification(
        fb_id,
        fix_commit_sha="abc1234567890def",
        fix_summary="Wire JSON content-negotiation on /items/{id}/tags",
        operator_email="ai-bot",
    )
    assert updated["status"] == "needs_verification"
    assert updated["fix_commit_sha"] == "abc1234567890def"
    assert "JSON content-negotiation" in updated["fix_summary"]
    assert updated["resolved_by"] == "ai-bot"
    assert updated["verified_in_staging_at"] is None


def test_mark_needs_verification_rejects_empty_commit_sha(client):
    """The whole point of needs_verification is "here's the commit
    to look at."  Refuse the transition without one."""
    import pytest
    fb_id = _mk_open_feedback(client)
    with pytest.raises(ValueError):
        client.app_module.dao_feedback.mark_needs_verification(
            fb_id, fix_commit_sha="", fix_summary="oops",
            operator_email="ai-bot",
        )


def test_mark_verified_in_staging_only_works_from_needs_verification(client):
    """An ``open`` row can't jump straight to verified — the AI
    has to commit a fix first.  Refuse the transition."""
    import pytest
    fb_id = _mk_open_feedback(client)
    with pytest.raises(ValueError):
        client.app_module.dao_feedback.mark_verified_in_staging(
            fb_id, operator_email="op@example.com",
        )


def test_full_verification_loop_open_to_done(client):
    """The happy path: open → needs_verification → verified on
    staging → done on release.  Pins every state stamp + the
    auto-done-on-release gate (needs both verification flag AND
    matching commit SHA in the release range)."""
    dao = client.app_module.dao_feedback
    fb_id = _mk_open_feedback(client, "queue customize hidden on mobile")
    # 1. AI commits a fix.
    dao.mark_needs_verification(
        fb_id, fix_commit_sha="deadbeef1234",
        fix_summary="move crop controls under photo",
        operator_email="ai-bot",
    )
    row = dao.get(fb_id)
    assert row["status"] == "needs_verification"
    assert row["fix_commit_sha"] == "deadbeef1234"
    # 2. Operator verifies on staging.
    dao.mark_verified_in_staging(fb_id, operator_email="op@example.com")
    row = dao.get(fb_id)
    assert row["status"] == "needs_verification"  # NOT done yet
    assert row["verified_in_staging_at"] is not None
    assert row["verified_in_staging_by"] == "op@example.com"
    # 3. Release ships including this commit.
    awaiting = dao.list_awaiting_release(["deadbeef1234", "unrelated"])
    assert len(awaiting) == 1
    assert awaiting[0]["id"] == fb_id
    dao.mark_done_on_release(fb_id, release_tag="v1.47.0")
    row = dao.get(fb_id)
    assert row["status"] == "done"
    assert row["released_in"] == "v1.47.0"


def test_done_on_release_refuses_unverified_rows(client):
    """The visual-verification gate is non-negotiable.  Even if a
    commit lands in a tagged release, an unverified row stays in
    needs_verification — the operator's ✓ is the prerequisite."""
    import pytest
    dao = client.app_module.dao_feedback
    fb_id = _mk_open_feedback(client)
    dao.mark_needs_verification(
        fb_id, fix_commit_sha="cafef00d", fix_summary="x",
        operator_email="ai-bot",
    )
    with pytest.raises(ValueError, match="not yet verified"):
        dao.mark_done_on_release(fb_id, release_tag="v1.48.0")


def test_list_awaiting_release_filters_by_commit_and_verification(client):
    """``list_awaiting_release`` only returns rows that are
    BOTH needs_verification AND verified_in_staging AND match a
    commit in the input list.  All three filters must hold."""
    dao = client.app_module.dao_feedback
    # Row A: verified, matching commit → should appear.
    a = _mk_open_feedback(client, "A")
    dao.mark_needs_verification(a, fix_commit_sha="aaa111",
                                 fix_summary="a", operator_email="ai")
    dao.mark_verified_in_staging(a, operator_email="op")
    # Row B: matching commit but NOT verified → should NOT appear.
    b = _mk_open_feedback(client, "B")
    dao.mark_needs_verification(b, fix_commit_sha="bbb222",
                                 fix_summary="b", operator_email="ai")
    # Row C: verified but commit NOT in input list → should NOT appear.
    c = _mk_open_feedback(client, "C")
    dao.mark_needs_verification(c, fix_commit_sha="ccc333",
                                 fix_summary="c", operator_email="ai")
    dao.mark_verified_in_staging(c, operator_email="op")
    # Row D: open, never needs_verification → should NOT appear.
    _mk_open_feedback(client, "D")

    eligible = dao.list_awaiting_release(["aaa111", "bbb222"])
    eligible_ids = [r["id"] for r in eligible]
    assert eligible_ids == [a]


def test_reopen_clears_fix_metadata_and_appends_note(client):
    """Operator's ✗ Still broken bounces the row back to open AND
    clears the fix_commit_sha so the AI can't just re-point at
    the same broken commit.  Optional note is prepended to
    operator_notes with a ``[reopen by …]`` prefix."""
    dao = client.app_module.dao_feedback
    fb_id = _mk_open_feedback(client, "leak in queue card")
    dao.mark_needs_verification(
        fb_id, fix_commit_sha="badf1x55",
        fix_summary="patched the leak",
        operator_email="ai-bot",
    )
    dao.reopen_after_failed_verification(
        fb_id,
        operator_email="op@example.com",
        note="still leaks on mobile Safari",
    )
    row = dao.get(fb_id)
    assert row["status"] == "open"
    assert row["fix_commit_sha"] is None
    assert row["fix_summary"] is None
    assert "[reopen by op@example.com]" in (row["operator_notes"] or "")
    assert "still leaks on mobile Safari" in (row["operator_notes"] or "")


def test_admin_verify_route_stamps_staging_verification(client, monkeypatch):
    """POST /admin/feedback/{id}/verify is the operator's ✓ button
    on the kanban — stamps verified_in_staging_at/by."""
    monkeypatch.setattr(
        client.app_module, "_OPERATOR_EMAILS",
        frozenset({"test@example.com"}),
    )
    dao = client.app_module.dao_feedback
    fb_id = _mk_open_feedback(client)
    dao.mark_needs_verification(
        fb_id, fix_commit_sha="cafe1234",
        fix_summary="x", operator_email="ai-bot",
    )
    r = client.post(
        f"/admin/feedback/{fb_id}/verify", follow_redirects=False,
    )
    assert r.status_code == 303
    row = dao.get(fb_id)
    assert row["verified_in_staging_at"] is not None
    assert row["verified_in_staging_by"] == "test@example.com"


def test_admin_verify_route_400s_on_wrong_status(client, monkeypatch):
    """Can't verify a row that's not in needs_verification."""
    monkeypatch.setattr(
        client.app_module, "_OPERATOR_EMAILS",
        frozenset({"test@example.com"}),
    )
    fb_id = _mk_open_feedback(client)
    r = client.post(
        f"/admin/feedback/{fb_id}/verify", follow_redirects=False,
    )
    assert r.status_code == 400


def test_admin_reopen_route_bounces_back_to_open(client, monkeypatch):
    """POST /admin/feedback/{id}/reopen is the ✗ Still broken
    button — flips back to open + clears fix metadata."""
    monkeypatch.setattr(
        client.app_module, "_OPERATOR_EMAILS",
        frozenset({"test@example.com"}),
    )
    dao = client.app_module.dao_feedback
    fb_id = _mk_open_feedback(client)
    dao.mark_needs_verification(
        fb_id, fix_commit_sha="cafe1234",
        fix_summary="x", operator_email="ai-bot",
    )
    r = client.post(
        f"/admin/feedback/{fb_id}/reopen",
        data={"note": "didn't work on mobile"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    row = dao.get(fb_id)
    assert row["status"] == "open"
    assert row["fix_commit_sha"] is None
    assert "didn't work on mobile" in (row["operator_notes"] or "")


def test_rewrite_host_to_staging_swaps_host(client, monkeypatch):
    """The kanban's "🧪 staging" link rewrites a prod-side
    source_url to its staging equivalent.  Preserves path, query,
    fragment; only swaps the host."""
    monkeypatch.setattr(
        client.app_module, "STAGING_URL",
        "https://stash-staging.example.com",
    )
    monkeypatch.setattr(
        client.app_module, "PUBLIC_URL",
        "https://stash.example.com",
    )
    rewrite = client.app_module._rewrite_host_to_staging
    assert rewrite("https://stash.example.com/queue?box=3#card-1") == (
        "https://stash-staging.example.com/queue?box=3#card-1"
    )


def test_rewrite_host_to_staging_returns_none_when_unset(client, monkeypatch):
    """No STAGING_URL → no link rendered.  Don't assume; check."""
    monkeypatch.setattr(client.app_module, "STAGING_URL", "")
    assert client.app_module._rewrite_host_to_staging(
        "https://stash.example.com/queue"
    ) is None


def test_rewrite_host_to_staging_refuses_cross_origin_source(client, monkeypatch):
    """Defence-in-depth: a feedback row with source_url pointing
    off-domain (e.g. someone copy-pasted a malicious link as a
    repro URL) should NOT get rewritten — the rewrite is only
    safe when the source host matches PUBLIC_URL's host."""
    monkeypatch.setattr(
        client.app_module, "STAGING_URL",
        "https://stash-staging.example.com",
    )
    monkeypatch.setattr(
        client.app_module, "PUBLIC_URL",
        "https://stash.example.com",
    )
    assert client.app_module._rewrite_host_to_staging(
        "https://attacker.example.com/evil"
    ) is None


def test_admin_kanban_renders_needs_verification_column(client, monkeypatch):
    """The /admin kanban gains a 5th column for needs_verification
    rows.  Pin the structure so a future template refactor that
    drops the column fails this test."""
    monkeypatch.setattr(
        client.app_module, "_OPERATOR_EMAILS",
        frozenset({"test@example.com"}),
    )
    dao = client.app_module.dao_feedback
    fb_id = _mk_open_feedback(client, "needs verifying")
    dao.mark_needs_verification(
        fb_id, fix_commit_sha="abc1234567",
        fix_summary="moved the controls",
        operator_email="ai-bot",
    )
    page = client.get("/admin").text
    assert "Needs verification" in page
    assert "kanban-column-needs_verification" in page
    # Card surfaces the fix commit + summary.
    assert "abc1234567" in page
    assert "moved the controls" in page
    # Specialised buttons present.
    assert "Verified on staging" in page or "✓ Verified" in page
    assert "Still broken" in page


def test_admin_queue_renders_mcp_source_pill(client, monkeypatch):
    """The /admin feedback queue shows a source pill on rows that
    came in via MCP so the operator can spot automated findings
    at a glance.  ``user_widget`` rows render without a pill."""
    client.app_module.dao_feedback.create(
        tenant_id=client.test_tenant_id, actor_email="user@example.com",
        body="real user complaint", source="user_widget",
    )
    client.app_module.dao_feedback.create(
        tenant_id=None, actor_email="op@example.com",
        body="sweep-flagged layout issue", source="mcp",
    )
    monkeypatch.setattr(
        client.app_module, "_OPERATOR_EMAILS",
        frozenset({"test@example.com"}),
    )
    page = client.get("/admin").text
    assert "real user complaint" in page
    assert "sweep-flagged layout issue" in page
    # The mcp row should carry the source pill; the user row shouldn't.
    assert "pill-source-mcp" in page
    assert "pill-source-user_widget" not in page
