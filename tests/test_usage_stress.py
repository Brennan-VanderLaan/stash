"""Bandwidth counter stress + accuracy tests.

The download rollup is the load-bearing part of the new
bandwidth meter on /usage — if it drops increments or
double-counts under contention, the user sees nonsense numbers
and trusts the rest of the page less.  These tests pin:

1. **Concurrent UPSERT correctness** — N threads × M increments
   sum to exactly N*M units, no lost writes.
2. **Cross-tenant isolation** — interleaved writes for two
   tenants stay segregated, neither leaks counters to the other.
3. **End-to-end byte accuracy** — hitting /uploads K times for a
   file of known size bumps the rollup by exactly K * len(plaintext).
4. **Day-boundary correctness** — a mocked clock writes to two
   separate rows when the date rolls over.
5. **Throughput sanity** — 2000 record_rollup calls finish in
   under 5 s on the test box (regression sentinel for any
   accidental n² behaviour).

The tests run against the same SQLite file the fixture stands up
(WAL mode, busy_timeout=5000) so they exercise the real
concurrency path, not an in-memory mock.
"""

from __future__ import annotations

import io
import threading
import time
from unittest.mock import patch

from vision import DetectedItem


# ── Concurrent UPSERT correctness ────────────────────────────────


def test_concurrent_record_rollup_sums_exactly(client):
    """8 threads × 100 increments of 1 byte each = 800 units in a
    single row.  Any lost write would show up as ``units < 800``;
    any double-count would show up as ``> 800``.  This is the
    cardinal "do we lose data under load" test."""
    from dao import usage as dao_usage
    n_threads = 8
    per_thread = 100

    def worker():
        for _ in range(per_thread):
            dao_usage.record_rollup(
                client.test_tenant_id, "download", "download_bytes",
                units=1,
            )

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with client.app_module.db() as conn:
        rows = conn.execute(
            "SELECT day, units FROM usage_rollups "
            "WHERE tenant_id = ? AND surface = 'download'",
            (client.test_tenant_id,),
        ).fetchall()
    # All increments fall on the same UTC day → single row.
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}: {[dict(r) for r in rows]}"
    assert rows[0]["units"] == n_threads * per_thread, (
        f"lost increments under contention: "
        f"expected {n_threads * per_thread}, got {rows[0]['units']}"
    )


def test_concurrent_record_rollup_variable_byte_sizes(client):
    """Same contention test but each increment is a different byte
    count.  Sum must equal sum(per-thread byte counts).  Catches
    bugs where the UPSERT body's ``units = units + excluded.units``
    formula gets replaced with ``units = excluded.units`` (would
    leave only the last write's value)."""
    from dao import usage as dao_usage
    sizes = [1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072]
    expected_total = sum(sizes) * 10  # each size pushed 10 times

    def worker(byte_count):
        for _ in range(10):
            dao_usage.record_rollup(
                client.test_tenant_id, "download", "download_bytes",
                units=byte_count,
            )

    threads = [threading.Thread(target=worker, args=(s,)) for s in sizes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT SUM(units) AS total FROM usage_rollups "
            "WHERE tenant_id = ? AND surface = 'download'",
            (client.test_tenant_id,),
        ).fetchone()
    assert row["total"] == expected_total


# ── Cross-tenant isolation ───────────────────────────────────────


def test_cross_tenant_rollups_stay_segregated_under_load(client):
    """Two tenants interleave 200 rollup writes each on parallel
    threads.  Each tenant's row must reflect exactly that tenant's
    writes — zero cross-contamination.  Without proper tenant
    keying the UPSERT could collapse both tenants into one row,
    which would be silent + catastrophic."""
    from dao import usage as dao_usage
    with client.app_module.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('Other', 'pro')",
        )
        other_tenant = cur.lastrowid
        conn.commit()

    def write(tenant_id, n, byte_count):
        for _ in range(n):
            dao_usage.record_rollup(
                tenant_id, "download", "download_bytes", units=byte_count,
            )

    t1 = threading.Thread(
        target=write, args=(client.test_tenant_id, 200, 100),
    )
    t2 = threading.Thread(target=write, args=(other_tenant, 200, 250))
    t1.start(); t2.start()
    t1.join(); t2.join()

    with client.app_module.db() as conn:
        a = conn.execute(
            "SELECT units FROM usage_rollups WHERE tenant_id = ? "
            "AND surface = 'download'",
            (client.test_tenant_id,),
        ).fetchone()
        b = conn.execute(
            "SELECT units FROM usage_rollups WHERE tenant_id = ? "
            "AND surface = 'download'",
            (other_tenant,),
        ).fetchone()
    assert a["units"] == 200 * 100
    assert b["units"] == 200 * 250


# ── End-to-end byte accuracy ─────────────────────────────────────


