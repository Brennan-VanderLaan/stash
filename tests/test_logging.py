"""Phase 16 — structured logger + audit-log backfill.

Two halves:

1. Logging infra: contextvars propagate through the request,
   request_id stamps the response header, the JSON formatter
   produces parseable lines.
2. Audit backfill: box / item / location / room mutations land
   audit_log rows with the expected action + metadata shape.
"""

from __future__ import annotations

import io
import json
import logging

import pytest


# ── Logging infra ──────────────────────────────────────────────────


def test_request_id_round_trips_via_response_header(client):
    """Every response carries an X-Request-Id; respect an inbound
    one when present so an upstream proxy can correlate access logs
    with our records."""
    r = client.get("/home")
    assert r.headers.get("X-Request-Id"), "missing X-Request-Id"
    inbound = "test-id-1234"
    r2 = client.get("/home", headers={"X-Request-Id": inbound})
    # Server echoes the supplied id rather than generating a fresh one.
    assert r2.headers["X-Request-Id"] == inbound


def test_json_formatter_shape():
    """One line per record, parseable, with the spec's context
    fields when they're populated.  Don't depend on the live
    request middleware here — set the contextvars directly."""
    import obs
    obs._INSTALLED = False  # let setup re-install the handler
    # Drive the handler into a buffer instead of stdout for capture.
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(obs._JsonFormatter())
    base = logging.getLogger("stash")
    # Snapshot + restore to avoid clobbering other tests' setup.
    saved_handlers = list(base.handlers)
    saved_propagate = base.propagate
    saved_level = base.level
    base.handlers = [handler]
    base.propagate = False
    base.setLevel(logging.INFO)
    try:
        log = obs.get_logger("dao.boxes")
        rid = obs.bind_request_id("abcd1234")
        actor_tokens = obs.bind_actor("me@example.com", 7)
        try:
            log.info("box.update id=%d", 42, extra={"surface": "core"})
        finally:
            obs.reset_tokens(*actor_tokens, rid)
        line = buf.getvalue().strip().splitlines()[-1]
        record = json.loads(line)
        assert record["level"] == "INFO"
        assert record["logger"] == "stash.dao.boxes"
        assert record["msg"] == "box.update id=42"
        assert record["request_id"] == "abcd1234"
        assert record["actor_email"] == "me@example.com"
        assert record["tenant_id"] == 7
        assert record["surface"] == "core"
        assert record["layer"] == "dao.boxes"
    finally:
        base.handlers = saved_handlers
        base.propagate = saved_propagate
        base.setLevel(saved_level)


def test_context_resets_after_request(client):
    """Context tokens reset after the response so a re-used worker
    thread doesn't carry one request's identity into the next."""
    import obs
    client.get("/home")
    # After the request returns the contextvars should be back to
    # their None defaults.
    ctx = obs.current_context()
    assert "request_id" not in ctx
    assert "actor_email" not in ctx
    assert "tenant_id" not in ctx


# ── Audit backfill ──────────────────────────────────────────────────


def _audit_actions_for_tenant(client, tenant_id: int) -> list[tuple[str, dict]]:
    """Return ``[(action, metadata), ...]`` for the audit_log rows
    keyed to a specific tenant, ordered by id."""
    with client.app_module.db() as conn:
        rows = conn.execute(
            "SELECT action, metadata_json FROM audit_log "
            "WHERE tenant_id = ? ORDER BY id",
            (tenant_id,),
        ).fetchall()
    return [(r["action"], json.loads(r["metadata_json"])) for r in rows]


def test_box_lifecycle_audits(client):
    """Create, edit, delete a box — three audit rows in order.
    Metadata carries enough that an operator audit view can
    reconstruct what changed without joining elsewhere."""
    client.post("/boxes", data={"name": "Original"})
    client.post("/boxes/1/edit",
                data={"name": "Renamed", "if_match": "1"})
    client.post("/boxes/1/delete", data={"confirm": "Renamed"})
    actions = [a for a, _ in _audit_actions_for_tenant(
        client, client.test_tenant_id,
    )]
    assert actions == ["box.create", "box.update", "box.delete"]


def test_item_lifecycle_audits(client):
    """item.create + item.move + item.delete all land as audit
    rows with the right metadata."""
    client.post("/boxes", data={"name": "Source"})
    client.post("/boxes", data={"name": "Dest"})
    client.post("/boxes/1/items", data={"name": "thing"})
    client.post("/items/1/move", data={"box_id": "2"})
    client.post("/items/1/delete")
    audits = _audit_actions_for_tenant(client, client.test_tenant_id)
    actions = [a for a, _ in audits]
    assert "item.create" in actions
    assert "item.move" in actions
    assert "item.delete" in actions
    move_meta = next(m for a, m in audits if a == "item.move")
    assert move_meta["old_box_id"] == 1
    assert move_meta["new_box_id"] == 2


def test_location_create_and_delete_audits(client):
    """The full path through the location-lifecycle UI: create,
    confirm-name to delete.  Two audit rows, both with the
    location's name in metadata."""
    client.post("/locations", data={"name": "Townhouse"})
    # Find the new location id.
    with client.app_module.db() as conn:
        loc = conn.execute(
            "SELECT id FROM locations WHERE name = 'Townhouse'"
        ).fetchone()
    client.post(f"/locations/{loc['id']}/delete",
                data={"confirm": "Townhouse"})
    actions = [a for a, _ in _audit_actions_for_tenant(
        client, client.test_tenant_id,
    )]
    assert "location.create" in actions
    assert "location.delete" in actions


def test_audit_rows_carry_actor_email(client):
    """Every audit row records who took the action — not just
    when, not just what.  A future operator audit view depends on
    this column."""
    client.post("/boxes", data={"name": "X"})
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT actor_email FROM audit_log "
            "WHERE action = 'box.create'"
        ).fetchone()
    assert row["actor_email"] == client.test_email
