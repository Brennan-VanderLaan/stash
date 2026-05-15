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

import obs
from dao._base import Actor, NotFoundError, db, require_role


_log = obs.get_logger("dao.backups")


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


def build_gdpr_zip(actor: Actor) -> tuple[bytes, dict]:
    """GDPR Article 20 portability bundle for the actor's tenant.

    Same DB shape as :func:`build_tenant_zip` (per-tenant filtered
    SQLite snapshot), but photos are *decrypted* into the zip so the
    user can read their data without ``STASH_KEK``.  Article 20
    requires "structured, commonly used, machine-readable" — the
    SQLite + JPEGs + manifest combo satisfies that.

    Includes a top-level ``README.md`` that names every artefact
    and explains the format in plain language, because the
    downloader is the data subject, not an engineer.

    Maintainer-only — same role gate as the encrypted backup."""
    import vault as _vault
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError("no active tenant")
    tenant_id = actor.tenant_id

    with db() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="stash-gdpr-")
    os.close(fd)
    tmp = Path(tmp_path)
    try:
        src = sqlite3.connect(_live_db_path())
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
                dst.execute("PRAGMA foreign_keys = OFF")
                row_counts: dict[str, int] = {}
                for table in _TENANT_OWNED_TABLES:
                    dst.execute(
                        f"DELETE FROM {table} WHERE tenant_id != ?",
                        (tenant_id,),
                    )
                    remain = dst.execute(
                        f"SELECT COUNT(*) FROM {table} "
                        f"WHERE tenant_id = ?",
                        (tenant_id,),
                    ).fetchone()[0]
                    row_counts[table] = remain
                dst.execute(
                    "DELETE FROM tenants WHERE id != ?", (tenant_id,),
                )
                row_counts["tenants"] = 1
                dst.commit()
                dst.execute("VACUUM")
            finally:
                dst.close()
        finally:
            src.close()

        kek = _vault.get_kek()
        manifest = {
            "format_version": _BACKUP_FORMAT_VERSION,
            "bundle_kind": "gdpr_portability",
            "gdpr_article": "Article 20 — Right to data portability",
            "tenant_id": tenant_id,
            "tenant_name": _tenant_name(tenant_id),
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "exported_by": actor.email,
            "row_counts": row_counts,
            "stash_version": os.environ.get("STASH_VERSION", "dev"),
            "git_sha": os.environ.get("STASH_GIT_SHA", "")[:12],
            "notes": (
                "Photos are decrypted into uploads/.  The SQLite file "
                "uses standard schema; open with `sqlite3 stash.db` or "
                "any DB browser.  See README.md for the full layout."
            ),
        }
        readme = _gdpr_readme(manifest)

        buf = io.BytesIO()
        decrypt_errors = 0
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("README.md", readme)
            zf.write(tmp, arcname="stash.db")
            uploads_root = _live_uploads_root() / str(tenant_id)
            file_count = 0
            file_bytes = 0
            if uploads_root.exists():
                with db() as conn:
                    for p in sorted(uploads_root.iterdir()):
                        if not p.is_file():
                            continue
                        try:
                            plaintext = _vault.decrypt_for_tenant(
                                conn, tenant_id, kek, p.read_bytes(),
                            )
                        except Exception:
                            # A blob we can't decrypt (rotated KEK,
                            # bit-rot) isn't fatal — we record the
                            # count + skip it so the user knows
                            # something didn't make it.
                            decrypt_errors += 1
                            continue
                        zf.writestr(
                            f"uploads/{p.name}", plaintext,
                        )
                        file_count += 1
                        file_bytes += len(plaintext)
            manifest["uploads_count"] = file_count
            manifest["uploads_bytes"] = file_bytes
            manifest["decrypt_errors"] = decrypt_errors
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        zip_bytes = buf.getvalue()
        manifest["zip_bytes"] = len(zip_bytes)
        manifest["zip_sha256"] = hashlib.sha256(zip_bytes).hexdigest()
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass

    _audit_gdpr_export(tenant_id, actor.email, manifest)
    return zip_bytes, manifest