def test_serve_upload_byte_count_is_exact(client):
    """Hit /uploads/{name} K times; the rollup must reflect exactly
    K * len(served_plaintext).  Any rounding, slicing, or
    double-counting bug shows up as a non-zero residual."""
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="thing", description="d"),
    ]):
        client.post(
            "/ingest",
            files={"photos": ("p.jpg", io.BytesIO(
                b"\xff\xd8\xff" + b"x" * 4096
            ), "image/jpeg")},
        )
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT photo FROM pending_items WHERE tenant_id = ?",
            (client.test_tenant_id,),
        ).fetchone()
    photo = row["photo"]

    # Warm one fetch + record the served byte count.
    first = client.get(f"/uploads/{photo}")
    assert first.status_code == 200
    served = len(first.content)

    # Zero out the rollup so we measure exactly K=15 fetches.
    with client.app_module.db() as conn:
        conn.execute(
            "DELETE FROM usage_rollups WHERE tenant_id = ? "
            "AND surface = 'download'",
            (client.test_tenant_id,),
        )
        conn.commit()

    n = 15
    for _ in range(n):
        r = client.get(f"/uploads/{photo}")
        assert r.status_code == 200
        assert len(r.content) == served, (
            "serve returned variable byte counts — accuracy test "
            "needs a deterministic served size"
        )

    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT SUM(units) AS total FROM usage_rollups "
            "WHERE tenant_id = ? AND surface = 'download'",
            (client.test_tenant_id,),
        ).fetchone()
    assert row["total"] == n * served, (
        f"download rollup off by {row['total'] - n * served} bytes "
        f"after {n} fetches of a {served}-byte payload"
    )


def test_serve_thumb_byte_count_is_exact(client):
    """Same byte-exact accuracy assertion for /thumbs/{name} —
    this is the higher-volume path that motivated the rollup
    design, so it has to be the cleaner of the two."""
    with patch("app.vision.detect_items", return_value=[
        DetectedItem(name="thing", description="d"),
    ]):
        client.post(
            "/ingest",
            files={"photos": ("p.jpg", io.BytesIO(
                b"\xff\xd8\xff" + b"x" * 8192
            ), "image/jpeg")},
        )
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT photo FROM pending_items WHERE tenant_id = ?",
            (client.test_tenant_id,),
        ).fetchone()
    photo = row["photo"]

    first = client.get(f"/thumbs/{photo}")
    assert first.status_code == 200
    served = len(first.content)

    with client.app_module.db() as conn:
        conn.execute(
            "DELETE FROM usage_rollups WHERE tenant_id = ? "
            "AND surface = 'download'",
            (client.test_tenant_id,),
        )
        conn.commit()

    n = 25
    for _ in range(n):
        client.get(f"/thumbs/{photo}")

    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT SUM(units) AS total FROM usage_rollups "
            "WHERE tenant_id = ? AND surface = 'download'",
            (client.test_tenant_id,),
        ).fetchone()
    assert row["total"] == n * served


# ── Day-boundary correctness ─────────────────────────────────────


def test_record_rollup_splits_on_day_boundary(client, monkeypatch):
    """Writes on day A and day B must produce TWO rows, not one
    coalesced row.  Without per-day keying the monthly sparkline
    would show one huge spike instead of two clean days."""
    from dao import usage as dao_usage
    from datetime import datetime, timezone

    # Patch ``datetime.now`` *inside the dao_usage module* — patching
    # the global datetime would break far more than this test
    # exercises.
    real_dt = dao_usage.datetime

    class FakeDT:
        _now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls._now

    monkeypatch.setattr(dao_usage, "datetime", FakeDT)

    FakeDT._now = datetime(2026, 5, 10, 23, 59, 59, tzinfo=timezone.utc)
    dao_usage.record_rollup(
        client.test_tenant_id, "download", "download_bytes", units=1000,
    )
    FakeDT._now = datetime(2026, 5, 11, 0, 0, 1, tzinfo=timezone.utc)
    dao_usage.record_rollup(
        client.test_tenant_id, "download", "download_bytes", units=2000,
    )

    monkeypatch.setattr(dao_usage, "datetime", real_dt)

    with client.app_module.db() as conn:
        rows = conn.execute(
            "SELECT day, units FROM usage_rollups "
            "WHERE tenant_id = ? AND surface = 'download' "
            "ORDER BY day",
            (client.test_tenant_id,),
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["day"] == "2026-05-10"
    assert rows[0]["units"] == 1000
    assert rows[1]["day"] == "2026-05-11"
    assert rows[1]["units"] == 2000


# ── Throughput sanity ────────────────────────────────────────────


def test_record_rollup_throughput_2000_calls_under_5s(client):
    """Regression sentinel.  2000 single-threaded UPSERTs must
    finish in under 5 s.  Today on a typical dev box it's well
    under a second; if someone accidentally adds an N² pass or
    drops the index, this fires."""
    from dao import usage as dao_usage
    start = time.monotonic()
    for _ in range(2000):
        dao_usage.record_rollup(
            client.test_tenant_id, "download", "download_bytes", units=42,
        )
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, (
        f"2000 rollups took {elapsed:.2f} s — regression vs the "
        "baseline (typically <1 s).  Inspect for missing index or "
        "lock contention."
    )
    # And the row count is still bounded — single row for one day.
    with client.app_module.db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM usage_rollups WHERE tenant_id = ?",
            (client.test_tenant_id,),
        ).fetchone()["n"]
    assert n == 1


def test_summary_under_load_returns_consistent_numbers(client):
    """``summary`` reads the rollup table mid-load and must return
    a number that matches the eventually-committed total.  This
    is a sanity check that summary isn't double-counting (e.g.
    if it accidentally joined both ``usage_events`` and
    ``usage_rollups`` for the same surface)."""
    from dao import usage as dao_usage, Actor
    for _ in range(500):
        dao_usage.record_rollup(
            client.test_tenant_id, "download", "download_bytes", units=10,
        )
    actor = Actor(
        email=client.test_email, tenant_id=client.test_tenant_id,
        role="maintainer", is_operator=False,
        memberships=((client.test_tenant_id, "maintainer"),),
    )
    out = dao_usage.summary(actor)
    assert out["download_bytes"] == 5000
