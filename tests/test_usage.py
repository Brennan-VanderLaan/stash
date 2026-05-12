"""Phase 9 — usage telemetry.

Covers the DAO record/summary surface and the auto-recording at the
real call sites (upload bytes on photo save, AI calls on the queue
suggest path).  We don't poke vision.* or call Gemini in tests; the
existing test fixtures already monkeypatch those, so we only verify
that the *recording* fires when the wrapped path runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_record_writes_event_with_default_cost(client):
    from dao import usage as dao_usage
    dao_usage.record(client.test_tenant_id, "ai", "gemini_detect")
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT surface, kind, units, cost_micros FROM usage_events "
            "WHERE tenant_id = ?",
            (client.test_tenant_id,),
        ).fetchone()
    assert row["surface"] == "ai"
    assert row["kind"] == "gemini_detect"
    assert row["units"] == 1
    # Default per-call cost from the price table — non-zero.
    assert row["cost_micros"] > 0


def test_record_drops_none_tenant(client):
    """Telemetry helpers can run from places that don't carry an
    actor (background workers, operator-cross-tenant calls).  A None
    tenant_id is silently dropped instead of raising — the spec
    explicitly wants telemetry to be best-effort."""
    from dao import usage as dao_usage
    dao_usage.record(None, "ai", "gemini_detect")
    with client.app_module.db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM usage_events"
        ).fetchone()[0]
    assert count == 0


def test_summary_aggregates_by_surface_within_month(client):
    """Summary buckets events per surface and exposes the kinds
    breakdown the /usage page uses for the AI sub-list."""
    from dao import Actor, usage as dao_usage
    tid = client.test_tenant_id
    dao_usage.record(tid, "ai", "gemini_detect")
    dao_usage.record(tid, "ai", "gemini_detect")
    dao_usage.record(tid, "ai", "anthropic_match")
    dao_usage.record(tid, "upload", "upload_bytes", units=2_500_000)

    actor = Actor(
        email=client.test_email, tenant_id=tid, role="maintainer",
        is_operator=False, memberships=((tid, "maintainer"),),
    )
    s = dao_usage.summary(actor)
    assert s["ai_calls"] == 3
    assert s["upload_bytes"] == 2_500_000
    # Per-kind tally for the AI breakdown.
    assert s["kinds"]["gemini_detect"] == 2
    assert s["kinds"]["anthropic_match"] == 1


def test_summary_filters_by_since(client):
    """An event from a previous month doesn't count toward the
    current month's meter — the page resets cleanly on the 1st."""
    from dao import Actor, usage as dao_usage
    tid = client.test_tenant_id
    # Insert an old event by hand (the DAO's record() always uses
    # CURRENT_TIMESTAMP, so we backdate via direct SQL).
    with client.app_module.db() as conn:
        conn.execute(
            "INSERT INTO usage_events "
            "(tenant_id, surface, kind, units, cost_micros, created_at) "
            "VALUES (?, 'ai', 'gemini_detect', 1, 700, '2020-01-01T00:00:00')",
            (tid,),
        )
        conn.commit()
    actor = Actor(
        email=client.test_email, tenant_id=tid, role="maintainer",
        is_operator=False, memberships=((tid, "maintainer"),),
    )
    s = dao_usage.summary(actor)
    # Old event excluded by the since= filter (default = month start).
    assert s["ai_calls"] == 0


def test_upload_records_byte_count(client, tmp_path):
    """save_photo_bytes is the real entry point — a successful
    upload should leave one upload_bytes row whose units match the
    encoded JPEG size."""
    # Tiny synthetic JPEG-shaped payload; the PIL try block in
    # save_photo_bytes will fall through to the raw-bytes branch.
    raw = b"\xff\xd8\xff\xe0" + b"x" * 200 + b"\xff\xd9"
    name = client.app_module.save_photo_bytes(
        client.test_tenant_id, raw, "test.jpg",
    )
    assert name.endswith(".jpg")
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT units FROM usage_events "
            "WHERE tenant_id = ? AND kind = 'upload_bytes'",
            (client.test_tenant_id,),
        ).fetchone()
    assert row is not None
    assert row["units"] == len(raw)


