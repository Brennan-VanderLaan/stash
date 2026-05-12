"""Telemetry — per-tenant usage event recording + aggregate readback.

Spec § "Telemetry & quotas".  Phase 9 surface: count every AI call,
every successful upload, every backup write, keyed by tenant.  Cost
is approximated from a hard-coded price table — refined later when
real billing lands.

The recording surface is intentionally tiny so call sites can wrap
it without ceremony: ``record(tenant_id, "ai", "gemini_detect")`` is
a one-liner.  The summary surface aggregates per surface so the
``/usage`` page can render meters without joining or pivoting.

Race between counter writes and quota checks is acknowledged in the
spec (we'd rather over-serve than block in-flight work); this module
makes no attempt to be transactional with the surface it instruments.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dao._base import Actor, db


# ── Pricing ─────────────────────────────────────────────────────────


# Cost approximations in micro-dollars (1e-6 USD) per unit.  These
# are deliberately rough — Gemini and Anthropic price by token; we
# count *calls* and apply a typical-cost-per-call so the operator
# panel surfaces the right order of magnitude without a token-counting
# pass.  Refined when phase 12 (operator panel) needs real numbers.
#
# Sources (accurate as of cutoff; bake into spec when billing ships):
#   * Gemini Flash detect: ~$0.0007/call typical (image + ~150 toks)
#   * Gemini 3 Pro Image (label art): ~$0.04/call (image generation)
#   * Anthropic Opus suggest_box: ~$0.0015/call (small text round-trip)
#   * B2 storage: ~$6/TB/month → 6 µ$ per byte-month, but we charge per
#     write event for now; storage-time billing arrives with B2 phase.
_PRICE_MICROS_PER_UNIT: dict[tuple[str, str], int] = {
    ("ai", "gemini_detect"): 700,
    ("ai", "gemini_art"): 40_000,
    ("ai", "anthropic_match"): 1_500,
    # upload_bytes records bytes; ~0.000006 µ$/byte (storage cost
    # only); rounded to 1 µ$/MB so multiplication stays an integer.
    ("upload", "upload_bytes"): 0,  # filled by formula in record()
    ("backup", "backup_bytes"): 0,
    # Egress bandwidth (per-byte cost).  B2 egress: ~$10/TB → 10 µ$/MB.
    # We round to 1 µ$/MB to match upload's conservative figure; the
    # number's purpose is order-of-magnitude visibility, not billing.
    ("download", "download_bytes"): 0,  # filled by formula in record_rollup()
}


def _cost_for(surface: str, kind: str, units: int) -> int:
    """Cost in micro-dollars for a recorded event.  Per-call surfaces
    use the table directly; byte-counted surfaces apply a per-MB rate
    so the integer math doesn't underflow."""
    per_unit = _PRICE_MICROS_PER_UNIT.get((surface, kind), 0)
    if per_unit:
        return per_unit * units
    if surface == "upload" and kind == "upload_bytes":
        # 1 µ$ per MB stored → conservative B2 ingest+storage estimate.
        return units // (1024 * 1024)
    if surface == "backup" and kind == "backup_bytes":
        return units // (1024 * 1024)
    if surface == "download" and kind == "download_bytes":
        # 1 µ$ per MB egress — same conservative rate.
        return units // (1024 * 1024)
    return 0


# ── Record ──────────────────────────────────────────────────────────


def record(
    tenant_id: int | None,
    surface: str,
    kind: str,
    *,
    units: int = 1,
    cost_micros: int | None = None,
) -> None:
    """Append a usage event.  No actor parameter — the call sites
    that record telemetry are typically deep helpers (vision, vault)
    that don't carry an Actor.  Tenant scoping is the caller's
    responsibility; ``tenant_id=None`` is silently dropped (e.g. for
    operator-cross-tenant calls that don't belong to anybody)."""
    if tenant_id is None:
        return
    if surface not in ("ai", "upload", "backup", "core", "mcp"):
        raise ValueError(f"unknown surface {surface!r}")
    if cost_micros is None:
        cost_micros = _cost_for(surface, kind, units)
    with db() as conn:
        conn.execute(
            "INSERT INTO usage_events "
            "(tenant_id, surface, kind, units, cost_micros) "
            "VALUES (?, ?, ?, ?, ?)",
            (tenant_id, surface, kind, units, cost_micros),
        )
        conn.commit()


