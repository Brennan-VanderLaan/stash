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

from datetime import datetime, timezone

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
    if surface not in ("ai", "upload", "backup", "core"):
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
    out = {
        "since": since,
        "ai_calls": _units(per_surface, "ai"),
        "ai_cost_micros": _cost(per_surface, "ai"),
        "upload_bytes": _units(per_surface, "upload"),
        "upload_cost_micros": _cost(per_surface, "upload"),
        "backup_bytes": _units(per_surface, "backup"),
        "backup_cost_micros": _cost(per_surface, "backup"),
        "kinds": kinds,
        "item_count": item_count,
        "box_count": box_count,
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
        "backup_bytes": 0, "backup_cost_micros": 0,
        "kinds": {},
        "item_count": 0,
        "box_count": 0,
    }
    for key in ("monthly_ai_calls", "monthly_upload_bytes",
                "daily_ai_cost_micros"):
        out[f"{key}_cap"] = None
        out[f"{key}_used"] = 0
        out[f"{key}_percent"] = 0
        out[f"{key}_band"] = None
    return out