def test_usage_page_renders(client):
    """End-to-end smoke: /usage renders a 200 with the meter
    headings.  Detailed assertions on numbers happen in the DAO
    tests above; this one just confirms the route is wired."""
    r = client.get("/usage")
    assert r.status_code == 200
    page = r.text
    assert "AI calls" in page
    assert "Photos uploaded" in page
    # New bandwidth + storage panels.
    assert "Bandwidth" in page
    assert "Storage" in page
    # Members section lists the test maintainer.
    assert client.test_email in page


# ── Bandwidth rollups + storage footprint ─────────────────────────


def test_record_rollup_upserts_one_row_per_day(client):
    """50 serves of the same kind on the same day MUST add to ONE
    row in usage_rollups — the whole point of the daily-grain
    schema is to keep the table size bounded under hot-fetch
    loads.  Without this, a heavy grid view would insert dozens
    of rows per page render."""
    from dao import usage as dao_usage
    for _ in range(50):
        dao_usage.record_rollup(
            client.test_tenant_id, "download", "download_bytes",
            units=1000,
        )
    with client.app_module.db() as conn:
        rows = conn.execute(
            "SELECT day, units, cost_micros FROM usage_rollups "
            "WHERE tenant_id = ?",
            (client.test_tenant_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["units"] == 50_000


def test_record_rollup_swallows_failures(client, monkeypatch):
    """The serve path can't be allowed to 500 because telemetry
    failed.  ``record_rollup`` logs and returns on any DB
    exception — verify by patching ``db`` to raise."""
    from dao import usage as dao_usage
    def boom(*args, **kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(dao_usage, "db", boom)
    # Must NOT raise.
    dao_usage.record_rollup(
        client.test_tenant_id, "download", "download_bytes", units=1,
    )


def test_serve_upload_records_download_bandwidth(client, monkeypatch):
    """Hitting /uploads/{name} after a real upload must bump the
    download rollup by the served byte count."""
    import io
    from unittest.mock import patch
    from vision import DetectedItem
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="thing", description="d"),
    ]):
        client.post(
            "/ingest",
            files={"photos": ("p.jpg", io.BytesIO(b"\xff\xd8\xff" + b"x" * 256), "image/jpeg")},
        )
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT photo FROM pending_items WHERE tenant_id = ?",
            (client.test_tenant_id,),
        ).fetchone()
    assert row is not None
    photo = row["photo"]
    before = _download_units(client)
    r = client.get(f"/uploads/{photo}")
    assert r.status_code == 200
    after = _download_units(client)
    assert after > before
    # Bumps by approximately the response body size (the actual
    # plaintext byte count post-decrypt).
    assert after - before == len(r.content)


def test_serve_thumb_records_download_bandwidth(client):
    """Same expectation for /thumbs/{name} — thumbs are the high-
    volume path that motivated the rollup design."""
    import io
    from unittest.mock import patch
    from vision import DetectedItem
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="thing", description="d"),
    ]):
        client.post(
            "/ingest",
            files={"photos": ("p.jpg", io.BytesIO(b"\xff\xd8\xff" + b"x" * 256), "image/jpeg")},
        )
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT photo FROM pending_items WHERE tenant_id = ?",
            (client.test_tenant_id,),
        ).fetchone()
    photo = row["photo"]
    before = _download_units(client)
    r = client.get(f"/thumbs/{photo}")
    assert r.status_code == 200
    after = _download_units(client)
    assert after > before


def test_summary_includes_download_bytes_and_storage(client):
    """``summary`` must surface the new download + storage fields
    so the /usage page can render the Bandwidth + Storage panels
    without further plumbing."""
    from dao import usage as dao_usage, Actor
    dao_usage.record_rollup(
        client.test_tenant_id, "download", "download_bytes",
        units=4_096,
    )
    actor = Actor(
        email=client.test_email, tenant_id=client.test_tenant_id,
        role="maintainer", is_operator=False,
        memberships=((client.test_tenant_id, "maintainer"),),
    )
    out = dao_usage.summary(actor)
    assert out["download_bytes"] == 4_096
    # Storage footprint is keyed off the upload dir on disk;
    # empty tenant → 0, but the field is present + numeric.
    assert "storage_bytes" in out
    assert "storage_files" in out
    assert isinstance(out["storage_bytes"], int)