def _gdpr_readme(manifest: dict) -> str:
    """Plain-language README explaining the bundle's contents to a
    non-engineer data subject."""
    return f"""# Your Stash data export

This zip is your personal copy of everything Stash has stored about
your tenant, exported under **GDPR Article 20 (Right to data
portability)**.

## Tenant
- Name: {manifest['tenant_name']}
- Tenant ID: {manifest['tenant_id']}
- Exported by: {manifest['exported_by']}
- Exported at (UTC): {manifest['exported_at']}

## Contents
- `stash.db` — SQLite database with every row in your tenant: boxes,
  items, rooms, locations, tags, shares, audit log, etc.  Open it
  with the `sqlite3` command-line tool or any free DB browser
  (e.g. https://sqlitebrowser.org/).  Schemas are standard — column
  names match what you see in the Stash UI.
- `uploads/` — every photo + thumbnail you've uploaded, **decrypted**
  to standard JPEG.  Filenames match the values in the `items.photo`
  / `items.thumb` columns.
- `manifest.json` — machine-readable summary (row counts, byte
  totals, SHA-256 of this archive).
- `README.md` — this file.

## What's NOT in here
- Other tenants' data (this export is scoped to your tenant only).
- Operator-level audit logs (those track platform-wide events and
  belong to the operator, not you).
- Encrypted-at-rest cipher material (the KEK).  You don't need it;
  photos are already decrypted in `uploads/`.

## Row counts in this bundle
""" + "\n".join(
        f"- `{table}`: {count}" for table, count in manifest["row_counts"].items()
    ) + f"""

## Notes
- {manifest['notes']}
- Stash version at export: {manifest['stash_version']}
- Git SHA at export: {manifest['git_sha'] or 'unknown'}
"""


def _audit_gdpr_export(tenant_id: int, actor_email: str, manifest: dict) -> None:
    summary = {
        "format_version": manifest["format_version"],
        "bundle_kind": "gdpr_portability",
        "uploads_count": manifest["uploads_count"],
        "uploads_bytes": manifest["uploads_bytes"],
        "decrypt_errors": manifest["decrypt_errors"],
        "zip_bytes": manifest["zip_bytes"],
        "zip_sha256": manifest["zip_sha256"],
    }
    with db() as conn:
        obs.write_audit(
            conn,
            tenant_id=tenant_id,
            actor_email=actor_email,
            action="gdpr.export",
            target_kind="tenant",
            target_id=tenant_id,
            metadata=summary,
        )
        conn.commit()
    _log.info(
        "gdpr.export tenant_id=%s sha256=%s bytes=%d files=%d",
        tenant_id, manifest["zip_sha256"], manifest["zip_bytes"],
        manifest["uploads_count"],
    )


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
        obs.write_audit(
            conn,
            tenant_id=tenant_id,
            actor_email=actor_email,
            action="backup.export",
            target_kind="tenant",
            target_id=tenant_id,
            metadata=summary,
        )
        conn.commit()
    _log.info(
        "backup.export tenant_id=%s sha256=%s bytes=%d",
        tenant_id, manifest["zip_sha256"], manifest["zip_bytes"],
    )


# ── B2 (S3-compatible) upload ───────────────────────────────────────


# Spec § "Backups · Off-site DR via Backblaze B2" — phase 8.
#
# Configuration lives in the standard env-var quartet:
#   B2_KEY_ID, B2_APPLICATION_KEY, B2_ENDPOINT, B2_BUCKET
# Plus the spec's hard rule: ``KEK lives in a *separate* bucket
# (and ideally vendor) than the data``.  This module never reads
# STASH_KEK — only the encrypted blob.  The KEK travels through
# a separate operator-managed channel.
#
# Tonight we ship the upload helper + a manual /admin trigger.
# Scheduled nightly runs land with the cron/scheduler decision
# (deferred — see roadmap markers in spec.md).


class B2NotConfiguredError(RuntimeError):
    """Raised when the B2 env vars aren't set.  Routes catch this
    and surface "configure B2 first" to the operator instead of
    500'ing on a missing credential."""