def record_rollup(
    tenant_id: int | None,
    surface: str,
    kind: str,
    *,
    units: int = 1,
    cost_micros: int | None = None,
) -> None:
    """High-frequency counter — UPSERTs into ``usage_rollups`` so a
    busy page view that fetches 50 thumbs adds 50 to a single
    row's ``units`` instead of inserting 50 rows.

    Currently the only call site is the ``serve_upload`` /
    ``serve_thumb`` download path, where event-per-fetch would
    have made the table grow unboundedly on a hobby VM.  Daily
    grain (``day`` keyed in UTC) keeps the row count to at most
    N tenants × M kinds rows/day; for the foreseeable future
    that's a handful of rows/day total.

    Failure is intentionally swallowed (logged but not raised) so
    a telemetry blip can't fault the serve path it's
    instrumenting."""
    if tenant_id is None or units <= 0:
        return
    if surface not in ("ai", "upload", "backup", "core", "mcp", "download"):
        raise ValueError(f"unknown surface {surface!r}")
    if cost_micros is None:
        cost_micros = _cost_for(surface, kind, units)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO usage_rollups "
                "(tenant_id, day, surface, kind, units, cost_micros) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, day, surface, kind) DO UPDATE SET "
                "  units = units + excluded.units, "
                "  cost_micros = cost_micros + excluded.cost_micros",
                (tenant_id, today, surface, kind, units, cost_micros),
            )
            conn.commit()
    except Exception:
        import logging
        logging.getLogger("stash.dao.usage").exception(
            "record_rollup failed tenant=%s surface=%s kind=%s units=%s",
            tenant_id, surface, kind, units,
        )


def storage_footprint(tenant_id: int | None) -> dict:
    """Current on-disk usage for ``tenant_id``: walk
    ``UPLOAD_DIR/{tenant_id}/`` and sum file sizes.

    Returns ``{"total_bytes": N, "file_count": N}``.  Cheap on a
    hobby-scale stash (low thousands of files); if this ever
    becomes a hot path we'll cache the result in a rollup row
    refreshed by a background sweep.

    The DEK / encryption overhead is included — these are the
    actual on-disk bytes the storage device sees, not the
    plaintext size.  Operators comparing this against B2 quota
    should look at *this* number, not the cumulative
    ``upload_bytes`` event-log total (which counts every write
    forever, including deletes)."""
    if tenant_id is None:
        return {"total_bytes": 0, "file_count": 0}
    import os
    from pathlib import Path
    upload_dir = Path(os.environ.get("STASH_UPLOADS", "uploads"))
    tenant_root = upload_dir / str(tenant_id)
    if not tenant_root.exists():
        return {"total_bytes": 0, "file_count": 0}
    total = 0
    count = 0
    for root, _dirs, files in os.walk(tenant_root):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
                count += 1
            except OSError:
                pass
    return {"total_bytes": total, "file_count": count}


# ── Read ────────────────────────────────────────────────────────────


