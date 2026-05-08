"""Per-tenant backup + restore.

Spec § "Backups".  This module ships the per-tenant slice; the
operator-side full-DB DR variant stays at /maintenance/export
(it pre-dates the multi-tenancy work and is the recovery hatch
for whole-deployment loss).

The backup is a zip containing:

* ``stash.db`` — a SQLite file with the schema and only this
  tenant's rows.
* ``uploads/{tenant_id}/<name>`` — every encrypted blob the
  tenant owns.
* ``manifest.json`` — version + tenant_id + row counts so a
  restore can sanity-check before clobbering anything.

Filtering strategy: ``src.backup(dst)`` then ``DELETE FROM …
WHERE tenant_id != ?`` on every owned table, plus a final
``VACUUM`` to reclaim the deleted pages.  This trades some
write amplification (we copy then prune) for absolute schema
fidelity — the spec explicitly biases toward "preserves schema
exactly" over "fastest export"; bias matches.

The encrypted blobs travel as-is, so a backup zip is *useless*
without the matching ``STASH_KEK``.  Mention this loudly in
the UI; when phase 8 ships per-tenant B2 upload, the backup
zip will be uploaded to a *separate* bucket from the KEK by
design (spec § "Backups · Off-site DR via Backblaze B2").
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from dao._base import Actor, NotFoundError, db, require_role


# Every table that holds tenant-owned data.  Order doesn't matter
# for the DELETE pass (we don't FK-cascade through this list), but
# keep it in line with spec § "Schema migrations to existing
# tables" so a future audit can match it row for row.
_TENANT_OWNED_TABLES = (
    "locations", "floors", "rooms",
    "boxes", "items",
    "tags", "item_tags", "pending_item_tags",
    "pending_items", "ingest_jobs",
    "tenant_members", "tenant_invites", "object_shares",
    "usage_events", "quotas", "audit_log",
)


_BACKUP_FORMAT_VERSION = 1


# ── DB path discovery ──────────────────────────────────────────────


def _live_db_path() -> Path:
    return Path(os.environ.get(
        "STASH_DB",
        Path(__file__).resolve().parent.parent / "stash.db",
    ))


def _live_uploads_root() -> Path:
    return Path(os.environ.get(
        "STASH_UPLOADS",
        Path(__file__).resolve().parent.parent / "uploads",
    ))


# ── Build ──────────────────────────────────────────────────────────


def build_tenant_zip(actor: Actor) -> tuple[bytes, dict]:
    """Build the per-tenant backup zip in memory.  Returns
    ``(zip_bytes, manifest)`` so the caller can both stream the bytes
    and audit-log the manifest summary.

    Maintainer-only.  The DAO does *not* compute checksums of the
    encrypted blobs (zip's CRC-32 already covers integrity inside
    the archive; the verification job in roadmap step 7 is the
    cross-DR signal).  Read-only members can't trigger backups —
    spec § "Roles · Operations matrix"."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError("no active tenant")
    tenant_id = actor.tenant_id

    # Drain the WAL so the zip captures every committed write.
    # In WAL mode, recent UPDATEs sit in the -wal sidecar until a
    # checkpoint promotes them into the main DB.
    with db() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # 1. Copy the live DB to a temp path, then prune to this tenant.
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="stash-backup-")
    os.close(fd)
    tmp = Path(tmp_path)
    try:
        src = sqlite3.connect(_live_db_path())
        try:
            dst = sqlite3.connect(tmp)
            try:
                # Schema-and-rows snapshot via online-backup API.
                src.backup(dst)
                dst.execute("PRAGMA foreign_keys = OFF")
                # Per-tenant filter: every owned table.
                row_counts: dict[str, int] = {}
                for table in _TENANT_OWNED_TABLES:
                    cur = dst.execute(
                        f"DELETE FROM {table} WHERE tenant_id != ?",
                        (tenant_id,),
                    )
                    # Counters are post-DELETE row totals (what the
                    # backup actually carries) so the manifest can
                    # be eyeballed against the live DB.
                    remain = dst.execute(
                        f"SELECT COUNT(*) FROM {table} "
                        f"WHERE tenant_id = ?",
                        (tenant_id,),
                    ).fetchone()[0]
                    row_counts[table] = remain
                # Tenants table: drop every row except ours.
                dst.execute(
                    "DELETE FROM tenants WHERE id != ?",
                    (tenant_id,),
                )
                row_counts["tenants"] = 1
                dst.commit()
                # VACUUM reclaims the pages we just freed; without
                # it the zip carries dead space.  VACUUM can't run
                # inside a transaction, so it goes after commit.
                dst.execute("VACUUM")
            finally:
                dst.close()
        finally:
            src.close()

        # 2. Build the zip.  DB first, then per-tenant uploads dir,
        # then the manifest at the end — that way a streaming reader
        # finds the heaviest payload first and the manifest only
        # gets parsed when integrity matters.
        manifest = {
            "format_version": _BACKUP_FORMAT_VERSION,
            "tenant_id": tenant_id,
            "tenant_name": _tenant_name(tenant_id),
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "exported_by": actor.email,
            "row_counts": row_counts,
            "stash_version": os.environ.get("STASH_VERSION", "dev"),
            "git_sha": os.environ.get("STASH_GIT_SHA", "")[:12],
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp, arcname="stash.db")
            uploads_root = _live_uploads_root() / str(tenant_id)
            file_count = 0
            file_bytes = 0
            if uploads_root.exists():
                for p in sorted(uploads_root.iterdir()):
                    if p.is_file():
                        zf.write(p, arcname=f"uploads/{tenant_id}/{p.name}")
                        file_count += 1
                        file_bytes += p.stat().st_size
            manifest["uploads_count"] = file_count
            manifest["uploads_bytes"] = file_bytes
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        zip_bytes = buf.getvalue()
        manifest["zip_bytes"] = len(zip_bytes)
        manifest["zip_sha256"] = hashlib.sha256(zip_bytes).hexdigest()
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass

    _audit_export(tenant_id, actor.email, manifest)
    return zip_bytes, manifest


def _tenant_name(tenant_id: int) -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT name FROM tenants WHERE id = ?",
            (tenant_id,),
        ).fetchone()
    return row["name"] if row else f"tenant-{tenant_id}"


def _audit_export(tenant_id: int, actor_email: str, manifest: dict) -> None:
    """Audit the export so an operator can later see who pulled a
    backup and when.  Metadata is the manifest minus the inner
    ``row_counts`` dict (those'd inflate the audit_log row) — keep
    just the headline numbers."""
    summary = {
        "format_version": manifest["format_version"],
        "uploads_count": manifest["uploads_count"],
        "uploads_bytes": manifest["uploads_bytes"],
        "zip_bytes": manifest["zip_bytes"],
        "zip_sha256": manifest["zip_sha256"],
    }
    with db() as conn:
        conn.execute(
            "INSERT INTO audit_log "
            "(tenant_id, actor_email, action, target_kind, target_id, "
            " metadata_json) "
            "VALUES (?, ?, 'backup.export', 'tenant', ?, ?)",
            (tenant_id, actor_email, tenant_id, json.dumps(summary)),
        )
        conn.commit()