def _b2_config() -> dict[str, str]:
    """Read the B2 quartet from env, raise if any are missing.
    Returns a dict suitable for ``boto3.client('s3', ...)``.

    The endpoint URL is a full URL like
    ``https://s3.us-west-002.backblazeb2.com`` — B2 is region-
    keyed and the account dashboard exposes the right one."""
    cfg = {}
    for var in ("B2_KEY_ID", "B2_APPLICATION_KEY",
                "B2_ENDPOINT", "B2_BUCKET"):
        val = os.environ.get(var, "").strip()
        if not val:
            raise B2NotConfiguredError(
                f"B2 backup is not configured: missing {var}.  "
                "See deploy/.env.example for the env-var quartet."
            )
        cfg[var] = val
    return cfg


def _make_b2_client():
    """Build a boto3 S3 client pointed at B2.  Lazy-imported so the
    rest of the module stays usable when boto3 isn't installed
    (local test runs)."""
    cfg = _b2_config()
    import boto3  # noqa: PLC0415 — intentional lazy import
    return boto3.client(
        "s3",
        endpoint_url=cfg["B2_ENDPOINT"],
        aws_access_key_id=cfg["B2_KEY_ID"],
        aws_secret_access_key=cfg["B2_APPLICATION_KEY"],
    )


# Test seam: tests substitute this to skip the real boto3 import.
# The route always calls through ``_make_b2_client`` which the test
# fixture monkeypatches.
_B2_CLIENT_FACTORY = _make_b2_client


def upload_tenant_to_b2(actor: Actor) -> dict:
    """Build the per-tenant zip and upload it to B2 keyed at
    ``s3://<bucket>/<tenant_id>/<YYYY-MM-DD>.zip``.  Returns a
    dict suitable for the audit-log payload + the route's redirect
    flash:

    * ``key`` — the S3 key the object landed at.
    * ``bucket`` — the B2 bucket name.
    * ``size`` — uploaded byte count.
    * ``sha256`` — sha256 of the zip (matches the X-Backup-Sha256
      header from the download path).

    Maintainer-only.  Operators trigger this surface via /admin
    for any tenant; that path threads through a synthetic
    maintainer-equivalent actor — see app.py."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError("no active tenant")
    cfg = _b2_config()  # raises early if mis-configured

    zip_bytes, manifest = build_tenant_zip(actor)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"{actor.tenant_id}/{today}.zip"

    client = _B2_CLIENT_FACTORY()
    client.put_object(
        Bucket=cfg["B2_BUCKET"],
        Key=key,
        Body=zip_bytes,
        ContentType="application/zip",
        Metadata={
            "stash-format-version": str(manifest["format_version"]),
            "stash-tenant-id": str(actor.tenant_id),
            "stash-zip-sha256": manifest["zip_sha256"],
            "stash-exported-by": actor.email,
        },
    )

    summary = {
        "bucket": cfg["B2_BUCKET"],
        "key": key,
        "size": len(zip_bytes),
        "sha256": manifest["zip_sha256"],
    }
    with db() as conn:
        obs.write_audit(
            conn,
            tenant_id=actor.tenant_id,
            actor_email=actor.email,
            action="backup.b2_upload",
            target_kind="tenant",
            target_id=actor.tenant_id,
            metadata=summary,
        )
        conn.commit()
    _log.info(
        "backup.b2_upload bucket=%s key=%s bytes=%d",
        cfg["B2_BUCKET"], key, len(zip_bytes),
    )
    # Backup-bytes telemetry for the /usage meter.
    from dao import usage as dao_usage
    dao_usage.record(
        actor.tenant_id, "backup", "backup_bytes",
        units=len(zip_bytes),
    )
    return summary


def upload_tenant_to_b2_as_operator(operator_email: str, tenant_id: int) -> dict:
    """Operator-driven variant for the manual /admin trigger.
    Doesn't require an Actor since the operator isn't a tenant
    member — synthesises a one-shot maintainer-shaped Actor for
    the underlying ``upload_tenant_to_b2`` call so the role gate
    + audit trail still record consistently."""
    op_actor = Actor(
        email=operator_email,
        tenant_id=tenant_id,
        role="maintainer",  # synthetic, scoped to this call only
        is_operator=True,
        memberships=((tenant_id, "maintainer"),),
        shares=(),
    )
    return upload_tenant_to_b2(op_actor)