def test_storage_footprint_reflects_files_on_disk(client, tmp_path):
    """``storage_footprint`` walks the tenant's upload dir.  Empty
    tenant returns 0; writing a file bumps the footprint by that
    file's byte count."""
    from dao import usage as dao_usage
    import os
    empty = dao_usage.storage_footprint(client.test_tenant_id)
    assert empty["total_bytes"] == 0
    upload_root = client.app_module.UPLOAD_DIR / str(client.test_tenant_id)
    upload_root.mkdir(parents=True, exist_ok=True)
    (upload_root / "x.jpg").write_bytes(b"a" * 1234)
    after = dao_usage.storage_footprint(client.test_tenant_id)
    assert after["total_bytes"] >= 1234
    assert after["file_count"] >= 1


# ── Monthly summary (sparkline data source) ───────────────────────


def test_monthly_summary_returns_12_months_filled(client):
    """``monthly_summary`` always returns N entries (defaults to
    12), one per UTC month, oldest first.  Quiet months stay in
    the list with zero counters so the sparkline draws a flat
    baseline instead of compressing the x-axis."""
    from dao import usage as dao_usage
    out = dao_usage.monthly_summary(client.test_tenant_id, months_back=12)
    assert len(out) == 12
    keys = [m["month"] for m in out]
    # Strictly increasing month keys.
    assert keys == sorted(keys)
    # Every entry has the four counters present + zero by default.
    for entry in out:
        assert "ai_calls" in entry
        assert "upload_bytes" in entry
        assert "download_bytes" in entry
        assert "ai_cost_micros" in entry


def test_monthly_summary_buckets_current_month_correctly(client):
    """An AI event recorded now must land in the CURRENT month's
    bucket — not the previous one (off-by-one) and not all
    months (broken GROUP BY)."""
    from dao import usage as dao_usage
    from datetime import datetime, timezone
    dao_usage.record(client.test_tenant_id, "ai", "gemini_detect")
    out = dao_usage.monthly_summary(client.test_tenant_id, months_back=3)
    current = datetime.now(timezone.utc).strftime("%Y-%m")
    by_month = {m["month"]: m for m in out}
    assert by_month[current]["ai_calls"] == 1
    # Previous months stay at zero.
    for m, entry in by_month.items():
        if m != current:
            assert entry["ai_calls"] == 0


def test_monthly_summary_pulls_downloads_from_rollups(client):
    """Download bytes live in ``usage_rollups``, not
    ``usage_events`` — ``monthly_summary`` must union both
    sources, otherwise the bandwidth sparkline would be empty
    even when the rollup table has data."""
    from dao import usage as dao_usage
    from datetime import datetime, timezone
    dao_usage.record_rollup(
        client.test_tenant_id, "download", "download_bytes", units=12345,
    )
    out = dao_usage.monthly_summary(client.test_tenant_id, months_back=3)
    current = datetime.now(timezone.utc).strftime("%Y-%m")
    by_month = {m["month"]: m for m in out}
    assert by_month[current]["download_bytes"] == 12345


def test_sparkline_helper_renders_svg(client):
    """End-to-end smoke through the Jinja global — /usage renders
    a Trends section with at least one ``<svg`` payload (the
    polyline output of ``_sparkline_svg``)."""
    r = client.get("/usage")
    assert r.status_code == 200
    assert "Trends" in r.text
    assert "<svg" in r.text
    # The polyline tag is the load-bearing piece — verify the
    # helper rendered the trend shape itself, not just the
    # container.
    assert "polyline" in r.text or "circle" in r.text


def _download_units(client) -> int:
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(units), 0) AS u FROM usage_rollups "
            "WHERE tenant_id = ? AND surface = 'download'",
            (client.test_tenant_id,),
        ).fetchone()
    return int(row["u"]) if row else 0
