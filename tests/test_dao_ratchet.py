"""Phase 3 outcome guard: keep app.py from regrowing inline conn.execute
call sites.

The DAO migration moved tenant-scoped queries into ``dao/`` modules and
left a small set of legitimate inline call sites in app.py:

* PRAGMAs (WAL/foreign_keys/integrity_check)
* schema setup + idempotent migrations (init_db / migrate_db)
* the bootstrap-tenancy actor middleware
* the orphan-detection + tenant-resolution file helpers
  (``_photo_still_referenced``, ``_resolve_tenant_for_filename``)
* ``_referenced_uploads`` for backup/cleanup walks
* ``submit_audit`` (a single multi-table transaction with several
  writes; safer to keep as one ``with db()`` block than to split)
* the ``location_detail`` floorplan-mosaic queries (multi-join with
  per-room bucketing — kept inline but explicitly tenant-scoped)

Anything *new* should land in ``dao/`` so role gates + tenant
filtering are uniform and testable.  This test fails if app.py's
``conn.execute(`` count goes up, so a future change is forced to
either (a) put new SQL in a DAO module, or (b) bump the ratchet
ceiling here with a comment explaining why the new inline site is
worth carrying."""

from __future__ import annotations

from pathlib import Path


# Ceiling at the moment the ratchet was installed.  Going UP requires
# a deliberate change here + a justification in the commit; going
# DOWN is welcome — drop the ceiling alongside the migration.
# Bumped from 66 → 67 on 2026-05-15 for the ``tour_seen`` table
# DDL + index in init_db (onboarding-tour persistence).  Schema
# migrations are explicitly called out in the ratchet docstring as
# a legit reason to lift the ceiling.
APP_CONN_EXECUTE_CEILING = 68  # +1 — migration: ALTER quotas rename column


def test_app_py_conn_execute_ratchet():
    src = Path(__file__).resolve().parent.parent / "app.py"
    text = src.read_text(encoding="utf-8")
    count = text.count("conn.execute(")
    assert count <= APP_CONN_EXECUTE_CEILING, (
        f"app.py inline conn.execute count rose to {count} "
        f"(ceiling: {APP_CONN_EXECUTE_CEILING}).  New tenant-scoped "
        f"queries belong in dao/ so role gates and tenant filtering "
        f"stay uniform.  If the new site is genuinely justified "
        f"(schema migration, file helpers, etc.), bump the ceiling "
        f"in tests/test_dao_ratchet.py and explain why in the commit."
    )


def test_no_conn_execute_outside_app_or_dao():
    """Outside of the explicit allow-list, no module should hold raw
    conn.execute calls — they all belong in DAO modules so role gates
    and tenant filtering stay uniform.

    Allow-list:
    * ``app.py`` — schema setup, file helpers, audit transaction.
    * ``dao/`` — by definition.
    * ``vault.py`` — per-tenant DEK plumbing for encryption-at-rest;
      operates pre-DAO and on its own ``tenant_dek`` table.
    * ``obs.py`` — the canonical ``write_audit`` helper.  Cross-cuts
      every DAO module; lives outside ``dao/`` because it's an
      observability concern, not a tenant-scoped query.
    * ``tests/`` — fixtures sometimes seed tables directly.
    """
    repo = Path(__file__).resolve().parent.parent
    allowed_top = {"app.py", "dao", "tests", ".venv", "venv",
                   "vault.py", "obs.py"}
    bad: list[str] = []
    for py in repo.rglob("*.py"):
        rel = py.relative_to(repo)
        parts = rel.parts
        if parts[0] in allowed_top:
            continue
        if "site-packages" in parts:
            continue
        text = py.read_text(encoding="utf-8", errors="ignore")
        if "conn.execute(" in text:
            bad.append(str(rel))
    assert not bad, (
        "These modules hold raw conn.execute calls — move them into a "
        f"dao/ module so they go through the actor + tenant gating: {bad}"
    )
