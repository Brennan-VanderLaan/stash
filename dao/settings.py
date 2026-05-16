"""Operator-tunable deployment settings.

Key-value store for values that should be editable from /admin
without a code deploy or env-var change.  Today's load-bearing
entry: ``free_tier_bytes_total`` — the disk allocation the
operator has carved out for free tenants.  When the operator
scales the underlying EBS volume up, they bump this value from
the admin tab and the platform absorbs the extra capacity as
"more free signups available now."

Design notes
~~~~~~~~~~~~

* Values are stored as TEXT (with explicit ``get_int`` /
  ``get_str`` accessors) so the schema doesn't bake a type per
  setting.  Adding a new tunable is one new key + one default,
  no migration.
* All writes are operator-only and audit-logged via
  ``settings.change`` so a future operator can trace who bumped
  the free pool when.
* ``get_int(key, default=...)`` returns the default for missing
  / malformed values so the caller never has to defend against
  a broken row.
"""
from __future__ import annotations

from typing import Optional

import obs
from dao._base import Actor, db, require_operator


_log = obs.get_logger("dao.settings")


# Default tuning values.  Pulled at first-read time if a key
# hasn't been explicitly set yet; also serves as a hint to future
# contributors about what each key controls.
DEFAULTS: dict[str, str] = {
    # Total disk bytes carved out for the free tier.  Per-tenant
    # cap is _PLAN_DEFAULTS["free"]["storage_bytes"] (100 MB);
    # slots = total // per-tenant cap.  10 GB default fits ~100
    # free users on a small deployment.
    "free_tier_bytes_total": str(10 * 1024 * 1024 * 1024),
}


def get(key: str) -> Optional[str]:
    """Raw string read.  Returns None if the key has never been
    set — callers usually want :func:`get_str` or :func:`get_int`
    with an explicit default instead."""
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM deployment_settings WHERE key = ?",
            (key,),
        ).fetchone()
    return row["value"] if row else None


def get_str(key: str, default: str | None = None) -> str | None:
    """String read with explicit default + DEFAULTS-table fallback."""
    raw = get(key)
    if raw is not None:
        return raw
    if default is not None:
        return default
    return DEFAULTS.get(key)


def get_int(key: str, default: int | None = None) -> int:
    """Integer read.  Falls back through: stored value → caller's
    default → :data:`DEFAULTS` table → 0.  Returns int — defends
    against malformed stored strings by reverting to the
    fallback chain instead of raising."""
    raw = get(key)
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    if default is not None:
        return default
    fallback = DEFAULTS.get(key)
    if fallback is not None:
        try:
            return int(fallback)
        except (TypeError, ValueError):
            pass
    return 0


def set_value(actor: Actor, key: str, value: str) -> None:
    """Operator-only write.  Upserts the row and audit-logs
    ``settings.change`` with the new value + the previous one so
    a future operator scanning the audit history can reconstruct
    exactly what changed when."""
    require_operator(actor)
    prev = get(key)
    with db() as conn:
        conn.execute(
            "INSERT INTO deployment_settings "
            "(key, value, updated_at, updated_by_email) "
            "VALUES (?, ?, CURRENT_TIMESTAMP, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  value = excluded.value, "
            "  updated_at = excluded.updated_at, "
            "  updated_by_email = excluded.updated_by_email",
            (key, value, actor.email),
        )
        obs.write_audit(
            conn,
            tenant_id=None,
            actor_email=actor.email,
            action="settings.change",
            target_kind="setting",
            target_id=None,
            metadata={"key": key, "value": value, "previous": prev},
        )
        conn.commit()
    _log.info("settings.change key=%s value=%s by=%s",
              key, value, actor.email)