def _month_start_utc() -> str:
    """``YYYY-MM-DD HH:MM:SS`` start of the current UTC month.
    Format matches SQLite's ``CURRENT_TIMESTAMP`` so a string
    compare doesn't trip on ``' '`` vs ``'T'`` lexical difference
    within the same date prefix."""
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0,
                       microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def summary(actor: Actor, *, since: str | None = None) -> dict:
    """Per-surface aggregate for the actor's tenant since ``since``
    (defaulting to the start of this UTC month).

    Returns a flat dict tuned for direct template consumption — no
    nested structure, no joins on the read side, just numbers the
    page can drop into meters:

    .. code-block:: python

        {
            "since": "2026-05-01T00:00:00+00:00",
            "ai_calls": 42,
            "ai_cost_micros": 30_100,
            "upload_bytes": 12_345_678,
            "upload_cost_micros": 11,
            "backup_bytes": 0,
            "backup_cost_micros": 0,
            "kinds": {
                "gemini_detect": 30,
                "gemini_art": 2,
                "anthropic_match": 10,
            },
            "item_count": ...,   # from items table, current totals
            "box_count": ...,
        }
    """
    tenant_id = actor.tenant_id
    if tenant_id is None:
        return _empty_summary()
    since = since or _month_start_utc()
    # ``since`` is "YYYY-MM-DD HH:MM:SS" but the rollups table keys
    # on date-only ("YYYY-MM-DD") — slicing to 10 chars gives the
    # right comparison key without parsing.
    since_day = since[:10]
    with db() as conn:
        per_surface = {
            r["surface"]: r for r in conn.execute(
                "SELECT surface, SUM(units) AS units, "
                "       SUM(cost_micros) AS cost_micros "
                "FROM usage_events "
                "WHERE tenant_id = ? AND created_at >= ? "
                "GROUP BY surface",
                (tenant_id, since),
            ).fetchall()
        }
        # Download bandwidth lives in usage_rollups (one row/day
        # per kind) instead of usage_events; pull it separately
        # and merge into the per-surface map.
        for r in conn.execute(
            "SELECT surface, SUM(units) AS units, "
            "       SUM(cost_micros) AS cost_micros "
            "FROM usage_rollups "
            "WHERE tenant_id = ? AND day >= ? "
            "GROUP BY surface",
            (tenant_id, since_day),
        ).fetchall():
            per_surface[r["surface"]] = r
        kinds = {
            r["kind"]: r["units"] for r in conn.execute(
                "SELECT kind, SUM(units) AS units "
                "FROM usage_events "
                "WHERE tenant_id = ? AND surface = 'ai' "
                "  AND created_at >= ? "
                "GROUP BY kind",
                (tenant_id, since),
            ).fetchall()
        }
        # Live totals on item / box count — these aren't usage_events
        # rows (deletes would skew the running counter) so we read
        # the current state directly.
        item_count = conn.execute(
            "SELECT COUNT(*) FROM items WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()[0]
        box_count = conn.execute(
            "SELECT COUNT(*) FROM boxes WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()[0]
    footprint = storage_footprint(tenant_id)
    out = {
        "since": since,
        "ai_calls": _units(per_surface, "ai"),
        "ai_cost_micros": _cost(per_surface, "ai"),
        "upload_bytes": _units(per_surface, "upload"),
        "upload_cost_micros": _cost(per_surface, "upload"),
        "download_bytes": _units(per_surface, "download"),
        "download_cost_micros": _cost(per_surface, "download"),
        "backup_bytes": _units(per_surface, "backup"),
        "backup_cost_micros": _cost(per_surface, "backup"),
        "kinds": kinds,
        "item_count": item_count,
        "box_count": box_count,
        "storage_bytes": footprint["total_bytes"],
        "storage_files": footprint["file_count"],
    }
    # Cap + percent + band per surface — the /usage meters render
    # these directly.  Imports lazily to avoid a circular import
    # at module load (dao.quotas reads usage_events).
    from dao import quotas as dao_quotas
    caps = dao_quotas.get_caps(tenant_id)
    today_usage = dao_quotas.usage_for_tenant(tenant_id)
    for key in ("monthly_ai_calls", "monthly_upload_bytes",
                "daily_ai_cost_micros"):
        cap = caps.get(key)
        used = today_usage.get(key, 0)
        out[f"{key}_cap"] = cap
        out[f"{key}_used"] = used
        out[f"{key}_percent"] = dao_quotas.percent(used, cap)
        out[f"{key}_band"] = dao_quotas.warning_band(used, cap)
    return out


def monthly_summary(
    tenant_id: int | None, *, months_back: int = 12,
) -> list[dict]:
    """Per-month aggregate going back ``months_back`` months
    (inclusive of the current month).  Powers the sparkline panel
    on /usage so the user can see whether AI calls / bandwidth /
    cost are trending up.

    Each entry is a dict with::

        {
            "month": "2026-05",
            "ai_calls": int,
            "ai_cost_micros": int,
            "upload_bytes": int,
            "download_bytes": int,
        }

    Months with zero activity still appear in the list (with all
    counters at 0) so the sparkline draws a continuous timeline —
    a "missing month" in the SQL output would otherwise smush the
    plot's x-axis and hide a quiet period as if it never happened.

    Storage footprint is intentionally NOT here: storage_bytes is
    a *current-state* number (walks the tenant's upload dir) and
    we don't keep historical snapshots.  Adding that's a separate
    phase if the trend matters; the bandwidth + cost trends here
    already cover the common "did my AI spend creep" question.
    """
    if tenant_id is None:
        return _empty_months(months_back)
    months = _month_keys(months_back)
    by_month = {m: {"ai_calls": 0, "ai_cost_micros": 0,
                    "upload_bytes": 0, "download_bytes": 0}
                for m in months}
    earliest = months[0] + "-01"  # YYYY-MM-01
    with db() as conn:
        # AI + uploads + backups still live in usage_events.
        for r in conn.execute(
            "SELECT substr(created_at, 1, 7) AS month, "
            "       surface, "
            "       SUM(units) AS units, "
            "       SUM(cost_micros) AS cost "
            "FROM usage_events "
            "WHERE tenant_id = ? AND created_at >= ? "
            "GROUP BY month, surface",
            (tenant_id, earliest),
        ).fetchall():
            m = r["month"]
            if m not in by_month:
                continue
            if r["surface"] == "ai":
                by_month[m]["ai_calls"] += int(r["units"] or 0)
                by_month[m]["ai_cost_micros"] += int(r["cost"] or 0)
            elif r["surface"] == "upload":
                by_month[m]["upload_bytes"] += int(r["units"] or 0)
        # Downloads come from the daily rollup table.
        for r in conn.execute(
            "SELECT substr(day, 1, 7) AS month, "
            "       SUM(units) AS units "
            "FROM usage_rollups "
            "WHERE tenant_id = ? AND day >= ? "
            "  AND surface = 'download' "
            "GROUP BY month",
            (tenant_id, earliest),
        ).fetchall():
            m = r["month"]
            if m in by_month:
                by_month[m]["download_bytes"] += int(r["units"] or 0)
    return [{"month": m, **by_month[m]} for m in months]


def _month_keys(n: int) -> list[str]:
    """Last ``n`` month strings ('YYYY-MM') ending with the
    current UTC month, oldest first."""
    out: list[str] = []
    now = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0,
    )
    cur = now
    for _ in range(n):
        out.append(cur.strftime("%Y-%m"))
        # Roll back one month by subtracting a day and zeroing.
        prev_month_last_day = cur - timedelta(days=1)
        cur = prev_month_last_day.replace(day=1)
    return list(reversed(out))


def _empty_months(n: int) -> list[dict]:
    return [
        {"month": m, "ai_calls": 0, "ai_cost_micros": 0,
         "upload_bytes": 0, "download_bytes": 0}
        for m in _month_keys(n)
    ]


def _units(per_surface: dict, surface: str) -> int:
    row = per_surface.get(surface)
    return int(row["units"]) if row and row["units"] is not None else 0


def _cost(per_surface: dict, surface: str) -> int:
    row = per_surface.get(surface)
    return int(row["cost_micros"]) if row and row["cost_micros"] is not None else 0


def _empty_summary() -> dict:
    out = {
        "since": _month_start_utc(),
        "ai_calls": 0, "ai_cost_micros": 0,
        "upload_bytes": 0, "upload_cost_micros": 0,
        "download_bytes": 0, "download_cost_micros": 0,
        "backup_bytes": 0, "backup_cost_micros": 0,
        "kinds": {},
        "item_count": 0,
        "box_count": 0,
        "storage_bytes": 0,
        "storage_files": 0,
    }
    for key in ("monthly_ai_calls", "monthly_upload_bytes",
                "daily_ai_cost_micros"):
        out[f"{key}_cap"] = None
        out[f"{key}_used"] = 0
        out[f"{key}_percent"] = 0
        out[f"{key}_band"] = None
    return out
