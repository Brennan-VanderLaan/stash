"""Per-tenant quota enforcement (phase 10).

Spec § "Telemetry & quotas".  Three caps per tenant:

* ``monthly_ai_calls`` — total AI surface calls in the calendar
  month (Gemini detect, Gemini art, Anthropic match).
* ``monthly_upload_bytes`` — total post-encode bytes written to
  the tenant's upload directory in the calendar month.
* ``daily_ai_cost_micros`` — *daily* hard ceiling on AI cost in
  micro-dollars.  Separate from the monthly call count because a
  single Gemini-art call costs ~30x a detect call; an MCP agent
  on a runaway loop hits a daily cost ceiling much faster than
  it'd hit a monthly call ceiling.

Cap resolution per tenant:

1. Per-tenant overrides from the ``quotas`` table win.
2. Plan defaults from :data:`_PLAN_DEFAULTS` fill in unset
   override fields.

Soft cap behaviour at request time (the route's job, not this
module's):

* < 80% of cap → no signal.
* 80–99% → ``X-Quota-Warning`` response header.
* ≥ 100% → ``HTTPException(429)`` from ``check_or_raise``.

Spec § "Telemetry & quotas" explicitly accepts the race between
counter writes and cap checks: we'd rather over-serve by a few
requests than block a write that already started.  Implementation
matches.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

import obs
from dao._base import Actor, db, require_role


_log = obs.get_logger("dao.quotas")


# Plan defaults.  Numbers are operator-visible estimates — the spec
# is explicit that real billing tightens these later (phase 13).
# All values in the unit the corresponding column expects.
_PLAN_DEFAULTS = {
    "free": {
        "monthly_ai_calls": 1_000,
        "monthly_upload_bytes": 5 * 1024 * 1024 * 1024,    # 5 GB
        "daily_ai_cost_micros": 1_000_000,                  # $1
    },
    "pro": {
        "monthly_ai_calls": 50_000,
        "monthly_upload_bytes": 100 * 1024 * 1024 * 1024,  # 100 GB
        "daily_ai_cost_micros": 50_000_000,                 # $50
    },
}


# ── Window helpers ──────────────────────────────────────────────────


# SQLite's ``CURRENT_TIMESTAMP`` produces ``YYYY-MM-DD HH:MM:SS`` —
# space separator, no timezone suffix.  We compare timestamps as
# strings (no datetime() casting) so the python-side strings need to
# use the same shape; otherwise lexical compare flips within the
# same date prefix (` ` < `T`).  ``_format_window`` produces matching
# output.


def _format_window(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _month_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return _format_window(
        now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
    )


def _day_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return _format_window(
        now.replace(hour=0, minute=0, second=0, microsecond=0),
    )


# ── Cap resolution ──────────────────────────────────────────────────


def get_caps(tenant_id: int) -> dict:
    """Resolve effective caps for a tenant: plan defaults
    overridden by per-tenant rows from the ``quotas`` table.

    Returns a dict with the full set of cap keys — None means
    "no cap on this surface" (operator override or explicit
    plan setting)."""
    if tenant_id is None:
        return _PLAN_DEFAULTS["free"].copy()
    with db() as conn:
        plan_row = conn.execute(
            "SELECT plan FROM tenants WHERE id = ?",
            (tenant_id,),
        ).fetchone()
        plan = plan_row["plan"] if plan_row else "free"
        defaults = _PLAN_DEFAULTS.get(plan, _PLAN_DEFAULTS["free"]).copy()
        override_row = conn.execute(
            "SELECT monthly_ai_calls, monthly_upload_bytes, "
            "       backup_storage_bytes, overrides_json "
            "FROM quotas WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
    if override_row is not None:
        for col in ("monthly_ai_calls", "monthly_upload_bytes"):
            v = override_row[col]
            if v is not None:
                defaults[col] = v
        # ``daily_ai_cost_micros`` lives in the JSON blob since it
        # arrived after the original schema; pull it from there.
        if override_row["overrides_json"]:
            try:
                blob = json.loads(override_row["overrides_json"])
                if "daily_ai_cost_micros" in blob:
                    defaults["daily_ai_cost_micros"] = (
                        blob["daily_ai_cost_micros"]
                    )
            except (ValueError, TypeError):
                pass
    return defaults


# ── Usage readback (for soft warnings + /usage meters) ─────────────


def usage_for_tenant(tenant_id: int) -> dict:
    """Per-surface usage in the current windows — month for
    AI/upload, day for AI cost.  Mirrors the cap dict's keys so
    the soft-warning math is a straight per-key compare."""
    if tenant_id is None:
        return {
            "monthly_ai_calls": 0,
            "monthly_upload_bytes": 0,
            "daily_ai_cost_micros": 0,
        }
    month = _month_start_iso()
    day = _day_start_iso()
    with db() as conn:
        ai_calls = conn.execute(
            "SELECT COALESCE(SUM(units), 0) AS n FROM usage_events "
            "WHERE tenant_id = ? AND surface = 'ai' "
            "  AND created_at >= ?",
            (tenant_id, month),
        ).fetchone()["n"]
        upload_bytes = conn.execute(
            "SELECT COALESCE(SUM(units), 0) AS n FROM usage_events "
            "WHERE tenant_id = ? AND surface = 'upload' "
            "  AND created_at >= ?",
            (tenant_id, month),
        ).fetchone()["n"]
        ai_cost_today = conn.execute(
            "SELECT COALESCE(SUM(cost_micros), 0) AS n FROM usage_events "
            "WHERE tenant_id = ? AND surface = 'ai' "
            "  AND created_at >= ?",
            (tenant_id, day),
        ).fetchone()["n"]
    return {
        "monthly_ai_calls": int(ai_calls),
        "monthly_upload_bytes": int(upload_bytes),
        "daily_ai_cost_micros": int(ai_cost_today),
    }


def percent(used: int, cap: Optional[int]) -> int:
    """Helper: clamp 0..200 (over-cap shows the overshoot)."""
    if not cap or cap <= 0:
        return 0
    return min(200, int((used / cap) * 100))


def warning_band(used: int, cap: Optional[int]) -> Optional[str]:
    """Return ``"warning"`` for 80-99%, ``"exceeded"`` for ≥ 100%,
    None otherwise.  The route layer translates these to the
    X-Quota-Warning header + 429."""
    if not cap or cap <= 0:
        return None
    p = (used / cap) * 100
    if p >= 100:
        return "exceeded"
    if p >= 80:
        return "warning"
    return None


# ── Enforcement ─────────────────────────────────────────────────────


class QuotaExceeded(Exception):
    """Raised by :func:`check_or_raise` when a request would land
    a usage event that pushes the tenant over a cap.  The route
    layer translates to ``HTTPException(429)`` with the surface
    name + reset window in the response body."""

    def __init__(self, surface: str, key: str, used: int, cap: int) -> None:
        super().__init__(
            f"{surface} quota exceeded: {key}={used} > {cap}"
        )
        self.surface = surface
        self.key = key
        self.used = used
        self.cap = cap


def check_or_raise(
    tenant_id: int | None,
    surface: str,
    *,
    units_about_to_record: int = 1,
    cost_about_to_record: int = 0,
) -> None:
    """Pre-flight quota check.  Call from the route layer *before*
    the expensive operation (Gemini call, photo encode, B2
    upload).  Raises :class:`QuotaExceeded` when the about-to-be-
    recorded units would push the tenant over a cap.

    The "about to record" args let the caller include the cost of
    the in-flight op so a request that wouldn't *individually*
    exceed but *cumulatively* would, gets caught.

    ``tenant_id=None`` is a no-op — background workers without an
    active tenant context (operator B2 verification, etc.) can
    call freely.  The corresponding usage_events row will also be
    a no-op."""
    if tenant_id is None:
        return
    caps = get_caps(tenant_id)
    used = usage_for_tenant(tenant_id)
    if surface == "ai":
        # Total monthly call count.
        cap = caps.get("monthly_ai_calls")
        if cap is not None and used["monthly_ai_calls"] + units_about_to_record > cap:
            raise QuotaExceeded(
                "ai", "monthly_ai_calls",
                used["monthly_ai_calls"] + units_about_to_record, cap,
            )
        # Daily cost ceiling (the runaway-MCP guard).
        cap = caps.get("daily_ai_cost_micros")
        if cap is not None and used["daily_ai_cost_micros"] + cost_about_to_record > cap:
            raise QuotaExceeded(
                "ai", "daily_ai_cost_micros",
                used["daily_ai_cost_micros"] + cost_about_to_record, cap,
            )
    elif surface == "upload":
        cap = caps.get("monthly_upload_bytes")
        if cap is not None and used["monthly_upload_bytes"] + units_about_to_record > cap:
            raise QuotaExceeded(
                "upload", "monthly_upload_bytes",
                used["monthly_upload_bytes"] + units_about_to_record, cap,
            )
    # core / backup surfaces are uncapped today.


# ── Tenant-creation throttle ────────────────────────────────────────


# Per-IP cap on POST /admin/tenants — defends against a stolen
# operator credential being used to mass-mint tenants in a
# scripted run.  Default 5/hour matches spec § "Anti-abuse".
TENANT_CREATION_PER_HOUR = int(
    os.environ.get("STASH_TENANT_CREATION_PER_HOUR", "5"),
)


def _hour_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return _format_window(
        now.replace(minute=0, second=0, microsecond=0),
    )


def check_tenant_creation_rate(client_ip: str) -> None:
    """Raise :class:`QuotaExceeded` if the IP has minted more than
    ``TENANT_CREATION_PER_HOUR`` tenants in the current hour.
    Driven from audit_log (we already record ``tenant.create``);
    no additional table needed.

    IPs not present in the metadata fall into a single ``unknown``
    bucket so a missing X-Forwarded-For doesn't let an attacker
    bypass by stripping the header."""
    if not client_ip:
        client_ip = "unknown"
    since = _hour_start_iso()
    with db() as conn:
        # SQLite's json_extract fishes the IP out of the metadata
        # blob without parsing on the Python side.
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_log "
            "WHERE action = 'tenant.create' "
            "  AND created_at >= ? "
            "  AND COALESCE("
            "        json_extract(metadata_json, '$.ip'), 'unknown'"
            "      ) = ?",
            (since, client_ip),
        ).fetchone()
    if row["n"] >= TENANT_CREATION_PER_HOUR:
        raise QuotaExceeded(
            "tenant", "creation_rate",
            int(row["n"]), TENANT_CREATION_PER_HOUR,
        )


# ── Override editor (operator surface) ──────────────────────────────


def set_overrides(
    actor: Actor,
    tenant_id: int,
    *,
    monthly_ai_calls: Optional[int] = None,
    monthly_upload_bytes: Optional[int] = None,
    daily_ai_cost_micros: Optional[int] = None,
) -> None:
    """Operator-driven per-tenant cap override.  ``None`` for a
    field means "leave the existing override alone"; pass an
    explicit ``-1`` to *unset* an override (reverting to the plan
    default).  Fields not present in the schema columns
    (``daily_ai_cost_micros``) live in the ``overrides_json``
    blob — same behavior."""
    from dao._base import require_operator
    require_operator(actor)

    with db() as conn:
        existing = conn.execute(
            "SELECT monthly_ai_calls, monthly_upload_bytes, "
            "       backup_storage_bytes, overrides_json "
            "FROM quotas WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()

        new_ai = existing["monthly_ai_calls"] if existing else None
        new_up = existing["monthly_upload_bytes"] if existing else None
        new_backup = existing["backup_storage_bytes"] if existing else None
        try:
            blob = json.loads(existing["overrides_json"]) if (
                existing and existing["overrides_json"]
            ) else {}
        except (ValueError, TypeError):
            blob = {}

        if monthly_ai_calls is not None:
            new_ai = None if monthly_ai_calls < 0 else monthly_ai_calls
        if monthly_upload_bytes is not None:
            new_up = None if monthly_upload_bytes < 0 else monthly_upload_bytes
        if daily_ai_cost_micros is not None:
            if daily_ai_cost_micros < 0:
                blob.pop("daily_ai_cost_micros", None)
            else:
                blob["daily_ai_cost_micros"] = daily_ai_cost_micros

        json_blob = json.dumps(blob) if blob else None
        if existing is None:
            conn.execute(
                "INSERT INTO quotas "
                "(tenant_id, monthly_ai_calls, monthly_upload_bytes, "
                " backup_storage_bytes, overrides_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (tenant_id, new_ai, new_up, new_backup, json_blob),
            )
        else:
            conn.execute(
                "UPDATE quotas SET monthly_ai_calls = ?, "
                "  monthly_upload_bytes = ?, backup_storage_bytes = ?, "
                "  overrides_json = ? "
                "WHERE tenant_id = ?",
                (new_ai, new_up, new_backup, json_blob, tenant_id),
            )

        obs.write_audit(
            conn,
            tenant_id=tenant_id,
            actor_email=actor.email,
            action="quota.override",
            target_kind="tenant",
            target_id=tenant_id,
            metadata={
                "monthly_ai_calls": new_ai,
                "monthly_upload_bytes": new_up,
                "daily_ai_cost_micros":
                    blob.get("daily_ai_cost_micros"),
            },
        )
        conn.commit()
    _log.warning("quota.override tenant_id=%s by=%s", tenant_id, actor.email)
