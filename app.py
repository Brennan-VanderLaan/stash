import os
import sqlite3
import secrets
from pathlib import Path
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import labels
import vault
import vision
from dao import Actor, ConflictError, ForbiddenError, NotFoundError
from dao import boxes as dao_boxes
from dao import floors as dao_floors
from dao import ingest_jobs as dao_ingest_jobs
from dao import items as dao_items
from dao import locations as dao_locations
from dao import pending_items as dao_pending
from dao import rooms as dao_rooms
from dao import tags as dao_tags
from dao import tenants as dao_tenants

load_dotenv()

ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get("STASH_DB", ROOT / "stash.db"))
UPLOAD_DIR = Path(os.environ.get("STASH_UPLOADS", ROOT / "uploads"))
UPLOAD_DIR.mkdir(exist_ok=True)
(ROOT / "static").mkdir(exist_ok=True)
(ROOT / "templates").mkdir(exist_ok=True)

VERSION = os.environ.get("STASH_VERSION", "dev")
GIT_SHA = os.environ.get("STASH_GIT_SHA", "")
WATCHTOWER_URL = os.environ.get("WATCHTOWER_URL", "").rstrip("/")
WATCHTOWER_TOKEN = os.environ.get("WATCHTOWER_TOKEN", "")
# At-rest encryption key.  Loaded once at import so a misconfigured or
# missing key fails fast instead of corrupting on first write.  The KEK
# wraps every tenant's DEK; losing it is total data loss, so it lives
# in env (not the DB or the uploads directory) and gets backed up to a
# different bucket than the data.  See vault.py + spec § "Encryption
# at rest".
_KEK = vault.get_kek()
# Public-facing base URL for QR codes on printed labels. Set in production
# (deploy/.env via the compose stack) to e.g. https://stash.example.com.
# Empty in local dev — labels fall back to the `stash:box:N` custom scheme.
PUBLIC_URL = os.environ.get("STASH_PUBLIC_URL", "").rstrip("/")


def _load_changelog_html() -> str:
    """Render CHANGELOG.md to HTML. Cached at import time — only changes on container restart."""
    path = ROOT / "CHANGELOG.md"
    if not path.exists():
        return ""
    try:
        import markdown as _md
        return _md.markdown(path.read_text(), extensions=["fenced_code", "tables"])
    except Exception:
        # Fall back to escaped plain text so a render error never breaks the page.
        from html import escape
        return f"<pre>{escape(path.read_text())}</pre>"


CHANGELOG_HTML = _load_changelog_html()


def _trigger_watchtower_update() -> None:
    """POST to watchtower's HTTP API to force an immediate scan+update.
    Runs as a background task so the HTTP response returns before our own
    container is potentially restarted."""
    if not WATCHTOWER_URL:
        return
    import urllib.request
    req = urllib.request.Request(
        f"{WATCHTOWER_URL}/v1/update",
        method="POST",
        headers={"Authorization": f"Bearer {WATCHTOWER_TOKEN}"} if WATCHTOWER_TOKEN else {},
    )
    try:
        urllib.request.urlopen(req, timeout=120).read()
    except Exception:
        # The container can be killed mid-call once watchtower pulls a new image —
        # that's expected, not an error worth surfacing.
        pass

app = FastAPI()
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")


# ── Localization seams ───────────────────────────────────────────────
# v1 ships English-only.  The seams land now (jinja2.ext.i18n + a
# NullTranslations identity catalog + a babel-driven date filter) so
# wrapping a string in `_()` or `{% trans %}` is a no-op today and
# becomes a translation task tomorrow.  Adding a real locale is a PR
# that drops a `.po` file under `locale/<lang>/LC_MESSAGES/messages.po`
# plus a one-line registration here; nothing else in the codebase
# changes.  See spec § "Localization".
import gettext as _gettext
from babel.dates import format_datetime as _babel_format_datetime
from babel.dates import format_date as _babel_format_date

# Active translations object.  NullTranslations passes source strings
# through unchanged — perfect for an English-only deployment that's
# wrapping strings for future-proofing.  When a `.po` file lands, swap
# this for `gettext.translation(...)`.
_translations = _gettext.NullTranslations()
_DEFAULT_LOCALE = os.environ.get("STASH_DEFAULT_LOCALE", "en")

templates.env.add_extension("jinja2.ext.i18n")
templates.env.install_gettext_translations(_translations, newstyle=True)


def _(message: str) -> str:
    """Mark a string for translation. v1 is English-only so this is the
    identity, but every user-visible string in Python code should still
    flow through here so `pybabel extract` finds them later."""
    return _translations.gettext(message)


def _format_datetime(dt, fmt: str = "medium", locale: str | None = None) -> str:
    """Locale-aware datetime formatter for templates. Accepts strings (ISO),
    datetimes, or None (returns empty)."""
    if dt is None or dt == "":
        return ""
    if isinstance(dt, str):
        from datetime import datetime
        try:
            dt = datetime.fromisoformat(dt.replace(" ", "T"))
        except ValueError:
            return dt  # render as-is if we can't parse
    return _babel_format_datetime(dt, format=fmt, locale=locale or _DEFAULT_LOCALE)


def _format_date(dt, fmt: str = "medium", locale: str | None = None) -> str:
    if dt is None or dt == "":
        return ""
    if isinstance(dt, str):
        from datetime import datetime
        try:
            dt = datetime.fromisoformat(dt.replace(" ", "T"))
        except ValueError:
            return dt
    return _babel_format_date(dt, format=fmt, locale=locale or _DEFAULT_LOCALE)


templates.env.filters["datetime"] = _format_datetime
templates.env.filters["date"] = _format_date


# ── Actor middleware ─────────────────────────────────────────────────
# Resolves X-Forwarded-Email (set by oauth2-proxy) into a `request.state.actor`
# carrying the active tenant + role + operator flag.  Replaces the old
# STASH_ALLOWED_EMAILS / FULLY_PUBLIC pair: the new gate is tenant_members.
# An email with no membership and no operator status gets a 403; everything
# downstream (routes, DAO once it lands) reads the actor off request.state.
#
# Operator emails (STASH_OPERATOR_EMAILS) get is_operator=True regardless
# of tenant membership.  /admin routes (lifecycle, metadata, vendor cost
# panel — see spec § "Operator surface") gate on this flag; tenant-data
# routes do NOT — operators access tenant data only via an explicit
# maintainer invite from the user, by design.
_OPERATOR_EMAILS = frozenset(
    e.strip().lower()
    for e in os.environ.get("STASH_OPERATOR_EMAILS", "").split(",")
    if e.strip()
)
# Actor lives in dao/_base.py so DAO methods can take it without a
# circular dao→app import.  Imported at the top of this module.


@app.middleware("http")
async def current_actor(request: Request, call_next):
    email = (request.headers.get("X-Forwarded-Email") or "").strip().lower()
    is_operator = bool(email) and email in _OPERATOR_EMAILS

    memberships: tuple[tuple[int, str], ...] = ()
    if email:
        with db() as conn:
            rows = conn.execute(
                "SELECT tenant_id, role FROM tenant_members "
                "WHERE email = ? "
                # joined_at is nullable until invites are accepted; sort
                # accepted members first (NULL last) without relying on
                # the NULLS LAST clause that older SQLite builds may
                # lack.
                "ORDER BY joined_at IS NULL, joined_at, tenant_id",
                (email,),
            ).fetchall()
        memberships = tuple((r["tenant_id"], r["role"]) for r in rows)

    if not memberships and not is_operator:
        return Response(
            "Forbidden — your email is not a member of any tenant on this stash.",
            status_code=403,
            media_type="text/plain",
        )

    active_tenant_id, active_role = memberships[0] if memberships else (None, None)
    request.state.actor = Actor(
        email=email,
        tenant_id=active_tenant_id,
        role=active_role,
        is_operator=is_operator,
        memberships=memberships,
    )
    return await call_next(request)


def _static_version() -> str:
    """Content hash of style.css, used for cache-busting. Picks up file changes
    without requiring a server restart — recomputed per request (tiny file, cheap)."""
    css = ROOT / "static" / "style.css"
    if not css.exists():
        return "0"
    import hashlib
    return hashlib.sha1(css.read_bytes()).hexdigest()[:8]


