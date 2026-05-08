"""Audit-log read surface (operator-only).

Every DAO mutation writes through ``obs.write_audit`` so the
audit_log table is the canonical "what happened" stream across
the whole deployment.  The /admin recent-activity feed reads
the tail of that stream so an operator can spot anomalies
(e.g. burst of share.creates from a single email, mass
oauth.token.issue for one client) at a glance.

This module is read-only — writes are still owned by
``obs.write_audit``.  Read filtering stays narrow: an operator
can see everything; tenant-scoped reads (a maintainer reading
their own tenant's audit log) land later when phase 12's
audit-log read view ships.
"""

from __future__ import annotations

from typing import Optional

from dao._base import Actor, db, require_operator


def list_recent_for_operator(
    actor: Actor,
    *,
    limit: int = 50,
    action_prefix: Optional[str] = None,
    tenant_id: Optional[int] = None,
) -> list[dict]:
    """Most recent audit-log entries.  Operator-only.  Joined to
    ``tenants.name`` so the UI can render the tenant column
    without a follow-up fetch.  ``tenant_id IS NULL`` rows
    (cross-tenant operator actions like ``oauth.client.register``)
    surface as ``tenant_name = None`` and the row stays visible.

    Filters:

    * ``action_prefix`` — substring match against ``action``
      (e.g. ``"share."`` for every share-related event).
    * ``tenant_id`` — restrict to one tenant.

    ``limit`` is clamped to [1, 500] so a malformed UI input
    can't pull the whole table."""
    require_operator(actor)
    limit = max(1, min(500, int(limit)))
    clauses = ["1=1"]
    params: list = []
    if action_prefix:
        clauses.append("a.action LIKE ?")
        params.append(action_prefix + "%")
    if tenant_id is not None:
        clauses.append("a.tenant_id = ?")
        params.append(int(tenant_id))
    where = " AND ".join(clauses)
    with db() as conn:
        rows = conn.execute(
            f"SELECT a.id, a.tenant_id, a.actor_email, a.action, "
            f"       a.target_kind, a.target_id, a.metadata_json, "
            f"       a.created_at, t.name AS tenant_name "
            f"FROM audit_log a "
            f"LEFT JOIN tenants t ON t.id = a.tenant_id "
            f"WHERE {where} "
            f"ORDER BY a.id DESC "
            f"LIMIT ?",
            [*params, limit],
        ).fetchall()
    return [dict(r) for r in rows]
