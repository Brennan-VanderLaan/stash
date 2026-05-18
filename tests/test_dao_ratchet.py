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
# Bumped 69 → 70 on 2026-05-17 for the case-insensitive room-name
# match in ``POST /queue/{pending_id}/create-suggested-box`` (the
# feedback-burndown #50 fix).  The lookup is a single SELECT that
# resolves a free-text ``suggested_new_box_location`` to an
# existing room id when one matches by name; the caller then either
# passes the resolved id into dao_rooms or falls back to the plain
# text.  Lives in app.py because it's an AI-suggestion concern, not
# a tenant-CRUD operation — there's no general "rooms.get by case-
# insensitive name" use case elsewhere to justify a DAO helper.
# Bumped 70 → 71 on 2026-05-18 for the migrate_db backfill of
# ``tenants.billing_owner_email`` (feedback #72).  Schema
# migrations are an explicit legitimate reason per the docstring
# above — this is a one-shot UPDATE that runs at boot to give
# pre-existing Pro tenants a billing owner (oldest maintainer by
# joined_at).  Pure migration code, lives in migrate_db, can't
# meaningfully be DAO-ified.
# Bumped 71 → 75 on 2026-05-18 for the marketing-analytics
# schema migrations: 2 CREATE TABLE + 2 CREATE INDEX statements
# in migrate_db that set up ``marketing_sessions`` +
# ``marketing_events``.  All four are pure schema; the runtime
# event-write surface lives in dao/marketing.py.
APP_CONN_EXECUTE_CEILING = 75  # +4 — marketing tables + indexes


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