templates.env.globals["static_version"] = _static_version


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Multi-writer concurrency tuning. With multi-tenant ingest fanning
    # out to pending_items + tags + thumbs writes in quick succession,
    # the default rollback journal hits "database is locked" under load.
    # WAL lets readers proceed during writes, busy_timeout absorbs short
    # contention without raising, and synchronous=NORMAL pairs cleanly
    # with WAL for the throughput bump (durability still anchored by the
    # WAL checkpoint). See spec § "SQLite concurrency".
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
        -- ── Multi-tenancy core ────────────────────────────────────────
        -- See spec.md § "Architecture · Schema additions".  Every owned
        -- table joins back to a tenant; members are (tenant_id, email)
        -- pairs with a role; invites + object_shares are the two
        -- sharing mechanisms.  audit_log captures user-visible events
        -- (tenant_id NULL for operator cross-tenant actions).
        CREATE TABLE IF NOT EXISTS tenants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            -- Lifecycle: soft-delete first, hard-delete by cron after grace.
            deleted_at TEXT,
            hard_delete_after TEXT,
            archived_backup_key TEXT,
            archived_backup_until TEXT,
            -- Wrapped DEK (envelope encryption); populated in roadmap step 2.
            wrapped_dek BLOB,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tenant_members (
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            email TEXT NOT NULL COLLATE NOCASE,
            role TEXT NOT NULL,
            -- NULL locale = use Accept-Language / deployment default.
            locale TEXT,
            invited_by_email TEXT,
            invited_at TEXT,
            joined_at TEXT,
            PRIMARY KEY (tenant_id, email)
        );
        CREATE INDEX IF NOT EXISTS idx_tenant_members_email ON tenant_members(email);

        CREATE TABLE IF NOT EXISTS tenant_invites (
            token TEXT PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            email TEXT NOT NULL COLLATE NOCASE,
            role TEXT NOT NULL,
            created_by_email TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT,
            consumed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tenant_invites_email ON tenant_invites(email);

        CREATE TABLE IF NOT EXISTS object_shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            -- 'box' | 'item'.  Discriminator over the join target.
            target_kind TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            recipient_email TEXT NOT NULL COLLATE NOCASE,
            role TEXT NOT NULL,
            created_by_email TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            revoked_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_object_shares_target ON object_shares(target_kind, target_id);
        CREATE INDEX IF NOT EXISTS idx_object_shares_recipient ON object_shares(recipient_email);

        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            -- 'ai' | 'upload' | 'backup' | 'core'.
            surface TEXT NOT NULL,
            -- 'gemini_detect', 'gemini_art', 'anthropic_match',
            -- 'upload_bytes', 'backup_bytes', etc.
            kind TEXT NOT NULL,
            units INTEGER NOT NULL,
            cost_micros INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_usage_events_tenant_surface
            ON usage_events(tenant_id, surface, created_at);

        CREATE TABLE IF NOT EXISTS quotas (
            tenant_id INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
            monthly_ai_calls INTEGER,
            monthly_upload_bytes INTEGER,
            backup_storage_bytes INTEGER,
            -- JSON blob for plan-specific overrides; NULL = inherit plan defaults.
            overrides_json TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            -- NULL on operator cross-tenant actions that don't belong to a
            -- single tenant (e.g. global DR import).
            tenant_id INTEGER,
            actor_email TEXT,
            action TEXT NOT NULL,
            target_kind TEXT,
            target_id INTEGER,
            metadata_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_audit_log_tenant ON audit_log(tenant_id, created_at);

        -- ── App data tables (existing) ────────────────────────────────
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            floorplan TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS floors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            floorplan TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_floors_location ON floors(location_id);
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
            floor_id INTEGER REFERENCES floors(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            -- Bounding box on the floorplan as fractions 0..1 of image dims so
            -- coordinates survive re-uploading at a different resolution.
            x REAL NOT NULL DEFAULT 0,
            y REAL NOT NULL DEFAULT 0,
            w REAL NOT NULL DEFAULT 0,
            h REAL NOT NULL DEFAULT 0,
            color TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_rooms_location ON rooms(location_id);
        -- idx_rooms_floor is created in migrate_db, after the floor_id column
        -- has been ALTER-added on legacy schemas.
        CREATE TABLE IF NOT EXISTS boxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            location TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            box_id INTEGER NOT NULL REFERENCES boxes(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            notes TEXT,
            photo TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ingest_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            photo TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            item_count INTEGER,
            error TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS pending_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            photo TEXT,
            suggested_box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL,
            suggested_new_box_name TEXT,
            suggested_new_box_location TEXT,
            suggestion_reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS item_tags (
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            value TEXT,
            PRIMARY KEY (item_id, tag_id)
        );
        CREATE TABLE IF NOT EXISTS pending_item_tags (
            pending_item_id INTEGER NOT NULL REFERENCES pending_items(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            value TEXT,
            PRIMARY KEY (pending_item_id, tag_id)
        );
        CREATE INDEX IF NOT EXISTS idx_item_tags_tag ON item_tags(tag_id);
        CREATE INDEX IF NOT EXISTS idx_pending_item_tags_tag ON pending_item_tags(tag_id);
        """)


def _add_column_if_missing(conn, table: str, column: str, col_def: str) -> None:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


def migrate_db():
    with db() as conn:
        _add_column_if_missing(conn, "boxes", "last_audited_at", "TEXT")
        _add_column_if_missing(conn, "items", "is_missing", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "items", "last_seen_at", "TEXT")
        _add_column_if_missing(conn, "pending_items", "previous_box_name", "TEXT")
        # Bounding box from Gemini vision (0-1000 normalized coords)
        _add_column_if_missing(conn, "pending_items", "bbox_y_min", "INTEGER")
        _add_column_if_missing(conn, "pending_items", "bbox_x_min", "INTEGER")
        _add_column_if_missing(conn, "pending_items", "bbox_y_max", "INTEGER")
        _add_column_if_missing(conn, "pending_items", "bbox_x_max", "INTEGER")
        # Source photo preserved on items for crop undo / re-crop
        _add_column_if_missing(conn, "items", "source_photo", "TEXT")
        # Generated label background art (Nano Banana 2). Filename in UPLOAD_DIR.
        _add_column_if_missing(conn, "boxes", "background_art", "TEXT")
        # Floorplan-driven room association. boxes.location stays as a free-text
        # fallback for now, but room_id is the source of truth going forward.
        _add_column_if_missing(
            conn, "boxes", "room_id",
            "INTEGER REFERENCES rooms(id) ON DELETE SET NULL",
        )
        # Multi-floor: rooms now hang off a floor instead of straight off a
        # location. Original location_id is kept as a denormalized hint.
        _add_column_if_missing(
            conn, "rooms", "floor_id",
            "INTEGER REFERENCES floors(id) ON DELETE CASCADE",
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rooms_floor ON rooms(floor_id)")
        # Per-box color override. Falls back to the room color when null,
        # so boxes can choose to break out from their room's hue.
        _add_column_if_missing(conn, "boxes", "color", "TEXT")
        # Backfill source_photo for items created before this column existed
        conn.execute(
            "UPDATE items SET source_photo = photo WHERE source_photo IS NULL"
        )
        _migrate_legacy_locations(conn)
        _migrate_locations_to_floors(conn)

        # Multi-tenancy: every owned table gets a tenant_id pointing at
        # tenants(id).  Nullable in the column DDL because SQLite doesn't
        # support adding NOT NULL via ALTER without a DEFAULT we can't
        # safely supply pre-backfill — the DAO enforces non-null on writes
        # once it lands.  See spec.md § "Schema migrations to existing
        # tables".
        for tbl in (
            "locations", "floors", "rooms", "boxes", "items",
            "tags", "item_tags", "pending_item_tags",
            "pending_items", "ingest_jobs",
        ):
            _add_column_if_missing(
                conn, tbl, "tenant_id",
                "INTEGER REFERENCES tenants(id) ON DELETE CASCADE",
            )
        # Hot-path indexes — every later query that filters by tenant_id
        # rides on these.
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_boxes_tenant ON boxes(tenant_id)",
            "CREATE INDEX IF NOT EXISTS idx_items_tenant_box ON items(tenant_id, box_id)",
            "CREATE INDEX IF NOT EXISTS idx_locations_tenant ON locations(tenant_id)",
            "CREATE INDEX IF NOT EXISTS idx_floors_tenant ON floors(tenant_id)",
            "CREATE INDEX IF NOT EXISTS idx_rooms_tenant ON rooms(tenant_id)",
            "CREATE INDEX IF NOT EXISTS idx_pending_items_tenant ON pending_items(tenant_id)",
            "CREATE INDEX IF NOT EXISTS idx_ingest_jobs_tenant ON ingest_jobs(tenant_id)",
            "CREATE INDEX IF NOT EXISTS idx_tags_tenant ON tags(tenant_id)",
        ):
            conn.execute(idx_sql)

        # Versioning column for the optimistic-concurrency story
        # (roadmap step 4).  Defaults to 1 so existing rows behave as
        # "version 1" on the first edit after the migration lands.
        for tbl in ("boxes", "items", "rooms", "floors", "locations"):
            _add_column_if_missing(conn, tbl, "version", "INTEGER NOT NULL DEFAULT 1")
            _add_column_if_missing(conn, tbl, "updated_at", "TEXT")

        _migrate_to_multi_tenant(conn)
        conn.commit()
    # Filesystem migration runs outside the DB transaction (file IO can
    # take a while on big stashes; we don't want a long transaction
    # holding writes for everyone).  Idempotent — re-runs are no-ops
    # because already-migrated files live in tenant subdirs and carry
    # the encryption marker.
    _migrate_uploads_to_encrypted_tenant_dirs()


def _migrate_uploads_to_encrypted_tenant_dirs() -> None:
    """One-shot relocate + encrypt for pre-phase-2 cleartext uploads.

    Walks UPLOAD_DIR's flat root and for each cleartext blob:
      1. Looks up which tenant owns the file (any DB column reference).
      2. Encrypts it with that tenant's DEK.
      3. Writes the ciphertext to UPLOAD_DIR/{tenant_id}/{name}.
      4. Roundtrip-verifies the new file before deleting the original.

    Idempotent — files already inside a tenant subdir are skipped, and
    files in the flat root that already start with the encryption
    marker just get moved (no re-encryption).  Orphans (no DB
    reference) stay where they are; the orphan sweep cleans them up
    later.

    Crashes mid-migration are safe: each file is processed in
    isolation, with the cleartext original surviving until the new
    encrypted blob has been roundtrip-verified.

    Logs to stdout: a banner before the scan, per-tenant
    encrypted/relocated/orphaned/failed counts after.  When the
    function is a no-op (no flat-root files left to process) it stays
    quiet."""
    import logging
    log = logging.getLogger("stash.migrate")

    if not UPLOAD_DIR.exists():
        return

    # Quick pre-check: anything in the flat root at all?  If not, the
    # migration is a no-op and we don't need to log a thing.
    flat_root_files = [
        e for e in UPLOAD_DIR.iterdir()
        if e.is_file() and e.suffix != ".tmp"
    ]
    if not flat_root_files:
        return

    log.warning(
        "[migrate] phase-2 filesystem migration: %d cleartext file(s) in "
        "UPLOAD_DIR root — relocating into per-tenant subdirs and encrypting",
        len(flat_root_files),
    )

    # Build a (filename → tenant_id) map from every reference column,
    # plus the thumb companion of each.
    file_owners: dict[str, int] = {}
    with db() as conn:
        for sql in (
            "SELECT tenant_id, photo FROM items WHERE photo IS NOT NULL",
            "SELECT tenant_id, source_photo FROM items WHERE source_photo IS NOT NULL",
            "SELECT tenant_id, photo FROM pending_items WHERE photo IS NOT NULL",
            "SELECT tenant_id, photo FROM ingest_jobs WHERE photo IS NOT NULL",
            "SELECT tenant_id, background_art FROM boxes WHERE background_art IS NOT NULL",
            "SELECT tenant_id, floorplan FROM floors WHERE floorplan IS NOT NULL",
            "SELECT tenant_id, floorplan FROM locations WHERE floorplan IS NOT NULL",
        ):
            for tid, name in conn.execute(sql).fetchall():
                if tid is None or not name:
                    continue
                file_owners[name] = tid
                file_owners[f"{Path(name).stem}_thumb.jpg"] = tid

    encrypted = relocated_only = orphaned = failed = tmp_swept = 0
    by_tenant: dict[int, int] = {}

    for entry in flat_root_files:
        if entry.suffix == ".tmp":
            # Atomic-rename intermediate from an interrupted write.
            try:
                entry.unlink()
                tmp_swept += 1
            except FileNotFoundError:
                pass
            continue
        tenant_id = file_owners.get(entry.name)
        if tenant_id is None:
            # Orphan — leave for /maintenance/cleanup to deal with so a
            # bug here can't accidentally delete an unmapped file the
            # operator might want to rescue.
            orphaned += 1
            continue
        target = _tenant_file(tenant_id, entry.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # Already migrated under this name (an earlier interrupted
            # run got this far).  Drop the original.
            try:
                entry.unlink()
            except FileNotFoundError:
                pass
            continue
        try:
            blob = entry.read_bytes()
            if vault.looks_encrypted(blob):
                # Pre-encrypted (operator manually placed it, perhaps).
                # Just move it.
                entry.rename(target)
                relocated_only += 1
                by_tenant[tenant_id] = by_tenant.get(tenant_id, 0) + 1
                continue
            ciphertext = _encrypt_for(tenant_id, blob)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(ciphertext)
            roundtrip = _decrypt_for(tenant_id, tmp.read_bytes())
            if roundtrip != blob:
                tmp.unlink()
                failed += 1
                log.error(
                    "[migrate] roundtrip-verify failed for %s; cleartext "
                    "original kept in place for manual inspection",
                    entry.name,
                )
                continue  # something went wrong; original stays put
            os.replace(tmp, target)
            entry.unlink()
            encrypted += 1
            by_tenant[tenant_id] = by_tenant.get(tenant_id, 0) + 1
        except Exception as exc:
            # Don't crash startup on a single bad file.  An operator
            # can investigate via the filesystem if photos go missing.
            failed += 1
            log.error(
                "[migrate] failed to encrypt %s: %s; cleartext kept "
                "in place for manual inspection",
                entry.name, exc,
            )
            continue

    log.warning(
        "[migrate] phase-2 filesystem migration done: "
        "encrypted=%d relocated_only=%d orphaned=%d failed=%d tmp_swept=%d "
        "by_tenant=%s",
        encrypted, relocated_only, orphaned, failed, tmp_swept, by_tenant,
    )
    if orphaned:
        log.warning(
            "[migrate] %d file(s) in UPLOAD_DIR root had no DB reference and "
            "were left in place; run /maintenance/cleanup once you've "
            "confirmed there's nothing in there worth rescuing",
            orphaned,
        )
    if failed:
        log.warning(
            "[migrate] %d file(s) failed to migrate — cleartext originals "
            "are still in UPLOAD_DIR root.  Investigate before re-running.",
            failed,
        )


def _first_email_from_env(var: str) -> str:
    """Pluck the first non-empty email out of a comma-separated env var.
    Falls back to '' if none."""
    for part in os.environ.get(var, "").split(","):
        e = part.strip().lower()
        if e:
            return e
    return ""


def _migrate_to_multi_tenant(conn) -> None:
    """One-time: existing data gets folded into a Personal tenant.

    Runs whenever migrate_db sees rows in the app tables and no tenants
    yet exist.  The bootstrap email comes from STASH_BOOTSTRAP_MEMBER_EMAIL
    (preferred), falling back to the first entry in STASH_ALLOWED_EMAILS
    so existing single-user deploys upgrade without extra config.

    Idempotent: any subsequent run is a no-op once tenants exist.
    Backfill of NULL tenant_id rows happens unconditionally because a
    legacy route inserting after the migration could leave fresh NULLs."""
    if conn.execute("SELECT 1 FROM tenants LIMIT 1").fetchone():
        # Sweep any NULLs that snuck in from legacy routes — once the
        # DAO migration (roadmap step 3-4) is complete this becomes a
        # no-op.
        for tbl in (
            "locations", "floors", "rooms", "boxes", "items",
            "tags", "item_tags", "pending_item_tags",
            "pending_items", "ingest_jobs",
        ):
            conn.execute(
                f"UPDATE {tbl} SET tenant_id = (SELECT id FROM tenants ORDER BY id LIMIT 1) "
                f"WHERE tenant_id IS NULL"
            )
        return

    has_data = any(
        conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
        for t in ("boxes", "items", "locations", "ingest_jobs", "pending_items")
    )
    if not has_data:
        # Fresh DB — first user creates a tenant via the sign-up flow
        # (roadmap step 5).  Nothing to migrate yet.
        return

    bootstrap_email = (
        os.environ.get("STASH_BOOTSTRAP_MEMBER_EMAIL", "").strip().lower()
        or _first_email_from_env("STASH_ALLOWED_EMAILS")
    )

    cur = conn.execute(
        "INSERT INTO tenants (name, plan) VALUES ('Personal', 'pro')"
    )
    tenant_id = cur.lastrowid

    if bootstrap_email:
        conn.execute(
            "INSERT INTO tenant_members "
            "(tenant_id, email, role, joined_at) "
            "VALUES (?, ?, 'maintainer', CURRENT_TIMESTAMP)",
            (tenant_id, bootstrap_email),
        )
        conn.execute(
            "INSERT INTO audit_log (tenant_id, actor_email, action, metadata_json) "
            "VALUES (?, ?, 'tenant.bootstrap', ?)",
            (tenant_id, bootstrap_email, '{"source": "migrate_to_multi_tenant"}'),
        )

    for tbl in (
        "locations", "floors", "rooms", "boxes", "items",
        "tags", "item_tags", "pending_item_tags",
        "pending_items", "ingest_jobs",
    ):
        conn.execute(
            f"UPDATE {tbl} SET tenant_id = ? WHERE tenant_id IS NULL",
            (tenant_id,),
        )


def _migrate_locations_to_floors(conn) -> None:
    """Convert each existing location-with-floorplan into a single 'Main floor'
    so the floor selector has something to point at and existing rooms get
    attached to a floor. Idempotent — only runs if no floors exist yet."""
    if conn.execute("SELECT 1 FROM floors LIMIT 1").fetchone():
        return
    locs = conn.execute(
        "SELECT id, floorplan FROM locations WHERE floorplan IS NOT NULL"
    ).fetchall()
    for loc in locs:
        cur = conn.execute(
            "INSERT INTO floors (location_id, name, floorplan, sort_order) "
            "VALUES (?, 'Main floor', ?, 0)",
            (loc["id"], loc["floorplan"]),
        )
        floor_id = cur.lastrowid
        conn.execute(
            "UPDATE rooms SET floor_id = ? WHERE location_id = ? AND floor_id IS NULL",
            (floor_id, loc["id"]),
        )


def _migrate_legacy_locations(conn) -> None:
    """One-shot conversion of free-text `boxes.location` strings into a default
    Location + Rooms structure. Idempotent — only runs if no locations exist yet,
    so re-running on an already-migrated DB is a no-op."""
    has_any_location = conn.execute("SELECT 1 FROM locations LIMIT 1").fetchone()
    if has_any_location:
        return
    legacy = [
        r["location"].strip() for r in conn.execute(
            "SELECT DISTINCT location FROM boxes "
            "WHERE location IS NOT NULL AND TRIM(location) != ''"
        ).fetchall()
    ]
    if not legacy:
        return
    cur = conn.execute(
        "INSERT INTO locations (name) VALUES (?)", ("Default location",),
    )
    location_id = cur.lastrowid
    for room_name in legacy:
        cur = conn.execute(
            "INSERT INTO rooms (location_id, name, x, y, w, h) VALUES (?, ?, 0, 0, 0, 0)",
            (location_id, room_name),
        )
        room_id = cur.lastrowid
        conn.execute(
            "UPDATE boxes SET room_id = ? WHERE TRIM(location) = ?",
            (room_id, room_name),
        )


# init_db / migrate_db are called at the *bottom* of this module — after
# every helper they need (filesystem migration, encryption helpers,
# path utilities) is defined.  Keep this comment so a future contributor
# doesn't move them back up here for tidiness.


MAX_IMAGE_DIM = 2048
JPEG_QUALITY = 85
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB; matches Caddy's request_body cap

# Refuse to decode anything that would expand to > 50M pixels (~50MB raw RGB).
# Defends against PNG/TIFF "decompression bombs" — small files that allocate
# huge amounts of memory when decoded.
from PIL import Image as _PilImage
_PilImage.MAX_IMAGE_PIXELS = 50_000_000


def save_photo(tenant_id: int, photo: UploadFile | None) -> str | None:
    if not photo or not photo.filename:
        return None
    return save_photo_bytes(tenant_id, photo.file.read(), photo.filename)


def save_photo_bytes(tenant_id: int, data: bytes, filename: str) -> str:
    """Re-encode as JPEG with EXIF orientation baked in and longest side capped.

    Encrypts the resulting bytes with the tenant's DEK and writes to
    UPLOAD_DIR/{tenant_id}/{name}.

    On PIL failure we still write the bytes (test fixtures use synthetic JPEG
    headers PIL can't decode), but ALWAYS as `.jpg` — never honor the caller's
    extension. Combined with `X-Content-Type-Options: nosniff` at the edge, this
    closes the stored-XSS path where an authenticated user uploads `evil.html`.

    Also writes the companion thumbnail in the same pass so the first grid
    view of a fresh upload doesn't have to lazy-generate it.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Upload too large")
    from PIL import Image, ImageOps
    import io as _io
    try:
        img = Image.open(_io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        if max(img.size) > MAX_IMAGE_DIM:
            img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM), Image.LANCZOS)
        name = f"{secrets.token_hex(8)}.jpg"
        out = _io.BytesIO()
        img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        _write_encrypted(tenant_id, name, out.getvalue())
        # Pre-generate the thumb from the in-memory image — cheaper than
        # opening the file again, and covers the new-upload-then-immediately-
        # render case without paying the lazy-gen cost on the first request.
        _save_thumb_from_image(tenant_id, img, name)
        return name
    except HTTPException:
        raise
    except Exception:
        name = f"{secrets.token_hex(8)}.jpg"
        _write_encrypted(tenant_id, name, data)
        return name


def _save_thumb_from_image(tenant_id: int, img, name: str) -> None:
    """Encrypt + write a thumbnail for `name` from an already-decoded PIL image."""
    from PIL import Image as _Image
    import io as _io
    try:
        thumb_img = img.copy()
        if max(thumb_img.size) > THUMB_MAX_DIM:
            thumb_img.thumbnail((THUMB_MAX_DIM, THUMB_MAX_DIM), _Image.LANCZOS)
        out = _io.BytesIO()
        thumb_img.save(out, format="JPEG", quality=80, optimize=True)
        thumb_name = _tenant_thumb(tenant_id, name).name
        _write_encrypted(tenant_id, thumb_name, out.getvalue())
    except Exception:
        pass  # the lazy /thumbs endpoint will retry generation on first view


def ensure_tag(conn, tenant_id: int, name: str) -> int:
    """Get or create a tag by name (case-insensitive) within a tenant.
    Returns tag id.

    Tags are per-tenant per spec § "Tag uniqueness" — same tag name in
    two tenants resolves to two distinct rows.  Existing rows from the
    pre-multi-tenant migration carry the same tenant_id but no UNIQUE
    constraint enforces (tenant_id, name) yet (DAO migration in step 3
    adds it); for now the lookup is by (tenant_id, name) and an
    INSERT OR IGNORE matches on the legacy global UNIQUE on `name`."""
    name = name.strip()
    conn.execute(
        "INSERT OR IGNORE INTO tags (name, tenant_id) VALUES (?, ?)",
        (name, tenant_id),
    )
    row = conn.execute(
        "SELECT id FROM tags WHERE name = ? AND "
        "(tenant_id = ? OR tenant_id IS NULL) "
        "ORDER BY tenant_id IS NULL, id LIMIT 1",
        (name, tenant_id),
    ).fetchone()
    return row["id"]


def parse_tag_input(raw: str) -> list[tuple[str, str | None]]:
    """Parse comma-separated tag input. 'serial:ABC' → ('serial', 'ABC'), 'red' → ('red', None)."""
    results = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            key, val = part.split(":", 1)
            results.append((key.strip(), val.strip() or None))
        else:
            results.append((part, None))
    return results


def set_item_tags(conn, tenant_id: int, item_id: int, tag_entries: list[tuple[str, str | None]]) -> None:
    for tag_name, value in tag_entries:
        tag_id = ensure_tag(conn, tenant_id, tag_name)
        conn.execute(
            "INSERT OR REPLACE INTO item_tags (item_id, tag_id, value, tenant_id) "
            "VALUES (?, ?, ?, ?)",
            (item_id, tag_id, value, tenant_id),
        )


def get_item_tags(conn, item_id: int) -> list[dict]:
    return [
        dict(r) for r in conn.execute(
            "SELECT t.id AS tag_id, t.name, it.value "
            "FROM item_tags it JOIN tags t ON t.id = it.tag_id "
            "WHERE it.item_id = ? ORDER BY t.name",
            (item_id,),
        ).fetchall()
    ]


def format_tag(name: str, value: str | None) -> str:
    return f"{name}:{value}" if value else name


def _open_image_oriented(path: Path):
    """Open an image and apply EXIF orientation so pixels match what the user sees.
    Cropper.js auto-rotates per EXIF, so we must do the same before cropping."""
    from PIL import Image, ImageOps
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    return img


def crop_photo(tenant_id: int, photo_name: str, bbox: tuple[int, int, int, int]) -> str:
    """Crop a photo using bbox (y_min, x_min, y_max, x_max in 0-1000 coords).
    Returns the filename of the cropped image saved to UPLOAD_DIR/{tenant_id}/.

    Source is read + decrypted from the tenant's directory; the new crop is
    encrypted with the tenant's DEK before write.  Normalizes to JPEG and
    pre-generates the companion thumbnail so a re-crop is reflected
    immediately on the next page render — without the thumbnail side, lazy
    /thumbs generation can fall back to serving the source under an
    immutable cache header, leaving stale crops visible."""
    plaintext = _read_encrypted(tenant_id, photo_name)
    import io as _io
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(_io.BytesIO(plaintext)))
    w, h = img.size
    y_min, x_min, y_max, x_max = bbox
    # Convert 0-1000 normalized coords to pixels
    left = int(x_min / 1000 * w)
    top = int(y_min / 1000 * h)
    right = int(x_max / 1000 * w)
    bottom = int(y_max / 1000 * h)
    # Clamp and ensure minimum size
    left = max(0, left)
    top = max(0, top)
    right = min(w, right)
    bottom = min(h, bottom)
    if right - left < 10 or bottom - top < 10:
        return photo_name  # bbox too small, use original
    cropped = img.crop((left, top, right, bottom))
    if cropped.mode not in ("RGB", "L"):
        cropped = cropped.convert("RGB")
    crop_name = f"{secrets.token_hex(8)}.jpg"
    out = _io.BytesIO()
    cropped.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    _write_encrypted(tenant_id, crop_name, out.getvalue())
    _save_thumb_from_image(tenant_id, cropped, crop_name)
    return crop_name


def _photo_still_referenced(conn, photo_name: str) -> bool:
    """True if any row still points at this upload file."""
    if not photo_name:
        return True
    return conn.execute(
        "SELECT 1 FROM items WHERE photo = ? OR source_photo = ? "
        "UNION SELECT 1 FROM pending_items WHERE photo = ? "
        "UNION SELECT 1 FROM ingest_jobs WHERE photo = ? "
        "UNION SELECT 1 FROM boxes WHERE background_art = ? "
        "UNION SELECT 1 FROM floors WHERE floorplan = ? "
        "UNION SELECT 1 FROM locations WHERE floorplan = ? LIMIT 1",
        (photo_name,) * 7,
    ).fetchone() is not None


def _delete_upload_if_orphan(conn, tenant_id: int, photo_name: str) -> None:
    """Delete a tenant's upload (and its thumb companion) if no row
    still references it.  Caller passes the tenant_id of the row that
    just stopped pointing at the file — cross-tenant references aren't
    a thing in the new schema, so the file lives in exactly one
    tenant's directory."""
    if not photo_name or _photo_still_referenced(conn, photo_name):
        return
    try:
        _tenant_file(tenant_id, photo_name).unlink()
    except FileNotFoundError:
        pass
    # Companion thumbnail goes with the source. The thumb is a derived
    # artifact, never tracked in the DB — so its lifetime is purely tied to
    # whether anything still references the original.
    _delete_thumb_if_exists(tenant_id, photo_name)


import re as _re
_UPLOAD_NAME_RE = _re.compile(r"^[A-Za-z0-9._-]+$")


# ── Per-tenant upload paths + encryption helpers ─────────────────────
# Files live under UPLOAD_DIR/{tenant_id}/{name} and are encrypted with
# the tenant's DEK.  See spec § "Filesystem layout" + "Encryption at
# rest".  Routes derive tenant_id from request.state.actor.tenant_id;
# the URL itself stays plain (`/uploads/{name}`) so templates don't
# need to thread tenant ids through every src attribute.

def _tenant_dir(tenant_id: int) -> Path:
    return UPLOAD_DIR / str(tenant_id)


def _ensure_tenant_dir(tenant_id: int) -> Path:
    p = _tenant_dir(tenant_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _tenant_file(tenant_id: int, name: str) -> Path:
    """Encrypted blob path for a given upload."""
    return _tenant_dir(tenant_id) / name


def _tenant_thumb(tenant_id: int, name: str) -> Path:
    """Encrypted thumbnail path for a given upload (always .jpg)."""
    return _tenant_dir(tenant_id) / f"{Path(name).stem}_thumb.jpg"


def _encrypt_for(tenant_id: int, plaintext: bytes) -> bytes:
    """Encrypt bytes with the tenant's DEK.  Opens a fresh connection;
    the DEK cache hides the cost after the first call per tenant."""
    with db() as conn:
        return vault.encrypt_for_tenant(conn, tenant_id, _KEK, plaintext)


def _decrypt_for(tenant_id: int, ciphertext: bytes) -> bytes:
    with db() as conn:
        return vault.decrypt_for_tenant(conn, tenant_id, _KEK, ciphertext)


def _write_encrypted(tenant_id: int, name: str, plaintext: bytes) -> None:
    """Encrypt + atomically replace the on-disk blob."""
    _ensure_tenant_dir(tenant_id)
    target = _tenant_file(tenant_id, name)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(_encrypt_for(tenant_id, plaintext))
    os.replace(tmp, target)


def _read_encrypted(tenant_id: int, name: str) -> bytes:
    """Read + decrypt an upload.  Caller is responsible for tenant scoping
    (i.e. for routes, pull tenant_id from request.state.actor)."""
    target = _tenant_file(tenant_id, name)
    return _decrypt_for(tenant_id, target.read_bytes())


def _resolve_tenant_for_filename(conn, name: str) -> int | None:
    """Reverse-lookup which tenant owns a file by scanning every column
    that holds an upload reference.  Used by serve_upload / serve_thumb
    to keep URLs tenant-id-free; also used by the filesystem migration
    to relocate cleartext blobs into the right per-tenant directory."""
    for sql in (
        "SELECT tenant_id FROM items WHERE photo = ? OR source_photo = ? LIMIT 1",
        "SELECT tenant_id FROM pending_items WHERE photo = ? LIMIT 1",
        "SELECT tenant_id FROM ingest_jobs WHERE photo = ? LIMIT 1",
        "SELECT tenant_id FROM boxes WHERE background_art = ? LIMIT 1",
        "SELECT tenant_id FROM floors WHERE floorplan = ? LIMIT 1",
        "SELECT tenant_id FROM locations WHERE floorplan = ? LIMIT 1",
    ):
        params = (name, name) if "OR" in sql else (name,)
        row = conn.execute(sql, params).fetchone()
        if row and row["tenant_id"] is not None:
            return row["tenant_id"]
    # Thumb companions follow their source: strip _thumb suffix and
    # try the source's owner.  Same approach the migration uses.
    stem = Path(name).stem
    if stem.endswith("_thumb"):
        source_stem = stem[: -len("_thumb")]
        # Source extension is unknown without filesystem inspection, but
        # the DB column always carries the full filename — try the four
        # most common image extensions.
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            for tid_sql in (
                "SELECT tenant_id FROM items WHERE photo = ? OR source_photo = ? LIMIT 1",
                "SELECT tenant_id FROM pending_items WHERE photo = ? LIMIT 1",
                "SELECT tenant_id FROM ingest_jobs WHERE photo = ? LIMIT 1",
                "SELECT tenant_id FROM boxes WHERE background_art = ? LIMIT 1",
                "SELECT tenant_id FROM floors WHERE floorplan = ? LIMIT 1",
                "SELECT tenant_id FROM locations WHERE floorplan = ? LIMIT 1",
            ):
                src = f"{source_stem}{ext}"
                params = (src, src) if "OR" in tid_sql else (src,)
                row = conn.execute(tid_sql, params).fetchone()
                if row and row["tenant_id"] is not None:
                    return row["tenant_id"]
    return None

# Longest side of a generated thumbnail. 320 px renders crisply at 100 px CSS
# squares on retina (3x = 300) without paying the cost of the 2048 px source.
THUMB_MAX_DIM = 320

# Cap concurrent thumbnail decodes so a page with a dozen brand-new photos
# can't fan out into a dozen full-resolution PIL decodes at once and run the
# container out of memory. Two at a time is plenty — each generation finishes
# in <100 ms once draft() is in play.
import threading as _threading
_THUMB_GEN_SEMAPHORE = _threading.Semaphore(2)


def _thumb_path(tenant_id: int, name: str) -> Path:
    """Companion thumbnail file for a given upload. Always .jpg since
    save_photo_bytes re-encodes everything to JPEG anyway."""
    return _tenant_thumb(tenant_id, name)


def _is_thumb_name(name: str) -> bool:
    return Path(name).stem.endswith("_thumb")


def _ensure_thumb(tenant_id: int, name: str) -> bytes | None:
    """Lazy-generate the thumb for an existing upload.  Returns decrypted
    thumb bytes or None if the source is missing / un-decodable.  Writes
    the encrypted thumb via tmp-file + rename so concurrent requests
    can't see a half-written file.

    Memory-aware: uses PIL's draft() to tell the JPEG decoder to scale down
    BEFORE allocating pixel buffers. A pre-cap 7000×7000 JPEG decodes at full
    res to ~150 MB RGB; with draft asking for ~640 px, the same image lands
    at <3 MB. Combined with the module-level semaphore, the container can no
    longer be OOM-killed by a fan-out of concurrent thumb requests."""
    if _is_thumb_name(name):
        return None
    src = _tenant_file(tenant_id, name)
    if not src.exists():
        return None
    thumb = _tenant_thumb(tenant_id, name)
    if thumb.exists():
        try:
            return _decrypt_for(tenant_id, thumb.read_bytes())
        except Exception:
            return None

    with _THUMB_GEN_SEMAPHORE:
        # Re-check now that we hold the lock — another request may have
        # generated this very thumb while we were queued.
        if thumb.exists():
            try:
                return _decrypt_for(tenant_id, thumb.read_bytes())
            except Exception:
                return None
        try:
            from PIL import Image, ImageOps
            import io as _io
            plaintext = _decrypt_for(tenant_id, src.read_bytes())
            with Image.open(_io.BytesIO(plaintext)) as opened:
                # draft() is JPEG-only and a no-op on other formats. Asking
                # for 2x the target size gives the decoder enough room to
                # downscale further with a clean LANCZOS pass below.
                if opened.format == "JPEG":
                    opened.draft("RGB", (THUMB_MAX_DIM * 2, THUMB_MAX_DIM * 2))
                img = ImageOps.exif_transpose(opened)
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                if max(img.size) > THUMB_MAX_DIM:
                    img.thumbnail((THUMB_MAX_DIM, THUMB_MAX_DIM), Image.LANCZOS)
                out = _io.BytesIO()
                img.save(out, format="JPEG", quality=80, optimize=True)
                thumb_plaintext = out.getvalue()
            _write_encrypted(tenant_id, thumb.name, thumb_plaintext)
            return thumb_plaintext
        except Exception:
            # Any decode failure (test fixtures, corrupt jpegs, OOM in PIL,
            # etc) — let the caller fall back to serving the full-res source
            # so the page doesn't break.
            return None


def _delete_thumb_if_exists(tenant_id: int, name: str) -> None:
    if _is_thumb_name(name):
        return
    try:
        _thumb_path(tenant_id, name).unlink()
    except FileNotFoundError:
        pass


def _resolve_serve_tenant(request: Request, name: str) -> int | None:
    """Pick the tenant whose directory holds `name` for this actor.

    Most requests resolve cleanly to ``actor.tenant_id`` — the file is
    in the active tenant's directory.  But operators with no
    membership, and (eventually) share recipients, need a fallback:
    walk every tenant the actor *might* see and pick the first that
    owns the file.  See spec § "Sharing model" for where this widens
    when object_shares lands."""
    actor: Actor = request.state.actor
    if actor.tenant_id is not None:
        # Fast path: file lives in the active tenant's directory.
        if _tenant_file(actor.tenant_id, name).exists():
            return actor.tenant_id
        # Membership in another tenant?  (Multi-membership pre-dates
        # the switcher; resolve any tenant the actor has access to.)
        for tid, _role in actor.memberships:
            if tid != actor.tenant_id and _tenant_file(tid, name).exists():
                return tid
    if actor.is_operator:
        # Operators have no automatic data access through /admin (see
        # spec § "Operator surface"), but if a tenant maintainer has
        # invited them, the membership above already resolved.  Refuse
        # any operator path that isn't backed by a real membership.
        return None
    return None


@app.get("/uploads/{name}")
def serve_upload(request: Request, name: str):
    # Reject obviously hostile names before any filesystem touch. We only
    # generate names like `<hex>.jpg` — anything outside that alphabet is not
    # ours and not worth resolving.
    if not _UPLOAD_NAME_RE.match(name) or ".." in name:
        raise HTTPException(404)
    tenant_id = _resolve_serve_tenant(request, name)
    if tenant_id is None:
        raise HTTPException(404)
    upload_root = UPLOAD_DIR.resolve()
    p = _tenant_file(tenant_id, name).resolve()
    # Defense-in-depth: even after the regex check, refuse to serve anything
    # that lands outside UPLOAD_DIR (e.g. through symlink shenanigans).
    if not p.is_relative_to(upload_root) or not p.is_file():
        raise HTTPException(404)
    try:
        plaintext = _decrypt_for(tenant_id, p.read_bytes())
    except Exception:
        raise HTTPException(500, "decryption failed")
    # Photo MIME — every stored upload is JPEG (save_photo_bytes
    # normalises) so we can hard-code without sniffing bytes.
    return Response(content=plaintext, media_type="image/jpeg")


@app.get("/thumbs/{name}")
def serve_thumb(request: Request, name: str):
    """Serves a downscaled version of /uploads/{name} for grid + list views.
    Filenames are content-hashed so the result is immutable — long-cached."""
    if not _UPLOAD_NAME_RE.match(name) or ".." in name:
        raise HTTPException(404)
    tenant_id = _resolve_serve_tenant(request, name)
    if tenant_id is None:
        raise HTTPException(404)
    plaintext = _ensure_thumb(tenant_id, name)
    if plaintext is None:
        # Fall through to the full-res source so the page doesn't break.
        try:
            plaintext = _read_encrypted(tenant_id, name)
        except Exception:
            raise HTTPException(404)
    return Response(
        content=plaintext,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    actor: Actor = request.state.actor
    boxes = dao_boxes.list_with_counts(actor)
    thumbs = dao_items.list_recent_photos_per_box(actor, limit_per_box=5)
    rooms = dao_rooms.list_for_picker(actor)
    box_groups = _group_boxes_for_index(boxes)
    return templates.TemplateResponse(
        request, "index.html",
        {"boxes": boxes, "thumbs": thumbs, "rooms": rooms,
         "box_groups": box_groups},
    )


def _group_boxes_for_index(boxes) -> list[dict]:
    """Bucket boxes by their (location, room) so the index renders as
    visual sections instead of one undifferentiated grid.  The SQL is
    already sorted into the right order — we just walk it and start a
    new bucket whenever the key changes."""
    groups: list[dict] = []
    prev_key: object = object()
    for b in boxes:
        if b["room_name"]:
            key = ("room", b["location_id"], b["room_id"])
        elif b["location"] and b["location"].strip():
            key = ("loc", b["location"].strip().casefold())
        else:
            key = ("none",)
        if key != prev_key:
            groups.append({
                "kind": key[0],
                "location_name": b["location_name"],
                "room_name": b["room_name"],
                "room_color": b["room_color"],
                "legacy_location": b["location"] if not b["room_name"] else None,
                "boxes": [],
            })
            prev_key = key
        groups[-1]["boxes"].append(b)
    return groups


def _coerce_room_id(value: str) -> int | None:
    """Form posts the room id as a string — empty/none means clear the link."""
    if not value or value in ("none", "0"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@app.post("/boxes")
def create_box(
    request: Request,
    name: str = Form(...),
    location: str = Form(""),
    notes: str = Form(""),
    room_id: str = Form(""),
):
    actor: Actor = request.state.actor
    rid = _coerce_room_id(room_id)
    # If a room is picked, denormalize its name into boxes.location so the
    # plain text shows up everywhere that doesn't JOIN to rooms.  Allow
    # legacy tenant_id-NULL rooms to match too — pre-multi-tenancy
    # databases may still have those rows around.
    if rid is not None:
        with db() as conn:
            row = conn.execute(
                "SELECT name FROM rooms WHERE id = ? "
                "  AND (tenant_id = ? OR tenant_id IS NULL)",
                (rid, actor.tenant_id),
            ).fetchone()
        if row:
            location = row["name"]
        else:
            rid = None
    try:
        dao_boxes.create(actor, name, location, notes, room_id=rid)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse("/", status_code=303)


def _known_locations(actor: Actor) -> list[str]:
    """Distinct denormalised box.location strings the actor's tenant has
    used — feeds the location combobox on the box-edit form."""
    if actor.tenant_id is None:
        return []
    with db() as conn:
        return [
            r["location"] for r in conn.execute(
                "SELECT DISTINCT location FROM boxes "
                "WHERE location IS NOT NULL AND location != '' "
                "  AND tenant_id = ? "
                "ORDER BY location",
                (actor.tenant_id,),
            ).fetchall()
        ]


@app.get("/boxes/{box_id}", response_class=HTMLResponse)
def box_detail(request: Request, box_id: int):
    actor: Actor = request.state.actor
    try:
        box = dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    items_raw = dao_items.list_for_box(actor, box_id)
    # Reverse to newest-first to match the previous ORDER BY DESC.
    items_raw = list(reversed(items_raw))
    items_with_tags = [
        {"item": it, "tags": dao_items.list_tags_for_item(actor, it["id"])}
        for it in items_raw
    ]
    other_boxes = [
        {"id": b["id"], "name": b["name"], "location": b["location"]}
        for b in dao_boxes.list_for_picker(actor)
        if b["id"] != box_id
    ]
    other_boxes.sort(key=lambda b: b["name"])
    locations = _known_locations(actor)
    rooms = dao_rooms.list_for_picker(actor)
    all_tags = dao_tags.list_names(actor)
    return templates.TemplateResponse(
        request, "box.html",
        {
            "box": box,
            "items_with_tags": items_with_tags,
            "other_boxes": other_boxes,
            "locations": locations,
            "rooms": rooms,
            "all_tags": all_tags,
            "color_palette": _ROOM_COLORS,
        },
    )


@app.post("/boxes/{box_id}/move-to-room")
def move_box_to_room(
    request: Request,
    box_id: int,
    room_id: str = Form(""),
):
    """Reassign a box to a different room. Used by floorplan drag-and-drop
    and as a generic API. Empty room_id clears the assignment."""
    actor: Actor = request.state.actor
    rid = _coerce_room_id(room_id)
    location_text = ""
    if rid is not None:
        # Validate the room belongs to this tenant before passing through.
        try:
            location_text = dao_rooms.get_with_location(actor, rid)["name"]
        except NotFoundError:
            raise HTTPException(400, "Unknown room")
    try:
        # set_room handles the box-tenancy check; we update location text
        # alongside it via the broader update() call so the legacy
        # free-text location stays in sync with the room reassignment.
        box = dao_boxes.get_by_id(actor, box_id)
        dao_boxes.update(
            actor, box_id,
            name=box["name"], location=location_text,
            notes=box.get("notes") or "", room_id=rid,
            color=box.get("color"),
        )
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "room_id": rid}
    return RedirectResponse(f"/boxes/{box_id}", status_code=303)


@app.post("/boxes/{box_id}/edit")
def edit_box(
    request: Request,
    box_id: int,
    name: str = Form(...),
    location: str = Form(""),
    notes: str = Form(""),
    room_id: str = Form(""),
    color: str = Form(""),
):
    if not name.strip():
        raise HTTPException(400, "Name required")
    actor: Actor = request.state.actor
    rid = _coerce_room_id(room_id)
    # "" / "inherit" wipes the override and falls back to the room color.
    color_val: str | None
    if color.strip() == "" or color.strip() == "inherit":
        color_val = None
    elif color.strip() in _ROOM_COLORS:
        color_val = color.strip()
    else:
        color_val = None  # silently reject off-palette
    if rid is not None:
        try:
            location = dao_rooms.get_with_location(actor, rid)["name"]
        except NotFoundError:
            rid = None
    try:
        dao_boxes.update(
            actor, box_id,
            name=name, location=location, notes=notes,
            room_id=rid, color=color_val,
        )
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse(f"/boxes/{box_id}", status_code=303)


@app.post("/items/{item_id}/move")
def move_item(request: Request, item_id: int, box_id: int = Form(...)):
    actor: Actor = request.state.actor
    # Distinguish "item gone" (404) from "target box bad" (400) — the
    # legacy route did this via two separate SELECTs; we reproduce it
    # by validating the target box first, then letting move_to_box
    # raise NotFoundError only for missing items.
    try:
        dao_items.get_by_id(actor, item_id)
    except NotFoundError:
        raise HTTPException(404)
    try:
        dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(400, "Unknown box")
    try:
        dao_items.move_to_box(actor, item_id, box_id)
    except ForbiddenError:
        raise HTTPException(403)
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "item_id": item_id, "box_id": box_id}
    return RedirectResponse(f"/boxes/{box_id}#item-{item_id}", status_code=303)


@app.post("/boxes/{box_id}/move-items")
async def bulk_move_items(request: Request, box_id: int):
    """Bulk-move all submitted item ids out of `box_id` and into the
    target.  Tenant-scoped: the UPDATE clause filters by tenant_id so a
    crafted item_ids list pointing at another tenant's items can't move
    them."""
    actor: Actor = request.state.actor
    form_data = await request.form()
    target_box_id = int(form_data["target_box_id"])
    item_ids = [int(v) for v in form_data.getlist("item_ids")]
    if not item_ids:
        return RedirectResponse(f"/boxes/{box_id}", status_code=303)
    # Validate the target box belongs to the actor's tenant.
    try:
        dao_boxes.get_by_id(actor, target_box_id)
    except NotFoundError:
        raise HTTPException(400, "Unknown target box")
    # Move each item via the DAO (tenant + box checks happen there).
    for item_id in item_ids:
        try:
            dao_items.move_to_box(actor, item_id, target_box_id)
        except (NotFoundError, ForbiddenError):
            # Skip items that don't belong to this actor / tenant
            # rather than failing the whole batch — the legacy SQL had
            # the same lenient semantics via the `AND box_id = ?` clause.
            continue
    return RedirectResponse(f"/boxes/{target_box_id}", status_code=303)


@app.get("/boxes/{box_id}/audit", response_class=HTMLResponse)
def audit_box(request: Request, box_id: int):
    actor: Actor = request.state.actor
    try:
        box = dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    items = sorted(
        dao_items.list_for_box(actor, box_id),
        key=lambda it: (it["name"] or ""),
    )
    return templates.TemplateResponse(
        request, "audit.html", {"box": box, "items": items}
    )


@app.post("/boxes/{box_id}/audit")
async def submit_audit(request: Request, box_id: int):
    form_data = await request.form()
    found_ids = {int(v) for v in form_data.getlist("found")}
    actor: Actor = request.state.actor
    with db() as conn:
        box = conn.execute(
            "SELECT name, tenant_id FROM boxes WHERE id = ? AND tenant_id = ?",
            (box_id, actor.tenant_id),
        ).fetchone()
        if not box:
            raise HTTPException(404)
        tenant_id = box["tenant_id"] or actor.tenant_id
        all_items = conn.execute(
            "SELECT id, name, notes, photo FROM items "
            "WHERE box_id = ? AND tenant_id = ?",
            (box_id, actor.tenant_id),
        ).fetchall()
        moved_to_queue = 0
        for it in all_items:
            if it["id"] in found_ids:
                conn.execute(
                    "UPDATE items SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (it["id"],),
                )
            else:
                # Item not in box anymore — extract to sort queue with provenance
                cur = conn.execute(
                    "INSERT INTO pending_items "
                    "(name, description, photo, previous_box_name, tenant_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (it["name"], it["notes"], it["photo"], box["name"], tenant_id),
                )
                pending_id = cur.lastrowid
                # Preserve tags
                conn.execute(
                    "INSERT INTO pending_item_tags "
                    "(pending_item_id, tag_id, value, tenant_id) "
                    "SELECT ?, tag_id, value, ? FROM item_tags WHERE item_id = ?",
                    (pending_id, tenant_id, it["id"]),
                )
                conn.execute("DELETE FROM items WHERE id = ?", (it["id"],))
                moved_to_queue += 1
        conn.execute(
            "UPDATE boxes SET last_audited_at = CURRENT_TIMESTAMP WHERE id = ?", (box_id,)
        )
        conn.commit()
    target = "/queue" if moved_to_queue else f"/boxes/{box_id}"
    return RedirectResponse(target, status_code=303)


@app.post("/boxes/{box_id}/items")
async def add_item(
    request: Request,
    box_id: int,
    name: str = Form(...),
    notes: str = Form(""),
    tags: str = Form(""),
    photo: UploadFile = File(None),
):
    actor: Actor = request.state.actor
    try:
        dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    photo_name = save_photo(actor.tenant_id, photo)
    try:
        new_id = dao_items.create(
            actor, box_id,
            name=name, notes=notes,
            photo=photo_name, source_photo=photo_name,
        )
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if tags.strip():
        dao_tags.attach_to_item(actor, new_id, parse_tag_input(tags))
    return RedirectResponse(f"/boxes/{box_id}", status_code=303)


@app.post("/items/{item_id}/tags")
def add_item_tag(request: Request, item_id: int, tag: str = Form(...)):
    entries = parse_tag_input(tag)
    if not entries:
        raise HTTPException(400, "Tag required")
    actor: Actor = request.state.actor
    try:
        item = dao_items.get_by_id(actor, item_id)
        dao_tags.attach_to_item(actor, item_id, entries)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse(f"/boxes/{item['box_id']}#item-{item_id}", status_code=303)


@app.post("/items/{item_id}/tags/{tag_id}/delete")
def remove_item_tag(request: Request, item_id: int, tag_id: int):
    actor: Actor = request.state.actor
    try:
        box_id = dao_items.remove_tag(actor, item_id, tag_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse(f"/boxes/{box_id}#item-{item_id}", status_code=303)


@app.get("/boxes/{box_id}/preview", response_class=HTMLResponse)
def box_preview(request: Request, box_id: int):
    """Compact box summary for the floorplan tile-click modal — name,
    location, item count, a few thumbs, and a link to open the box."""
    actor: Actor = request.state.actor
    try:
        box = dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    all_items = dao_items.list_for_box(actor, box_id)
    # newest-first to match the previous ORDER BY DESC LIMIT 60.
    all_items_desc = list(reversed(all_items))
    items = all_items_desc[:60]
    item_count = len(all_items)
    return templates.TemplateResponse(
        request, "_floorplan_box_preview.html",
        {"box": box, "items": items, "item_count": item_count},
    )


@app.get("/items/{item_id}/preview", response_class=HTMLResponse)
def item_preview(request: Request, item_id: int):
    """HTML fragment rendering an item's detail card. Used by the search
    page to open a result in a modal instead of navigating away — the same
    actions (re-tag, move, replace photo, delete) work in place."""
    actor: Actor = request.state.actor
    try:
        item = dao_items.get_by_id(actor, item_id)
        box = dao_boxes.get_by_id(actor, item["box_id"])
    except NotFoundError:
        raise HTTPException(404)
    item = {**item, "box_name": box["name"]}
    tags = dao_items.list_tags_for_item(actor, item_id)
    other_boxes = sorted(
        ({"id": b["id"], "name": b["name"], "location": b["location"]}
         for b in dao_boxes.list_for_picker(actor)
         if b["id"] != item["box_id"]),
        key=lambda b: b["name"] or "",
    )
    return templates.TemplateResponse(
        request, "_search_item_modal.html",
        {"it": item, "tags": tags, "other_boxes": other_boxes},
    )


@app.get("/items/{item_id}/recrop", response_class=HTMLResponse)
def recrop_item(request: Request, item_id: int):
    actor: Actor = request.state.actor
    try:
        item = dao_items.get_by_id(actor, item_id)
    except NotFoundError:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request, "recrop.html", {"item": item}
    )


@app.post("/items/{item_id}/recrop")
def apply_recrop(
    request: Request,
    item_id: int,
    crop_y_min: str = Form(""),
    crop_x_min: str = Form(""),
    crop_y_max: str = Form(""),
    crop_x_max: str = Form(""),
    skip_crop: str = Form(""),
):
    actor: Actor = request.state.actor
    try:
        item = dao_items.get_for_recrop(actor, item_id)
    except NotFoundError:
        raise HTTPException(404)
    tenant_id = item["tenant_id"]
    source = item["source_photo"] or item["photo"]
    old_photo = item["photo"]

    if skip_crop.strip() == "1":
        # Undo crop — revert to full source image
        new_photo = source
    elif crop_y_min.strip() and crop_x_min.strip() and crop_y_max.strip() and crop_x_max.strip():
        bbox = (int(crop_y_min), int(crop_x_min), int(crop_y_max), int(crop_x_max))
        new_photo = crop_photo(tenant_id, source, bbox)
    else:
        # No change
        return RedirectResponse(f"/boxes/{item['box_id']}#item-{item_id}", status_code=303)

    try:
        dao_items.apply_recrop(actor, item_id, new_photo, source)
    except (NotFoundError, ForbiddenError):
        raise HTTPException(404)
    # Old crop file may now be orphaned
    if old_photo and old_photo != new_photo and old_photo != source:
        with db() as conn:
            _delete_upload_if_orphan(conn, tenant_id, old_photo)
    return RedirectResponse(f"/boxes/{item['box_id']}#item-{item_id}", status_code=303)


@app.post("/items/{item_id}/replace-photo")
async def replace_item_photo(request: Request, item_id: int, photo: UploadFile = File(...)):
    if not photo or not photo.filename:
        raise HTTPException(400, "Photo required")
    actor: Actor = request.state.actor
    try:
        item = dao_items.get_for_recrop(actor, item_id)
    except NotFoundError:
        raise HTTPException(404)
    tenant_id = item["tenant_id"]
    new_photo = save_photo(tenant_id, photo)
    try:
        result = dao_items.replace_photo(actor, item_id, new_photo)
    except (NotFoundError, ForbiddenError):
        raise HTTPException(404)
    with db() as conn:
        for old in {result["old_photo"], result["old_source"]}:
            if old:
                _delete_upload_if_orphan(conn, tenant_id, old)
    return RedirectResponse(f"/boxes/{result['box_id']}#item-{item_id}", status_code=303)


@app.post("/items/{item_id}/delete")
def delete_item(request: Request, item_id: int):
    actor: Actor = request.state.actor
    try:
        result = dao_items.delete(actor, item_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    with db() as conn:
        for photo_name in {result["photo"], result["source_photo"]}:
            if photo_name:
                _delete_upload_if_orphan(conn, actor.tenant_id, photo_name)
    return RedirectResponse(f"/boxes/{result['box_id']}", status_code=303)


_EXT_TO_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def _bytes_for_vision(tenant_id: int, photo_name: str) -> bytes:
    """Re-encode the saved photo into a JPEG with no EXIF segment before
    sending it to the vision model.

    save_photo_bytes already runs ImageOps.exif_transpose, but Gemini's
    behaviour with EXIF is undocumented — when a phone photo carries an
    orientation tag, the browser auto-rotates while Gemini decoded the raw
    landscape pixels and returned bboxes in that un-rotated coordinate
    space, so the overlay rendered 90° off from what the user sees.
    Stripping the EXIF segment entirely (info.pop + a fresh save without
    the `exif=` kwarg) means there's nothing left for the decoder to
    interpret either way: pixel orientation is the only source of truth.

    Falls back to raw plaintext bytes if PIL can't decode the file (test
    fixtures sometimes use synthetic JPEG headers PIL refuses)."""
    plaintext = _read_encrypted(tenant_id, photo_name)
    try:
        from PIL import Image, ImageOps
        import io as _io
        with Image.open(_io.BytesIO(plaintext)) as opened:
            rotated = ImageOps.exif_transpose(opened)
        if rotated.mode not in ("RGB", "L"):
            rotated = rotated.convert("RGB")
        rotated.info.pop("exif", None)
        buf = _io.BytesIO()
        rotated.save(buf, format="JPEG", quality=JPEG_QUALITY)
        return buf.getvalue()
    except Exception:
        return plaintext


def process_ingest_job(job_id: int, photo_name: str, tenant_id: int) -> None:
    """Background worker: vision pass → insert pending items → mark job done.
    Runs after the request scope closes, so it routes through the no-actor
    DAO entry points instead of going through Actor-gated mutations."""
    dao_ingest_jobs.mark_processing(job_id)
    try:
        image_bytes = _bytes_for_vision(tenant_id, photo_name)
        detected = vision.detect_items(image_bytes, media_type="image/jpeg")
        for item in detected:
            bbox = item.bbox or [None, None, None, None]
            dao_ingest_jobs.insert_pending_item(
                tenant_id,
                name=item.name,
                description=item.description,
                photo=photo_name,
                bbox=tuple(bbox),
            )
        dao_ingest_jobs.mark_done(job_id, len(detected))
    except Exception as e:
        dao_ingest_jobs.mark_failed(job_id, str(e))


@app.get("/ingest", response_class=HTMLResponse)
def ingest_form(request: Request):
    actor: Actor = request.state.actor
    jobs = dao_ingest_jobs.list_active(actor)
    fp = dao_ingest_jobs.fingerprint(actor)
    return templates.TemplateResponse(
        request, "ingest.html",
        {"jobs": jobs, "fingerprint": fp["fingerprint"]},
    )


@app.get("/ingest/state")
def ingest_state(request: Request):
    """Lightweight poll target so the ingest page can update its job list
    without a full meta-refresh — that one was nuking in-progress file
    picker selections and cancelling uploads mid-stream."""
    actor: Actor = request.state.actor
    return dao_ingest_jobs.fingerprint(actor)


@app.get("/ingest/jobs", response_class=HTMLResponse)
def ingest_jobs_fragment(request: Request):
    """HTML fragment of just the jobs list — the ingest page swaps this in
    when its fingerprint changes."""
    actor: Actor = request.state.actor
    return templates.TemplateResponse(
        request, "_ingest_jobs.html",
        {"jobs": dao_ingest_jobs.list_active(actor)},
    )


@app.post("/ingest")
async def ingest(
    request: Request,
    background_tasks: BackgroundTasks,
    photos: list[UploadFile] = File(...),
):
    valid = [p for p in photos if p and p.filename]
    if not valid:
        raise HTTPException(400, "Photo required")
    actor: Actor = request.state.actor

    for photo in valid:
        image_bytes = await photo.read()
        photo_name = save_photo_bytes(actor.tenant_id, image_bytes, photo.filename)
        try:
            job_id = dao_ingest_jobs.create(actor, photo_name)
        except ForbiddenError:
            raise HTTPException(403)
        background_tasks.add_task(process_ingest_job, job_id, photo_name, actor.tenant_id)

    return RedirectResponse("/ingest", status_code=303)


@app.post("/ingest/{job_id}/retry")
def ingest_retry(request: Request, background_tasks: BackgroundTasks, job_id: int):
    actor: Actor = request.state.actor
    try:
        row = dao_ingest_jobs.get_for_retry(actor, job_id)
    except NotFoundError:
        raise HTTPException(404, "Job not found or not failed")
    except ForbiddenError:
        raise HTTPException(403)
    dao_ingest_jobs.reset_to_pending(actor, job_id)
    background_tasks.add_task(
        process_ingest_job, job_id, row["photo"], row["tenant_id"] or actor.tenant_id,
    )
    return RedirectResponse("/ingest", status_code=303)


@app.post("/ingest/{job_id}/dismiss")
def ingest_dismiss(request: Request, job_id: int):
    actor: Actor = request.state.actor
    try:
        dao_ingest_jobs.dismiss(actor, job_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse("/ingest", status_code=303)


def _queue_view_context(actor: Actor) -> dict:
    """Load the data needed to render the queue page (or just its cards
    fragment).  Both /queue and /queue/items share the same shape so a
    real-time refresh produces markup identical to the SSR path."""
    pending = dao_pending.list_for_queue(actor)
    boxes = dao_boxes.list_for_picker(actor)
    all_tags = dao_tags.list_names(actor)

    # Group boxes for the <optgroup>'d picker: "Location · Room", or just
    # "Room" if no location, or "Unassigned" for boxes with neither.
    boxes_grouped: list[tuple[str, list]] = []
    current_label = object()
    current_group: list = []
    for b in boxes:
        if b["location_name"] and b["room_name"]:
            label = f"{b['location_name']} · {b['room_name']}"
        elif b["room_name"]:
            label = b["room_name"]
        elif b["location"]:  # legacy free-text fallback
            label = b["location"]
        else:
            label = "Unassigned"
        if label != current_label:
            current_group = []
            boxes_grouped.append((label, current_group))
            current_label = label
        current_group.append(b)

    return {
        "pending": pending,
        "boxes": boxes,
        "boxes_grouped": boxes_grouped,
        "fingerprint": dao_pending.fingerprint(actor),
        "all_tags": all_tags,
    }


@app.get("/queue", response_class=HTMLResponse)
def queue(request: Request):
    actor: Actor = request.state.actor
    return templates.TemplateResponse(request, "queue.html", _queue_view_context(actor))


@app.get("/queue/items", response_class=HTMLResponse)
def queue_items_fragment(request: Request):
    """HTML fragment of the pending-item cards only.

    The queue page polls this on /queue/state fingerprint changes and
    splices new cards in (and prunes vanished ones) without a full
    reload — earlier the page hard-reloaded on every fingerprint flip,
    which kept eating in-flight edits whenever a background ingest job
    finished or another tab touched the queue."""
    actor: Actor = request.state.actor
    return templates.TemplateResponse(
        request, "_queue_cards.html", _queue_view_context(actor),
    )


@app.post("/queue/{pending_id}/match")
def queue_match(request: Request, pending_id: int):
    actor: Actor = request.state.actor
    try:
        row = dao_pending.get_by_id(actor, pending_id)
    except NotFoundError:
        raise HTTPException(404)
    boxes = dao_boxes.list_for_picker(actor)

    suggestion = vision.suggest_box(row["name"], row["description"] or "", boxes)

    try:
        dao_pending.update_suggestion(
            actor, pending_id,
            suggested_box_id=suggestion.box_id if suggestion.match == "existing" else None,
            suggested_new_box_name=suggestion.new_box_name if suggestion.match == "new" else None,
            suggested_new_box_location=suggestion.new_box_location if suggestion.match == "new" else None,
            suggestion_reason=suggestion.reason,
        )
    except (NotFoundError, ForbiddenError):
        raise HTTPException(404)
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/{pending_id}/assign")
def queue_assign(
    request: Request,
    pending_id: int,
    box_id: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    tags: str = Form(""),
    crop_y_min: str = Form(""),
    crop_x_min: str = Form(""),
    crop_y_max: str = Form(""),
    crop_x_max: str = Form(""),
    skip_crop: str = Form(""),
):
    if not box_id.strip():
        raise HTTPException(400, "Pick a box")
    if not name.strip():
        raise HTTPException(400, "Name required")
    actor: Actor = request.state.actor

    try:
        row = dao_pending.get_for_assign(actor, pending_id)
    except NotFoundError:
        raise HTTPException(404)
    tenant_id = row["tenant_id"]
    target_box_id = int(box_id)
    try:
        dao_boxes.get_by_id(actor, target_box_id)
    except NotFoundError:
        raise HTTPException(400, "Unknown box")

    # Use manual crop coords if submitted, fall back to DB bbox, skip if cleared
    source_photo = row["photo"]
    photo = source_photo
    if skip_crop.strip() != "1":
        if crop_y_min.strip() and crop_x_min.strip() and crop_y_max.strip() and crop_x_max.strip():
            bbox = (int(crop_y_min), int(crop_x_min), int(crop_y_max), int(crop_x_max))
            photo = crop_photo(tenant_id, photo, bbox)
        elif photo and row["bbox_y_min"] is not None:
            bbox = (row["bbox_y_min"], row["bbox_x_min"], row["bbox_y_max"], row["bbox_x_max"])
            photo = crop_photo(tenant_id, photo, bbox)

    new_item_id = dao_items.create(
        actor, target_box_id,
        name=name, notes=description, photo=photo, source_photo=source_photo,
    )
    # Transfer tags from pending to the real item.  This is the one
    # cross-table copy that doesn't fit any single DAO method cleanly,
    # so we keep the SELECT/INSERT inline — both filters scope to the
    # actor's tenant.
    with db() as conn:
        conn.execute(
            "INSERT INTO item_tags (item_id, tag_id, value, tenant_id) "
            "SELECT ?, tag_id, value, ? FROM pending_item_tags "
            "WHERE pending_item_id = ? AND tenant_id = ?",
            (new_item_id, tenant_id, pending_id, tenant_id),
        )
        conn.commit()
    if tags.strip():
        dao_tags.attach_to_item(actor, new_item_id, parse_tag_input(tags))
    dao_pending.delete(actor, pending_id)
    return RedirectResponse("/queue", status_code=303)


@app.get("/queue/state")
def queue_state(request: Request):
    """Fingerprint for real-time polling — changes whenever queue content changes."""
    actor: Actor = request.state.actor
    return {"fingerprint": dao_pending.fingerprint(actor)}


@app.post("/queue/{pending_id}/delete")
def queue_delete(request: Request, pending_id: int):
    actor: Actor = request.state.actor
    try:
        result = dao_pending.delete(actor, pending_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if result["photo"]:
        with db() as conn:
            _delete_upload_if_orphan(conn, actor.tenant_id, result["photo"])
    return RedirectResponse("/queue", status_code=303)


SEARCH_PAGE_SIZE = 100


def _coerce_search_int(value: str) -> int | None:
    if not value or value in ("none", "0", "all"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _build_search_query(
    actor: Actor,
    q: str, tag: str, location_id: int | None, room_id: int | None,
    box_id: int | None, missing: bool, has_photo: bool,
) -> tuple[str, list]:
    """Compose the WHERE clause + params for the search query. Pulled out so
    both the listing query and the count/facet queries can share it.
    Always pins on i.tenant_id so a tenant can't see another's items
    even when they brute-force filter ids in the URL."""
    clauses = ["i.tenant_id = ?"]
    params: list = [actor.tenant_id]
    if q.strip():
        like = f"%{q.strip()}%"
        clauses.append("(i.name LIKE ? OR i.notes LIKE ?)")
        params.extend([like, like])
    if tag.strip():
        clauses.append(
            "i.id IN (SELECT it.item_id FROM item_tags it "
            "JOIN tags t ON t.id = it.tag_id "
            "WHERE t.name = ? AND it.tenant_id = ?)"
        )
        params.extend([tag.strip(), actor.tenant_id])
    if box_id is not None:
        clauses.append("b.id = ?")
        params.append(box_id)
    if room_id is not None:
        clauses.append("r.id = ?")
        params.append(room_id)
    if location_id is not None:
        clauses.append("l.id = ?")
        params.append(location_id)
    if missing:
        clauses.append("i.is_missing = 1")
    if has_photo:
        clauses.append("i.photo IS NOT NULL AND i.photo != ''")
    return " AND ".join(clauses), params


@app.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    q: str = "",
    tag: str = "",
    location_id: str = "",
    room_id: str = "",
    box_id: str = "",
    missing: str = "",
    has_photo: str = "",
    offset: int = 0,
):
    """Faceted search across all items.

    Supports filtering by free text, tag, location/room/box, missing-flag,
    and has-photo. Results are grouped by box and paginated server-side
    (LIMIT/OFFSET) so a stash with thousands of items still renders.
    Honors Accept: application/json so the client can use it as the API
    behind a "load more" button without re-rendering the chrome.
    """
    loc_id = _coerce_search_int(location_id)
    rm_id = _coerce_search_int(room_id)
    bx_id = _coerce_search_int(box_id)
    is_missing = bool(missing)
    has_photo_flag = bool(has_photo)
    offset = max(0, offset)
    actor: Actor = request.state.actor

    all_tags = dao_tags.list_names(actor)
    with db() as conn:
        all_locations = [
            dict(r) for r in conn.execute(
                "SELECT id, name FROM locations WHERE tenant_id = ? ORDER BY name",
                (actor.tenant_id,),
            ).fetchall()
        ]
        all_rooms = dao_rooms.list_for_picker(actor)
        all_boxes = [
            dict(r) for r in conn.execute(
                "SELECT b.id, b.name, "
                "       l.id AS location_id, l.name AS location_name, "
                "       r.id AS room_id, r.name AS room_name "
                "FROM boxes b "
                "LEFT JOIN rooms r ON r.id = b.room_id "
                "LEFT JOIN locations l ON l.id = r.location_id "
                "WHERE b.tenant_id = ? "
                "ORDER BY l.name IS NULL, l.name, r.name, b.name",
                (actor.tenant_id,),
            ).fetchall()
        ]

        where, params = _build_search_query(
            actor,
            q, tag, loc_id, rm_id, bx_id, is_missing, has_photo_flag,
        )
        common_join = (
            "FROM items i "
            "JOIN boxes b ON b.id = i.box_id "
            "LEFT JOIN rooms r ON r.id = b.room_id "
            "LEFT JOIN locations l ON l.id = r.location_id "
        )

        # Headline counts so the user knows the result size before pagination.
        totals = conn.execute(
            f"SELECT COUNT(DISTINCT i.id) AS items, COUNT(DISTINCT b.id) AS boxes "
            f"{common_join}WHERE {where}",
            params,
        ).fetchone()
        total_items = totals["items"]
        total_boxes = totals["boxes"]

        rows = conn.execute(
            f"SELECT i.id, i.name, i.notes, i.photo, i.is_missing, "
            f"       b.id AS box_id, b.name AS box_name, b.color AS box_color, "
            f"       r.id AS room_id, r.name AS room_name, r.color AS room_color, "
            f"       l.id AS location_id, l.name AS location_name "
            f"{common_join}WHERE {where} "
            f"ORDER BY l.name IS NULL, l.name, r.name, b.name, i.name "
            f"LIMIT ? OFFSET ?",
            [*params, SEARCH_PAGE_SIZE, offset],
        ).fetchall()

        # Pull tags per item in a single pass so the row template doesn't
        # round-trip per item.
        item_ids = [r["id"] for r in rows]
        tags_by_item: dict[int, list[dict]] = {}
        if item_ids:
            placeholders = ",".join("?" * len(item_ids))
            for r in conn.execute(
                f"SELECT it.item_id, t.id AS tag_id, t.name, it.value "
                f"FROM item_tags it JOIN tags t ON t.id = it.tag_id "
                f"WHERE it.item_id IN ({placeholders}) AND it.tenant_id = ? "
                f"ORDER BY t.name",
                [*item_ids, actor.tenant_id],
            ).fetchall():
                tags_by_item.setdefault(r["item_id"], []).append(dict(r))

    # Group server-side so the template renders one section per box without
    # a stateful loop in Jinja. Boxes are already ordered by location/room
    # in the SQL, so we just bucket on transitions.
    groups: list[dict] = []
    current_box_id = None
    for r in rows:
        if r["box_id"] != current_box_id:
            groups.append({
                "box_id": r["box_id"],
                "box_name": r["box_name"],
                "box_color": r["box_color"],
                "room_name": r["room_name"],
                "room_color": r["room_color"],
                "location_name": r["location_name"],
                "items": [],
            })
            current_box_id = r["box_id"]
        groups[-1]["items"].append({
            **dict(r),
            "tags": tags_by_item.get(r["id"], []),
        })

    page_loaded = offset + len(rows)
    has_more = page_loaded < total_items

    if "application/json" in request.headers.get("accept", ""):
        # JSON response for the "Load more" path — just the new groups +
        # whether there's more behind them. Wrap in JSONResponse so the
        # endpoint's response_class=HTMLResponse default doesn't try to
        # render the dict as HTML.
        from fastapi.responses import JSONResponse
        return JSONResponse({
            "groups": groups,
            "total_items": total_items,
            "total_boxes": total_boxes,
            "page_loaded": page_loaded,
            "has_more": has_more,
            "offset": offset,
        })

    # Reflect the current filter state so the template can render filter
    # chips and pre-select dropdowns.
    return templates.TemplateResponse(
        request, "search.html",
        {
            "q": q,
            "tag": tag,
            "location_id": loc_id,
            "room_id": rm_id,
            "box_id": bx_id,
            "missing": is_missing,
            "has_photo": has_photo_flag,
            "groups": groups,
            "total_items": total_items,
            "total_boxes": total_boxes,
            "page_loaded": page_loaded,
            "has_more": has_more,
            "page_size": SEARCH_PAGE_SIZE,
            "all_tags": all_tags,
            "all_locations": all_locations,
            "all_rooms": all_rooms,
            "all_boxes": all_boxes,
        },
    )


@app.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request):
    actor: Actor = request.state.actor
    # ``use_count`` here is the template's "item_count" — rename in the
    # template if/when this loses its single caller, but for now keep
    # the dict shape stable.
    tags = [
        {"id": t["id"], "name": t["name"], "item_count": t["use_count"]}
        for t in dao_tags.list_with_counts(actor)
    ]
    return templates.TemplateResponse(request, "tags.html", {"tags": tags})


@app.get("/tags/autocomplete")
def tags_autocomplete(request: Request, q: str = ""):
    actor: Actor = request.state.actor
    names = dao_tags.list_names(actor)
    if q:
        ql = q.lower()
        # Same prefix-match LIKE 'q%' semantic, capped at 20.
        return [n for n in names if n.lower().startswith(ql)][:20]
    return names[:50]


def _box_art_bytes(box_row) -> bytes | None:
    """Decrypt a box's background art (if any) for embedding in a label.
    Returns None when there's no art configured or the file is missing /
    won't decrypt (rotation gone wrong, etc.) — labels still render
    cleanly without art."""
    art = box_row["background_art"] if "background_art" in box_row.keys() else None
    tenant_id = box_row["tenant_id"] if "tenant_id" in box_row.keys() else None
    if not art or tenant_id is None:
        return None
    p = _tenant_file(tenant_id, art)
    if not p.exists():
        return None
    try:
        return _decrypt_for(tenant_id, p.read_bytes())
    except Exception:
        return None


def _attach_art_bytes(box_dict: dict) -> dict:
    """Mutate a box dict in place to add `art_bytes` for the label
    renderers, then return it for chaining."""
    box_dict["art_bytes"] = _box_art_bytes(box_dict)
    return box_dict


@app.get("/boxes/{box_id}/label.svg")
def box_label_svg(request: Request, box_id: int):
    actor: Actor = request.state.actor
    try:
        box = dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    svg = labels.render_label_svg(
        box["id"], box["name"], box["notes"] or "", PUBLIC_URL,
        background_art=_box_art_bytes(box),
    )
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Content-Disposition": f'attachment; filename="box-{box_id}-label.svg"'},
    )


def _selected_boxes(conn, actor: Actor, box_ids_raw: list[str]) -> list:
    """Return rows for the selected boxes, or all boxes in the actor's
    tenant if no selection given.  Ordering matches the labels page
    (alpha) so printed sheets are predictable."""
    if box_ids_raw:
        placeholders = ",".join("?" * len(box_ids_raw))
        return conn.execute(
            f"SELECT id, name, notes, background_art, tenant_id FROM boxes "
            f"WHERE id IN ({placeholders}) AND tenant_id = ? ORDER BY name",
            [*[int(b) for b in box_ids_raw], actor.tenant_id],
        ).fetchall()
    return conn.execute(
        "SELECT id, name, notes, background_art, tenant_id FROM boxes "
        "WHERE tenant_id = ? ORDER BY name",
        (actor.tenant_id,),
    ).fetchall()


@app.get("/labels", response_class=HTMLResponse)
def labels_page(request: Request):
    actor: Actor = request.state.actor
    with db() as conn:
        boxes = conn.execute(
            "SELECT * FROM boxes WHERE tenant_id = ? ORDER BY name",
            (actor.tenant_id,),
        ).fetchall()
    return templates.TemplateResponse(
        request, "labels.html",
        {
            "boxes": boxes,
            "labels_per_page": labels.LABELS_PER_PAGE,
            "art_enabled": bool(os.environ.get("GEMINI_API_KEY")),
        },
    )


@app.get("/labels/sheet.svg")
def labels_sheet(request: Request):
    actor: Actor = request.state.actor
    box_ids_raw = request.query_params.getlist("box_ids")
    with db() as conn:
        boxes = _selected_boxes(conn, actor, box_ids_raw)
    payload = [_attach_art_bytes(dict(b)) for b in boxes]
    svg = labels.render_sheet_svg(payload, PUBLIC_URL)
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Content-Disposition": 'attachment; filename="stash-labels.svg"'},
    )


@app.get("/labels/sheet.pdf")
def labels_sheet_pdf(request: Request):
    """Multi-page vector PDF — fits Avery label sheets directly and is the
    Cricut/print-ready artifact. Each sheet is its own page."""
    actor: Actor = request.state.actor
    box_ids_raw = request.query_params.getlist("box_ids")
    with db() as conn:
        boxes = _selected_boxes(conn, actor, box_ids_raw)
    payload = [_attach_art_bytes(dict(b)) for b in boxes]
    try:
        pdf_bytes = labels.render_sheet_pdf(payload, PUBLIC_URL)
    except ImportError:
        # cairosvg + pypdf aren't installed in this environment (e.g. local
        # dev without the deps yet). Fall back to a clear error rather than
        # a 500.
        raise HTTPException(
            501, "PDF export requires cairosvg + pypdf — install them or "
                 "use the SVG / Print buttons.",
        )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="stash-labels.pdf"'},
    )


@app.get("/labels/print", response_class=HTMLResponse)
def labels_print(request: Request):
    """Browser-printable preview of all selected labels, paginated via CSS so
    Cmd/Ctrl+P produces real multi-page output. Single-sheet SVGs get wrapped
    in page-break-after divs — no PDF library needed for clean printing."""
    actor: Actor = request.state.actor
    box_ids_raw = request.query_params.getlist("box_ids")
    with db() as conn:
        boxes = [_attach_art_bytes(dict(b)) for b in _selected_boxes(conn, actor, box_ids_raw)]
    pages = []
    for chunk_start in range(0, max(len(boxes), 1), labels.LABELS_PER_PAGE):
        chunk = boxes[chunk_start:chunk_start + labels.LABELS_PER_PAGE]
        pages.append(labels.render_single_sheet_svg(chunk, PUBLIC_URL))
    return templates.TemplateResponse(
        request, "labels_print.html",
        {"sheet_svgs": pages, "label_count": len(boxes)},
    )


def _wants_json(request: Request) -> bool:
    """The labels page calls these endpoints from fetch() with Accept: application/json
    so it can re-render in place; the form-post fallback gets a 303 redirect."""
    return "application/json" in request.headers.get("accept", "")


def _safe_internal_redirect(target: str, default: str = "/labels") -> str:
    """Reject open-redirect targets — only honor relative paths beneath this app."""
    if not target.startswith("/") or target.startswith("//"):
        return default
    return target


@app.post("/boxes/{box_id}/generate-art")
def generate_box_art(
    request: Request,
    box_id: int,
    next_url: str = Form("/labels"),
):
    """Synchronously generate label background art via Nano Banana 2.

    Synchronous because the user is waiting on it from the labels page; the
    model takes ~10-20s. Each call independently grabs items + a few photos
    so the prompt is grounded in actual contents instead of just the box
    name. The old image, if any, is cleaned up only after the new one writes
    successfully so a failed generation doesn't strand the box without art."""
    actor: Actor = request.state.actor
    try:
        box = dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    tenant_id = box["tenant_id"]
    # Newest 12 items, by created_at DESC, for the prompt.  list_for_box
    # returns oldest-first so we reverse + slice here.
    items = list(reversed(dao_items.list_for_box(actor, box_id)))[:12]

    # Up to 3 small photo references for the multimodal prompt.  Read +
    # decrypt; mime is always image/jpeg post-phase-2 because every
    # upload is re-encoded as JPEG.
    photo_refs: list[tuple[bytes, str]] = []
    for it in items:
        if not it.get("photo"):
            continue
        p = _tenant_file(tenant_id, it["photo"])
        if not p.exists():
            continue
        try:
            photo_refs.append((_decrypt_for(tenant_id, p.read_bytes()), "image/jpeg"))
        except Exception:
            continue
        if len(photo_refs) >= 3:
            break

    try:
        image_bytes = vision.generate_label_art(
            box["name"], box["notes"] or "",
            items=items, item_photos=photo_refs,
        )
    except Exception as e:
        if _wants_json(request):
            raise HTTPException(502, f"Art generation failed: {e}")
        raise HTTPException(502, f"Art generation failed: {e}")

    new_name = f"art-{secrets.token_hex(8)}.jpg"
    _write_encrypted(tenant_id, new_name, image_bytes)

    try:
        old = dao_boxes.set_background_art(actor, box_id, new_name)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if old and old != new_name:
        with db() as conn:
            _delete_upload_if_orphan(conn, tenant_id, old)

    if _wants_json(request):
        return {"ok": True, "box_id": box_id, "background_art": new_name}
    return RedirectResponse(
        _safe_internal_redirect(next_url), status_code=303,
    )


@app.post("/boxes/{box_id}/clear-art")
def clear_box_art(
    request: Request,
    box_id: int,
    next_url: str = Form("/labels"),
):
    """Drop the generated background art for a box."""
    actor: Actor = request.state.actor
    try:
        old = dao_boxes.clear_background_art(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if old:
        with db() as conn:
            _delete_upload_if_orphan(conn, actor.tenant_id, old)

    if _wants_json(request):
        return {"ok": True, "box_id": box_id, "background_art": None}
    return RedirectResponse(
        _safe_internal_redirect(next_url), status_code=303,
    )


@app.post("/boxes/{box_id}/delete")
def delete_box(request: Request, box_id: int, confirm: str = Form(...)):
    actor: Actor = request.state.actor
    try:
        box = dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    if confirm.strip() != box["name"]:
        raise HTTPException(400, "Type the box name to confirm deletion")
    try:
        result = dao_boxes.delete(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    tenant_id = result["tenant_id"]
    for p in result["photos"]:
        try:
            _tenant_file(tenant_id, p).unlink()
        except FileNotFoundError:
            pass
        try:
            _tenant_thumb(tenant_id, p).unlink()
        except FileNotFoundError:
            pass
    return RedirectResponse("/", status_code=303)


# ── Locations + rooms (where stuff actually lives) ─────────────────────────

# A small fixed palette so distinct rooms get visually separable colors on the
# floorplan. Cycled by index when assigning a fresh room.
_ROOM_COLORS = [
    "#4ade80", "#60a5fa", "#fbbf24", "#f87171",
    "#a78bfa", "#34d399", "#fb923c", "#22d3ee",
    "#f472b6", "#facc15", "#94a3b8", "#fde047",
]


def _next_room_color(conn, location_id: int) -> str:
    """Pick the next unused color in the palette for a location, cycling if all
    are taken. Keeps rooms inside a single floorplan visually distinct."""
    used = {
        r["color"] for r in conn.execute(
            "SELECT color FROM rooms WHERE location_id = ? AND color IS NOT NULL",
            (location_id,),
        ).fetchall()
    }
    for c in _ROOM_COLORS:
        if c not in used:
            return c
    n = conn.execute(
        "SELECT COUNT(*) FROM rooms WHERE location_id = ?", (location_id,),
    ).fetchone()[0]
    return _ROOM_COLORS[n % len(_ROOM_COLORS)]


@app.get("/locations", response_class=HTMLResponse)
def locations_index(request: Request):
    actor: Actor = request.state.actor
    locs = dao_locations.list_with_room_counts(actor)
    return templates.TemplateResponse(
        request, "locations.html", {"locations": locs},
    )


@app.post("/locations")
def create_location(request: Request, name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    actor: Actor = request.state.actor
    try:
        location_id = dao_locations.create(actor, name)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse(f"/locations/{location_id}", status_code=303)


@app.get("/locations/{location_id}", response_class=HTMLResponse)
def location_detail(
    request: Request,
    location_id: int,
    edit: str = "",
    floor: int | None = None,
):
    actor: Actor = request.state.actor
    try:
        loc = dao_locations.get_by_id(actor, location_id)
    except NotFoundError:
        raise HTTPException(404)
    floors = dao_floors.list_for_location(actor, location_id)

    # Default the current floor: explicit ?floor= wins, else the first one.
    current_floor = None
    if floors:
        if floor is not None:
            current_floor = next((f for f in floors if f["id"] == floor), floors[0])
        else:
            current_floor = floors[0]

    rooms = []
    with db() as conn:
        if current_floor:
            rooms = [dict(r) for r in conn.execute(
                "SELECT r.*, "
                "       (SELECT COUNT(*) FROM boxes "
                "         WHERE room_id = r.id AND tenant_id = ?) AS box_count "
                "FROM rooms r "
                "WHERE r.floor_id = ? AND r.tenant_id = ? "
                "ORDER BY r.name",
                (actor.tenant_id, current_floor["id"], actor.tenant_id),
            ).fetchall()]
            # Pull every box on this floor in one shot, then bucket by room
            # so each room rect can render its boxes as tiles inside it.
            box_rows = conn.execute(
                "SELECT b.id, b.name, b.room_id, b.color, "
                "       b.created_at, b.last_audited_at, "
                "       (SELECT COUNT(*) FROM items "
                "         WHERE box_id = b.id AND tenant_id = ?) AS item_count "
                "FROM boxes b "
                "JOIN rooms r ON r.id = b.room_id "
                "WHERE r.floor_id = ? AND b.tenant_id = ? "
                "ORDER BY b.name",
                (actor.tenant_id, current_floor["id"], actor.tenant_id),
            ).fetchall()
            # Items (with photo) for the high-LOD tile mosaic. Pull id +
            # name so the rendered img can carry both — needed for the
            # hover tooltip and the item drag-and-drop between boxes.
            item_rows = conn.execute(
                "SELECT i.id AS item_id, i.box_id, i.name AS item_name, i.photo "
                "FROM items i "
                "JOIN boxes b ON b.id = i.box_id "
                "JOIN rooms r ON r.id = b.room_id "
                "WHERE r.floor_id = ? AND i.photo IS NOT NULL "
                "  AND i.tenant_id = ? "
                "ORDER BY i.box_id, i.created_at DESC",
                (current_floor["id"], actor.tenant_id),
            ).fetchall()
            # Cap at 64 per box (8x8 grid worth) — enough headroom that even
            # heavily-photographed boxes show every item, but bounded so a
            # pathological 1000-item box doesn't bloat the page payload.
            mosaic_by_box: dict[int, list] = {}
            for it in item_rows:
                lst = mosaic_by_box.setdefault(it["box_id"], [])
                if len(lst) < 64:
                    lst.append({
                        "item_id": it["item_id"],
                        "name": it["item_name"],
                        "photo": it["photo"],
                    })
            import math as _math
            boxes_by_room: dict[int, list] = {}
            for b in box_rows:
                d = dict(b)
                mosaic = mosaic_by_box.get(b["id"], [])
                d["mosaic"] = mosaic
                # Square-ish grid: ⌈√N⌉ columns. With auto-flow rows the
                # mosaic packs into a near-square so each cell stays as
                # large as possible at the deepest zoom tier.
                d["mosaic_cols"] = max(1, _math.ceil(_math.sqrt(len(mosaic)))) if mosaic else 1
                boxes_by_room.setdefault(b["room_id"], []).append(d)
            for r in rooms:
                rb = boxes_by_room.get(r["id"], [])
                r["boxes"] = rb
                # Same square-pack idea as the mosaic: pick a column count
                # that lets N boxes fill the room cleanly. 1 box → 1 col
                # → fills the room; 4 boxes → 2x2; 12 boxes → 4x3-ish.
                r["box_cols"] = max(1, _math.ceil(_math.sqrt(len(rb)))) if rb else 1

        # Rooms with no floor (e.g. from the legacy text-location migration)
        # surface separately so the user can clean them up.
        unassigned = [dict(r) for r in conn.execute(
            "SELECT r.*, "
            "       (SELECT COUNT(*) FROM boxes "
            "         WHERE room_id = r.id AND tenant_id = ?) AS box_count "
            "FROM rooms r "
            "WHERE r.location_id = ? AND r.floor_id IS NULL AND r.tenant_id = ? "
            "ORDER BY r.name",
            (actor.tenant_id, location_id, actor.tenant_id),
        ).fetchall()]
    return templates.TemplateResponse(
        request, "location.html",
        {
            "location": loc,
            "floors": floors,
            "current_floor": current_floor,
            "rooms": rooms,
            "unassigned_rooms": unassigned,
            "edit_mode": edit == "1",
            "room_palette": _ROOM_COLORS,
        },
    )


@app.post("/locations/{location_id}")
def edit_location(request: Request, location_id: int, name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    actor: Actor = request.state.actor
    try:
        dao_locations.rename(actor, location_id, name)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse(f"/locations/{location_id}", status_code=303)


@app.post("/locations/{location_id}/delete")
def delete_location(request: Request, location_id: int, confirm: str = Form(...)):
    actor: Actor = request.state.actor
    try:
        result = dao_locations.delete(actor, location_id, confirm.strip())
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError as exc:
        # The DAO raises ForbiddenError for both role denial AND for
        # the type-the-name confirm mismatch — distinguish by message
        # so the type-the-name path returns 400 (a user error) rather
        # than 403 (an authz error).
        if "type the location name" in str(exc).lower():
            raise HTTPException(400, "Type the location name to confirm deletion")
        raise HTTPException(403)
    if result.get("floorplan"):
        with db() as conn:
            _delete_upload_if_orphan(conn, actor.tenant_id, result["floorplan"])
    return RedirectResponse("/locations", status_code=303)


@app.post("/locations/{location_id}/floors")
def create_floor(request: Request, location_id: int, name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    actor: Actor = request.state.actor
    try:
        floor_id = dao_floors.create(actor, location_id, name)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse(
        f"/locations/{location_id}?floor={floor_id}&edit=1", status_code=303,
    )


@app.post("/floors/{floor_id}")
def edit_floor(request: Request, floor_id: int, name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    actor: Actor = request.state.actor
    try:
        location_id = dao_floors.rename(actor, floor_id, name)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse(
        f"/locations/{location_id}?floor={floor_id}", status_code=303,
    )


@app.post("/floors/{floor_id}/floorplan")
async def upload_floor_floorplan(request: Request, floor_id: int, image: UploadFile = File(...)):
    if not image or not image.filename:
        raise HTTPException(400, "Image required")
    actor: Actor = request.state.actor
    try:
        floor = dao_floors.get_by_id(actor, floor_id)
    except NotFoundError:
        raise HTTPException(404)
    new_name = save_photo_bytes(actor.tenant_id, await image.read(), image.filename)
    try:
        result = dao_floors.update_floorplan(actor, floor_id, new_name)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    old = result["old_floorplan"]
    if old and old != new_name:
        with db() as conn:
            _delete_upload_if_orphan(conn, actor.tenant_id, old)
    return RedirectResponse(
        f"/locations/{floor['location_id']}?floor={floor_id}&edit=1", status_code=303,
    )


@app.post("/floors/{floor_id}/delete")
def delete_floor(request: Request, floor_id: int):
    actor: Actor = request.state.actor
    try:
        result = dao_floors.delete(actor, floor_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if result.get("floorplan"):
        with db() as conn:
            _delete_upload_if_orphan(conn, actor.tenant_id, result["floorplan"])
    return RedirectResponse(f"/locations/{result['location_id']}", status_code=303)


@app.post("/floors/{floor_id}/rooms")
def create_room(
    request: Request,
    floor_id: int,
    name: str = Form(...),
    x: float = Form(0), y: float = Form(0),
    w: float = Form(0), h: float = Form(0),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    actor: Actor = request.state.actor
    try:
        floor = dao_floors.get_by_id(actor, floor_id)
    except NotFoundError:
        raise HTTPException(404)
    with db() as conn:
        color = _next_room_color(conn, floor["location_id"])
    try:
        room_id = dao_rooms.create(
            actor, floor_id, name,
            x=_clamp01(x), y=_clamp01(y), w=_clamp01(w), h=_clamp01(h),
            color=color,
        )
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "id": room_id, "color": color, "name": name}
    return RedirectResponse(
        f"/locations/{floor['location_id']}?floor={floor_id}&edit=1", status_code=303,
    )


@app.post("/rooms/{room_id}")
def edit_room(
    request: Request,
    room_id: int,
    name: str = Form(...),
    x: float = Form(0), y: float = Form(0),
    w: float = Form(0), h: float = Form(0),
    color: str = Form(""),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    color_val = color.strip() if color and color.strip() in _ROOM_COLORS else None
    actor: Actor = request.state.actor
    try:
        row = dao_rooms.get_with_location(actor, room_id)
    except NotFoundError:
        raise HTTPException(404)
    new_color = color_val if color_val is not None else row["color"]
    try:
        dao_rooms.update(
            actor, room_id,
            name=name,
            x=_clamp01(x), y=_clamp01(y), w=_clamp01(w), h=_clamp01(h),
            color=new_color,
        )
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "color": new_color}
    target = f"/locations/{row['location_id']}?edit=1"
    if row["floor_id"]:
        target = f"/locations/{row['location_id']}?floor={row['floor_id']}&edit=1"
    return RedirectResponse(target, status_code=303)


@app.post("/rooms/{room_id}/delete")
def delete_room(request: Request, room_id: int):
    actor: Actor = request.state.actor
    try:
        result = dao_rooms.delete(actor, room_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True}
    target = f"/locations/{result['location_id']}?edit=1"
    if result["floor_id"]:
        target = f"/locations/{result['location_id']}?floor={result['floor_id']}&edit=1"
    return RedirectResponse(target, status_code=303)


def _clamp01(v: float) -> float:
    """Floorplan coordinates are stored as fractions of the image; clamp so a
    drag that overshoots the canvas doesn't end up with negative or >1 values."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


@app.get("/rooms/{room_id}/boxes", response_class=HTMLResponse)
def room_boxes(request: Request, room_id: int):
    """Boxes assigned to a single room — used as the click-through target from
    the floorplan view. Renders with the same card+thumb-strip presentation
    as the main /boxes index so the user gets parity."""
    actor: Actor = request.state.actor
    try:
        room = dao_rooms.get_with_location(actor, room_id)
    except NotFoundError:
        raise HTTPException(404)
    boxes = dao_boxes.list_for_room(actor, room_id)
    thumbs = dao_items.list_recent_photos_for_room(actor, room_id, limit_per_box=5)
    return templates.TemplateResponse(
        request, "room_boxes.html",
        {"room": room, "boxes": boxes, "thumbs": thumbs},
    )


def _referenced_uploads() -> set[tuple[int, str]]:
    """All upload `(tenant_id, filename)` pairs referenced by any row in
    the DB, plus their derived thumbnail companions so the orphan sweep
    keeps both halves.

    This is the single source of truth for both /maintenance/cleanup
    (orphan deletion) and /maintenance/export (backup zip).  Any new
    file-bearing column MUST be added here — otherwise a fresh feature
    will silently leak files on cleanup AND drop them from backups.

    DB tables themselves are captured by zipping the whole stash.db, so
    DB-only additions (new tables, new non-file columns) need nothing.

    The set is keyed on (tenant_id, filename) since after phase 2 every
    upload lives under UPLOAD_DIR/{tenant_id}/{name} — same filename
    can legitimately exist in two tenants without collision."""
    refs: set[tuple[int, str]] = set()
    with db() as conn:
        for sql in (
            "SELECT tenant_id, photo FROM items WHERE photo IS NOT NULL",
            "SELECT tenant_id, source_photo FROM items WHERE source_photo IS NOT NULL",
            "SELECT tenant_id, photo FROM pending_items WHERE photo IS NOT NULL",
            "SELECT tenant_id, photo FROM ingest_jobs WHERE photo IS NOT NULL",
            "SELECT tenant_id, background_art FROM boxes WHERE background_art IS NOT NULL",
            "SELECT tenant_id, floorplan FROM floors WHERE floorplan IS NOT NULL",
            "SELECT tenant_id, floorplan FROM locations WHERE floorplan IS NOT NULL",
        ):
            for tid, name in conn.execute(sql).fetchall():
                if tid is None or not name:
                    continue
                refs.add((tid, name))
    # Thumb companions follow their source — same tenant, derived name.
    for tid, name in list(refs):
        refs.add((tid, _thumb_path(tid, name).name))
    return refs


@app.get("/maintenance", response_class=HTMLResponse)
def maintenance(request: Request, cleaned: str = "", update: str = "", imported: str = ""):
    actor: Actor = request.state.actor
    with db() as conn:
        item_count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        box_count = conn.execute("SELECT COUNT(*) FROM boxes").fetchone()[0]
        tenant_name = None
        members = []
        if actor.tenant_id is not None:
            row = conn.execute(
                "SELECT name FROM tenants WHERE id = ?", (actor.tenant_id,),
            ).fetchone()
            tenant_name = row["name"] if row else None
            members = [
                dict(r) for r in conn.execute(
                    "SELECT email, role FROM tenant_members "
                    "WHERE tenant_id = ? ORDER BY joined_at, email",
                    (actor.tenant_id,),
                ).fetchall()
            ]
    on_disk = sum(1 for _ in UPLOAD_DIR.iterdir()) if UPLOAD_DIR.exists() else 0
    referenced = len(_referenced_uploads())
    return templates.TemplateResponse(
        request, "maintenance.html",
        {
            "item_count": item_count, "box_count": box_count,
            "files_on_disk": on_disk, "files_referenced": referenced,
            "orphan_count": max(0, on_disk - referenced),
            "cleaned": cleaned,
            "imported": imported,
            "version": VERSION,
            "git_sha": GIT_SHA[:7] if GIT_SHA else "",
            "update_enabled": bool(WATCHTOWER_URL),
            "update_status": update,
            "changelog_html": CHANGELOG_HTML,
            # Access panel data: the active tenant + its members, replacing
            # the old global STASH_ALLOWED_EMAILS view.  This whole card
            # moves to /usage in roadmap step 13; until then we keep it on
            # /maintenance with tenant-scoped data so the live user has
            # parity with the old behaviour.
            "tenant_name": tenant_name,
            "tenant_members": members,
            "current_email": actor.email,
            "current_role": actor.role,
            "is_operator": actor.is_operator,
        },
    )


@app.post("/maintenance/update")
def maintenance_update(background: BackgroundTasks):
    if not WATCHTOWER_URL:
        return RedirectResponse("/maintenance?update=disabled", status_code=303)
    background.add_task(_trigger_watchtower_update)
    return RedirectResponse("/maintenance?update=triggered", status_code=303)


@app.get("/maintenance/version")
def maintenance_version():
    """Lightweight probe for client-side polling. The maintenance page snapshots
    the version on load and watches this endpoint for a change, which signals
    that watchtower has finished restarting the container with a new image."""
    return {
        "version": VERSION,
        "git_sha": GIT_SHA[:7] if GIT_SHA else "",
    }


@app.post("/maintenance/cleanup")
def maintenance_cleanup():
    refs = _referenced_uploads()
    cleaned = 0
    if UPLOAD_DIR.exists():
        # Walk per-tenant subdirectories — every upload after phase 2
        # lives at UPLOAD_DIR/{tenant_id}/{name}.  Anything in the flat
        # root is either a leftover .tmp from an interrupted write or a
        # pre-migration orphan; both get cleaned.
        for entry in UPLOAD_DIR.iterdir():
            if entry.is_dir() and entry.name.isdigit():
                tid = int(entry.name)
                for path in entry.iterdir():
                    if path.is_file() and (tid, path.name) not in refs:
                        try:
                            path.unlink()
                            cleaned += 1
                        except FileNotFoundError:
                            pass
            elif entry.is_file():
                # Stray file in the flat root (pre-migration leftover or
                # broken write).  Always orphan in the post-phase-2
                # world.
                try:
                    entry.unlink()
                    cleaned += 1
                except FileNotFoundError:
                    pass
    return RedirectResponse(f"/maintenance?cleaned={cleaned}", status_code=303)


@app.get("/maintenance/export")
def maintenance_export():
    """Stream a zip of stash.db + every upload file still referenced.

    Files are written into the zip under their on-disk relative path
    (`uploads/{tenant_id}/{name}`).  The blobs are stored as ciphertext —
    the zip is a coherent snapshot of the on-disk state, not a cleartext
    dump.  Restoring this zip without the matching STASH_KEK is useless,
    by design (see spec § "Encryption at rest")."""
    import io as _io
    import zipfile
    from datetime import datetime

    # Drain the WAL into main.db so the zip captures every committed
    # write. In WAL mode, recent UPDATEs can sit in the -wal sidecar
    # file until checkpointed; zipping just stash.db without this would
    # ship a snapshot that's missing the latest changes (and the next
    # restore would silently roll the user back).
    with db() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    refs = _referenced_uploads()
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if DB_PATH.exists():
            zf.write(DB_PATH, arcname="stash.db")
        for tid, name in sorted(refs):
            p = _tenant_file(tid, name)
            if p.exists():
                zf.write(p, arcname=f"uploads/{tid}/{name}")
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="stash-backup-{stamp}.zip"'},
    )


_SQLITE_MAGIC = b"SQLite format 3\x00"
_ZIP_MAGIC = b"PK\x03\x04"

# Backup restores carry every referenced photo; a 2GB ceiling keeps DoS-on-disk
# bounded while comfortably covering realistic stashes. Caddy is configured to
# permit the same on /maintenance/import.
MAX_IMPORT_BYTES = 2 * 1024 * 1024 * 1024


def _validate_sqlite_file(path: Path) -> None:
    """Raise HTTPException unless `path` is a SQLite DB with a `boxes` table."""
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='boxes'"
            ).fetchone()
            if not row:
                raise HTTPException(400, "Database is missing the boxes table — not a stash backup")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise HTTPException(400, f"Database failed integrity check: {integrity[0] if integrity else 'unknown'}")
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        raise HTTPException(400, f"Not a valid SQLite database: {e}")


def _backup_current_db() -> Path | None:
    """Copy the live DB to stash.db.bak-<timestamp> so a bad import is recoverable."""
    if not DB_PATH.exists():
        return None
    from datetime import datetime
    import shutil
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB_PATH.with_name(f"{DB_PATH.name}.bak-{stamp}")
    shutil.copy2(DB_PATH, backup)
    return backup


def _replace_db_from_path(src_db: Path) -> None:
    """Validate the SQLite file at `src_db` and copy it into the live DB location.

    Uses SQLite's online backup API rather than `os.replace` — on Windows the
    live DB file is held open by any active connection, which makes a rename
    fail with `PermissionError`. `Connection.backup()` swaps page contents
    through the SQLite library and is safe even with concurrent readers.

    With WAL mode enabled on the live DB, pages written by the wipe-step
    transactions (DELETE FROMs etc.) live in the WAL until checkpointed.
    `backup()` overwrites the main DB file's contents from the source but
    doesn't sync the WAL — so subsequent reads can see stale WAL pages
    layered over the freshly-restored main pages. A FULL checkpoint +
    TRUNCATE before close forces the WAL to drain into main and resets
    its size to zero, so the next connection opens a coherent file."""
    with open(src_db, "rb") as f:
        header = f.read(len(_SQLITE_MAGIC))
    if not header.startswith(_SQLITE_MAGIC):
        raise HTTPException(400, "File is not a SQLite database")
    _validate_sqlite_file(src_db)
    _backup_current_db()
    src = sqlite3.connect(src_db)
    dst = sqlite3.connect(DB_PATH)
    try:
        src.backup(dst)
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        src.close()
        dst.close()
    migrate_db()


def _extract_db_member_to_path(zf, member: str, dst: Path) -> None:
    """Stream a zip member to disk in 1MB chunks (no full decompress in memory)."""
    import shutil
    with zf.open(member) as src, open(dst, "wb") as out:
        shutil.copyfileobj(src, out, length=1024 * 1024)


def _restore_uploads_from_zip(zf) -> int:
    """Extract upload entries into the per-tenant directory layout.

    Accepts both shapes the export has used:
    * Phase-2+ zips: ``uploads/{tenant_id}/{name}`` (encrypted blob).
    * Pre-phase-2 legacy zips: ``uploads/{name}`` (cleartext); these
      get dropped into the flat root so the migration step can
      relocate + encrypt them on next startup.

    Skips entries with hostile names (path traversal, suspicious
    chars).  Returns count restored."""
    import shutil
    upload_root = UPLOAD_DIR.resolve()
    count = 0
    for name in zf.namelist():
        if not name.startswith("uploads/") or name.endswith("/"):
            continue
        rel = name[len("uploads/"):]
        if ".." in rel or "\\" in rel:
            continue

        parts = rel.split("/", 1)
        if len(parts) == 2 and parts[0].isdigit():
            # Phase-2+ layout: uploads/{tenant_id}/{name}.
            tid_segment, base = parts
            if not _UPLOAD_NAME_RE.match(base):
                continue
            target_dir = UPLOAD_DIR / tid_segment
            target_dir.mkdir(parents=True, exist_ok=True)
            target = (target_dir / base).resolve()
        else:
            # Legacy flat layout; restore into the root so the next
            # migrate_db relocates + encrypts.
            base = rel
            if "/" in base or not _UPLOAD_NAME_RE.match(base):
                continue
            target = (UPLOAD_DIR / base).resolve()

        if not target.is_relative_to(upload_root):
            continue
        with zf.open(name) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out, length=1024 * 1024)
        count += 1
    return count


async def _spool_upload_to_disk(file: UploadFile, dst: Path, max_bytes: int) -> int:
    """Stream `file` to `dst` in chunks. Aborts with 413 if it exceeds `max_bytes`."""
    size = 0
    chunk_size = 1024 * 1024
    with open(dst, "wb") as out:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(413, "Upload too large")
            out.write(chunk)
    return size


@app.post("/maintenance/import")
async def maintenance_import(file: UploadFile = File(...)):
    """Replace the live DB (and optionally uploads/) from a `.db` or backup `.zip`.

    The current DB is copied to `stash.db.bak-<timestamp>` before replacement so
    a botched import can be reverted by hand. Streams the upload to disk rather
    than buffering — backup zips can be multi-GB on photo-heavy stashes."""
    if not file or not file.filename:
        raise HTTPException(400, "File required")

    spool = DB_PATH.with_name(f"{DB_PATH.name}.import-upload")
    try:
        size = await _spool_upload_to_disk(file, spool, MAX_IMPORT_BYTES)
        if size == 0:
            raise HTTPException(400, "File is empty")

        with open(spool, "rb") as f:
            header = f.read(max(len(_SQLITE_MAGIC), len(_ZIP_MAGIC)))

        if header.startswith(_ZIP_MAGIC):
            import zipfile
            try:
                zf = zipfile.ZipFile(spool)
            except zipfile.BadZipFile:
                raise HTTPException(400, "File is not a valid zip archive")
            with zf:
                if "stash.db" not in zf.namelist():
                    raise HTTPException(400, "Zip is missing stash.db — not a stash backup")
                db_tmp = DB_PATH.with_name(f"{DB_PATH.name}.zipdb-tmp")
                try:
                    _extract_db_member_to_path(zf, "stash.db", db_tmp)
                    _replace_db_from_path(db_tmp)
                finally:
                    try:
                        db_tmp.unlink()
                    except FileNotFoundError:
                        pass
                _restore_uploads_from_zip(zf)
        elif header.startswith(_SQLITE_MAGIC):
            _replace_db_from_path(spool)
        else:
            raise HTTPException(400, "File must be a SQLite .db or a stash backup .zip")
    finally:
        try:
            spool.unlink()
        except FileNotFoundError:
            pass

    return RedirectResponse("/maintenance?imported=1", status_code=303)


# ── Module bottom: schema + filesystem migrations run last ───────────
# Every helper they need is defined above (encryption helpers,
# _tenant_file, _migrate_uploads_to_encrypted_tenant_dirs).  Keeping
# the calls here means the ordering between helper-definition and
# helper-use never trips someone up.
init_db()
migrate_db()
