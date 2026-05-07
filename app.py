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
import vision

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


# Defense-in-depth on top of oauth2-proxy. The proxy is supposed to gate on
# emails.txt, but a missing host file silently turns the bind mount into a
# directory — oauth2-proxy then loads an empty list and `--email-domain "*"`
# happily lets every Google account through. With this gate, even if the
# proxy fails open, stash refuses any X-Forwarded-Email that isn't on the
# explicit STASH_ALLOWED_EMAILS list.
#
# Fails-closed at startup: refuse to boot without an allowlist unless the
# operator opts into a fully-public deployment with FULLY_PUBLIC=true.
# Sessions stay owned by oauth2-proxy / Google — to revoke someone, remove
# them from emails.txt (and rotate the cookie secret if you need active
# sessions killed immediately) rather than chasing it through stash's DB.
_ALLOWED_EMAILS = frozenset(
    e.strip().lower()
    for e in os.environ.get("STASH_ALLOWED_EMAILS", "").split(",")
    if e.strip()
)
_FULLY_PUBLIC = os.environ.get("FULLY_PUBLIC", "").strip().lower() == "true"

if not _ALLOWED_EMAILS and not _FULLY_PUBLIC:
    raise RuntimeError(
        "Refusing to start: STASH_ALLOWED_EMAILS is empty. Set it to a "
        "comma-separated list of authorized emails, or set FULLY_PUBLIC=true "
        "to explicitly opt into a no-allowlist deployment."
    )


@app.middleware("http")
async def enforce_email_allowlist(request: Request, call_next):
    if _FULLY_PUBLIC:
        return await call_next(request)
    email = (request.headers.get("X-Forwarded-Email") or "").strip().lower()
    if not email or email not in _ALLOWED_EMAILS:
        return Response(
            "Forbidden — your email is not authorized for this stash.",
            status_code=403,
            media_type="text/plain",
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
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
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
        conn.commit()


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


init_db()
migrate_db()


MAX_IMAGE_DIM = 2048
JPEG_QUALITY = 85
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB; matches Caddy's request_body cap

# Refuse to decode anything that would expand to > 50M pixels (~50MB raw RGB).
# Defends against PNG/TIFF "decompression bombs" — small files that allocate
# huge amounts of memory when decoded.
from PIL import Image as _PilImage
_PilImage.MAX_IMAGE_PIXELS = 50_000_000


def save_photo(photo: UploadFile | None) -> str | None:
    if not photo or not photo.filename:
        return None
    return save_photo_bytes(photo.file.read(), photo.filename)


def save_photo_bytes(data: bytes, filename: str) -> str:
    """Re-encode as JPEG with EXIF orientation baked in and longest side capped.

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
        img.save(UPLOAD_DIR / name, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        # Pre-generate the thumb from the in-memory image — cheaper than
        # opening the file again, and covers the new-upload-then-immediately-
        # render case without paying the lazy-gen cost on the first request.
        _save_thumb_from_image(img, name)
        return name
    except HTTPException:
        raise
    except Exception:
        name = f"{secrets.token_hex(8)}.jpg"
        (UPLOAD_DIR / name).write_bytes(data)
        return name


def _save_thumb_from_image(img, name: str) -> None:
    """Write a thumbnail for `name` from an already-decoded PIL image."""
    from PIL import Image as _Image
    try:
        thumb_img = img.copy()
        if max(thumb_img.size) > THUMB_MAX_DIM:
            thumb_img.thumbnail((THUMB_MAX_DIM, THUMB_MAX_DIM), _Image.LANCZOS)
        thumb = _thumb_path(name)
        tmp = thumb.with_suffix(thumb.suffix + ".tmp")
        thumb_img.save(tmp, format="JPEG", quality=80, optimize=True)
        os.replace(tmp, thumb)
    except Exception:
        pass  # the lazy /thumbs endpoint will retry generation on first view


def ensure_tag(conn, name: str) -> int:
    """Get or create a tag by name (case-insensitive). Returns tag id."""
    name = name.strip()
    conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
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


def set_item_tags(conn, item_id: int, tag_entries: list[tuple[str, str | None]]) -> None:
    for tag_name, value in tag_entries:
        tag_id = ensure_tag(conn, tag_name)
        conn.execute(
            "INSERT OR REPLACE INTO item_tags (item_id, tag_id, value) VALUES (?, ?, ?)",
            (item_id, tag_id, value),
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


def crop_photo(photo_name: str, bbox: tuple[int, int, int, int]) -> str:
    """Crop a photo using bbox (y_min, x_min, y_max, x_max in 0-1000 coords).
    Returns the filename of the cropped image saved to UPLOAD_DIR.

    Normalizes to JPEG and pre-generates the companion thumbnail so a re-crop
    is reflected immediately on the next page render — without the thumbnail
    side, lazy /thumbs generation can fall back to serving the source under an
    immutable cache header, leaving stale crops visible."""
    src = UPLOAD_DIR / photo_name
    img = _open_image_oriented(src)
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
    cropped.save(UPLOAD_DIR / crop_name, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    _save_thumb_from_image(cropped, crop_name)
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


def _delete_upload_if_orphan(conn, photo_name: str) -> None:
    if not photo_name or _photo_still_referenced(conn, photo_name):
        return
    try:
        (UPLOAD_DIR / photo_name).unlink()
    except FileNotFoundError:
        pass
    # Companion thumbnail goes with the source. The thumb is a derived
    # artifact, never tracked in the DB — so its lifetime is purely tied to
    # whether anything still references the original.
    _delete_thumb_if_exists(photo_name)


import re as _re
_UPLOAD_NAME_RE = _re.compile(r"^[A-Za-z0-9._-]+$")

# Longest side of a generated thumbnail. 320 px renders crisply at 100 px CSS
# squares on retina (3x = 300) without paying the cost of the 2048 px source.
THUMB_MAX_DIM = 320

# Cap concurrent thumbnail decodes so a page with a dozen brand-new photos
# can't fan out into a dozen full-resolution PIL decodes at once and run the
# container out of memory. Two at a time is plenty — each generation finishes
# in <100 ms once draft() is in play.
import threading as _threading
_THUMB_GEN_SEMAPHORE = _threading.Semaphore(2)


def _thumb_path(name: str) -> Path:
    """Companion thumbnail file for a given upload. Always .jpg since
    save_photo_bytes re-encodes everything to JPEG anyway."""
    return UPLOAD_DIR / f"{Path(name).stem}_thumb.jpg"


def _is_thumb_name(name: str) -> bool:
    return Path(name).stem.endswith("_thumb")


def _ensure_thumb(name: str) -> Path | None:
    """Lazy-generate the thumb for an existing upload. Returns the thumb path
    or None if the source is missing / un-decodable. Writes via a tmp file +
    rename so concurrent requests can't see a half-written thumb.

    Memory-aware: uses PIL's draft() to tell the JPEG decoder to scale down
    BEFORE allocating pixel buffers. A pre-cap 7000×7000 JPEG decodes at full
    res to ~150 MB RGB; with draft asking for ~640 px, the same image lands
    at <3 MB. Combined with the module-level semaphore, the container can no
    longer be OOM-killed by a fan-out of concurrent thumb requests."""
    src = UPLOAD_DIR / name
    if not src.exists() or _is_thumb_name(name):
        return None
    thumb = _thumb_path(name)
    if thumb.exists():
        return thumb

    with _THUMB_GEN_SEMAPHORE:
        # Re-check now that we hold the lock — another request may have
        # generated this very thumb while we were queued.
        if thumb.exists():
            return thumb
        try:
            from PIL import Image, ImageOps
            with Image.open(src) as opened:
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
                tmp = thumb.with_suffix(thumb.suffix + ".tmp")
                img.save(tmp, format="JPEG", quality=80, optimize=True)
                os.replace(tmp, thumb)
            return thumb
        except Exception:
            # Any decode failure (test fixtures, corrupt jpegs, OOM in PIL,
            # etc) — let the caller fall back to serving the full-res source
            # so the page doesn't break.
            return None


def _delete_thumb_if_exists(name: str) -> None:
    if _is_thumb_name(name):
        return
    try:
        _thumb_path(name).unlink()
    except FileNotFoundError:
        pass


@app.get("/uploads/{name}")
def serve_upload(name: str):
    # Reject obviously hostile names before any filesystem touch. We only
    # generate names like `<hex>.jpg` — anything outside that alphabet is not
    # ours and not worth resolving.
    if not _UPLOAD_NAME_RE.match(name) or ".." in name:
        raise HTTPException(404)
    upload_root = UPLOAD_DIR.resolve()
    p = (UPLOAD_DIR / name).resolve()
    # Defense-in-depth: even after the regex check, refuse to serve anything
    # that lands outside UPLOAD_DIR (e.g. through symlink shenanigans).
    if not p.is_relative_to(upload_root) or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/thumbs/{name}")
def serve_thumb(name: str):
    """Serves a downscaled version of /uploads/{name} for grid + list views.
    Filenames are content-hashed so the result is immutable — long-cached."""
    if not _UPLOAD_NAME_RE.match(name) or ".." in name:
        raise HTTPException(404)
    upload_root = UPLOAD_DIR.resolve()
    src = (UPLOAD_DIR / name).resolve()
    if not src.is_relative_to(upload_root) or not src.is_file():
        raise HTTPException(404)
    thumb = _ensure_thumb(name)
    target = thumb if thumb and thumb.exists() else src
    return FileResponse(
        target,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with db() as conn:
        boxes = conn.execute(
            "SELECT b.*, COUNT(i.id) AS item_count, "
            "       r.name AS room_name, r.color AS room_color, "
            "       l.id AS location_id, l.name AS location_name "
            "FROM boxes b "
            "LEFT JOIN rooms r ON r.id = b.room_id "
            "LEFT JOIN locations l ON l.id = r.location_id "
            "LEFT JOIN items i ON i.box_id = b.id "
            "GROUP BY b.id ORDER BY b.created_at DESC"
        ).fetchall()
        # Up to 5 most-recent item photos per box, for the preview strip
        thumb_rows = conn.execute(
            "SELECT box_id, photo FROM items "
            "WHERE photo IS NOT NULL ORDER BY box_id, created_at DESC"
        ).fetchall()
        rooms = _rooms_for_picker(conn)
    thumbs: dict[int, list[str]] = {}
    for r in thumb_rows:
        lst = thumbs.setdefault(r["box_id"], [])
        if len(lst) < 5:
            lst.append(r["photo"])
    return templates.TemplateResponse(
        request, "index.html",
        {"boxes": boxes, "thumbs": thumbs, "rooms": rooms},
    )


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
    name: str = Form(...),
    location: str = Form(""),
    notes: str = Form(""),
    room_id: str = Form(""),
):
    rid = _coerce_room_id(room_id)
    with db() as conn:
        # If a room is picked, denormalize its name into boxes.location so the
        # plain text shows up everywhere that doesn't JOIN to rooms.
        if rid is not None:
            row = conn.execute("SELECT name FROM rooms WHERE id = ?", (rid,)).fetchone()
            if row:
                location = row["name"]
            else:
                rid = None
        conn.execute(
            "INSERT INTO boxes (name, location, notes, room_id) VALUES (?, ?, ?, ?)",
            (name.strip(), location.strip(), notes.strip(), rid),
        )
        conn.commit()
    return RedirectResponse("/", status_code=303)


def _known_locations(conn) -> list[str]:
    return [
        r["location"] for r in conn.execute(
            "SELECT DISTINCT location FROM boxes "
            "WHERE location IS NOT NULL AND location != '' ORDER BY location"
        ).fetchall()
    ]


@app.get("/boxes/{box_id}", response_class=HTMLResponse)
def box_detail(request: Request, box_id: int):
    with db() as conn:
        box = conn.execute(
            "SELECT b.*, r.name AS room_name, r.color AS room_color, "
            "       l.id AS location_id, l.name AS location_name "
            "FROM boxes b "
            "LEFT JOIN rooms r ON r.id = b.room_id "
            "LEFT JOIN locations l ON l.id = r.location_id "
            "WHERE b.id = ?",
            (box_id,),
        ).fetchone()
        if not box:
            raise HTTPException(404)
        items_raw = conn.execute(
            "SELECT * FROM items WHERE box_id = ? ORDER BY created_at DESC",
            (box_id,),
        ).fetchall()
        items_with_tags = []
        for it in items_raw:
            tags = get_item_tags(conn, it["id"])
            items_with_tags.append({"item": it, "tags": tags})
        other_boxes = conn.execute(
            "SELECT id, name, location FROM boxes WHERE id != ? ORDER BY name", (box_id,)
        ).fetchall()
        locations = _known_locations(conn)
        rooms = _rooms_for_picker(conn)
        all_tags = conn.execute("SELECT name FROM tags ORDER BY name").fetchall()
    return templates.TemplateResponse(
        request, "box.html",
        {
            "box": box,
            "items_with_tags": items_with_tags,
            "other_boxes": other_boxes,
            "locations": locations,
            "rooms": rooms,
            "all_tags": [r["name"] for r in all_tags],
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
    rid = _coerce_room_id(room_id)
    with db() as conn:
        if not conn.execute("SELECT 1 FROM boxes WHERE id = ?", (box_id,)).fetchone():
            raise HTTPException(404)
        location_text = ""
        if rid is not None:
            row = conn.execute("SELECT name FROM rooms WHERE id = ?", (rid,)).fetchone()
            if not row:
                raise HTTPException(400, "Unknown room")
            location_text = row["name"]
        conn.execute(
            "UPDATE boxes SET room_id = ?, location = ? WHERE id = ?",
            (rid, location_text, box_id),
        )
        conn.commit()
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "room_id": rid}
    return RedirectResponse(f"/boxes/{box_id}", status_code=303)


@app.post("/boxes/{box_id}/edit")
def edit_box(
    box_id: int,
    name: str = Form(...),
    location: str = Form(""),
    notes: str = Form(""),
    room_id: str = Form(""),
    color: str = Form(""),
):
    if not name.strip():
        raise HTTPException(400, "Name required")
    rid = _coerce_room_id(room_id)
    # "" / "inherit" wipes the override and falls back to the room color.
    color_val: str | None
    if color.strip() == "" or color.strip() == "inherit":
        color_val = None
    elif color.strip() in _ROOM_COLORS:
        color_val = color.strip()
    else:
        color_val = None  # silently reject off-palette
    with db() as conn:
        if not conn.execute("SELECT 1 FROM boxes WHERE id = ?", (box_id,)).fetchone():
            raise HTTPException(404)
        if rid is not None:
            row = conn.execute("SELECT name FROM rooms WHERE id = ?", (rid,)).fetchone()
            if row:
                location = row["name"]
            else:
                rid = None
        conn.execute(
            "UPDATE boxes SET name = ?, location = ?, notes = ?, room_id = ?, color = ? WHERE id = ?",
            (name.strip(), location.strip(), notes.strip(), rid, color_val, box_id),
        )
        conn.commit()
    return RedirectResponse(f"/boxes/{box_id}", status_code=303)


@app.post("/items/{item_id}/move")
def move_item(request: Request, item_id: int, box_id: int = Form(...)):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
            raise HTTPException(404)
        if not conn.execute("SELECT 1 FROM boxes WHERE id = ?", (box_id,)).fetchone():
            raise HTTPException(400, "Unknown box")
        conn.execute("UPDATE items SET box_id = ? WHERE id = ?", (box_id, item_id))
        conn.commit()
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "item_id": item_id, "box_id": box_id}
    return RedirectResponse(f"/boxes/{box_id}#item-{item_id}", status_code=303)


@app.post("/boxes/{box_id}/move-items")
async def bulk_move_items(request: Request, box_id: int):
    form_data = await request.form()
    target_box_id = int(form_data["target_box_id"])
    item_ids = [int(v) for v in form_data.getlist("item_ids")]
    if not item_ids:
        return RedirectResponse(f"/boxes/{box_id}", status_code=303)
    with db() as conn:
        if not conn.execute("SELECT 1 FROM boxes WHERE id = ?", (target_box_id,)).fetchone():
            raise HTTPException(400, "Unknown target box")
        placeholders = ",".join("?" * len(item_ids))
        conn.execute(
            f"UPDATE items SET box_id = ? WHERE id IN ({placeholders}) AND box_id = ?",
            [target_box_id, *item_ids, box_id],
        )
        conn.commit()
    return RedirectResponse(f"/boxes/{target_box_id}", status_code=303)


@app.get("/boxes/{box_id}/audit", response_class=HTMLResponse)
def audit_box(request: Request, box_id: int):
    with db() as conn:
        box = conn.execute("SELECT * FROM boxes WHERE id = ?", (box_id,)).fetchone()
        if not box:
            raise HTTPException(404)
        items = conn.execute(
            "SELECT * FROM items WHERE box_id = ? ORDER BY name", (box_id,)
        ).fetchall()
    return templates.TemplateResponse(
        request, "audit.html", {"box": box, "items": items}
    )


@app.post("/boxes/{box_id}/audit")
async def submit_audit(request: Request, box_id: int):
    form_data = await request.form()
    found_ids = {int(v) for v in form_data.getlist("found")}
    with db() as conn:
        box = conn.execute("SELECT name FROM boxes WHERE id = ?", (box_id,)).fetchone()
        if not box:
            raise HTTPException(404)
        all_items = conn.execute(
            "SELECT id, name, notes, photo FROM items WHERE box_id = ?", (box_id,)
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
                    "INSERT INTO pending_items (name, description, photo, previous_box_name) "
                    "VALUES (?, ?, ?, ?)",
                    (it["name"], it["notes"], it["photo"], box["name"]),
                )
                pending_id = cur.lastrowid
                # Preserve tags
                conn.execute(
                    "INSERT INTO pending_item_tags (pending_item_id, tag_id, value) "
                    "SELECT ?, tag_id, value FROM item_tags WHERE item_id = ?",
                    (pending_id, it["id"]),
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
    box_id: int,
    name: str = Form(...),
    notes: str = Form(""),
    tags: str = Form(""),
    photo: UploadFile = File(None),
):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM boxes WHERE id = ?", (box_id,)).fetchone():
            raise HTTPException(404)
        photo_name = save_photo(photo)
        cur = conn.execute(
            "INSERT INTO items (box_id, name, notes, photo, source_photo) VALUES (?, ?, ?, ?, ?)",
            (box_id, name.strip(), notes.strip(), photo_name, photo_name),
        )
        if tags.strip():
            set_item_tags(conn, cur.lastrowid, parse_tag_input(tags))
        conn.commit()
    return RedirectResponse(f"/boxes/{box_id}", status_code=303)


@app.post("/items/{item_id}/tags")
def add_item_tag(item_id: int, tag: str = Form(...)):
    entries = parse_tag_input(tag)
    if not entries:
        raise HTTPException(400, "Tag required")
    with db() as conn:
        row = conn.execute("SELECT box_id FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        set_item_tags(conn, item_id, entries)
        conn.commit()
    return RedirectResponse(f"/boxes/{row['box_id']}#item-{item_id}", status_code=303)


@app.post("/items/{item_id}/tags/{tag_id}/delete")
def remove_item_tag(item_id: int, tag_id: int):
    with db() as conn:
        row = conn.execute("SELECT box_id FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        conn.execute(
            "DELETE FROM item_tags WHERE item_id = ? AND tag_id = ?", (item_id, tag_id)
        )
        conn.commit()
    return RedirectResponse(f"/boxes/{row['box_id']}#item-{item_id}", status_code=303)


@app.get("/boxes/{box_id}/preview", response_class=HTMLResponse)
def box_preview(request: Request, box_id: int):
    """Compact box summary for the floorplan tile-click modal — name,
    location, item count, a few thumbs, and a link to open the box."""
    with db() as conn:
        box = conn.execute(
            "SELECT b.*, r.name AS room_name, r.color AS room_color, "
            "       l.id AS location_id, l.name AS location_name "
            "FROM boxes b "
            "LEFT JOIN rooms r ON r.id = b.room_id "
            "LEFT JOIN locations l ON l.id = r.location_id "
            "WHERE b.id = ?",
            (box_id,),
        ).fetchone()
        if not box:
            raise HTTPException(404)
        items = conn.execute(
            "SELECT id, name, photo FROM items "
            "WHERE box_id = ? ORDER BY created_at DESC LIMIT 60",
            (box_id,),
        ).fetchall()
        item_count = conn.execute(
            "SELECT COUNT(*) FROM items WHERE box_id = ?", (box_id,),
        ).fetchone()[0]
    return templates.TemplateResponse(
        request, "_floorplan_box_preview.html",
        {"box": box, "items": items, "item_count": item_count},
    )


@app.get("/items/{item_id}/preview", response_class=HTMLResponse)
def item_preview(request: Request, item_id: int):
    """HTML fragment rendering an item's detail card. Used by the search
    page to open a result in a modal instead of navigating away — the same
    actions (re-tag, move, replace photo, delete) work in place."""
    with db() as conn:
        item = conn.execute(
            "SELECT i.*, b.name AS box_name, b.id AS box_id "
            "FROM items i JOIN boxes b ON b.id = i.box_id "
            "WHERE i.id = ?",
            (item_id,),
        ).fetchone()
        if not item:
            raise HTTPException(404)
        tags = get_item_tags(conn, item_id)
        other_boxes = conn.execute(
            "SELECT id, name, location FROM boxes WHERE id != ? ORDER BY name",
            (item["box_id"],),
        ).fetchall()
    return templates.TemplateResponse(
        request, "_search_item_modal.html",
        {"it": item, "tags": tags, "other_boxes": other_boxes},
    )


@app.get("/items/{item_id}/recrop", response_class=HTMLResponse)
def recrop_item(request: Request, item_id: int):
    with db() as conn:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not item:
            raise HTTPException(404)
    return templates.TemplateResponse(
        request, "recrop.html", {"item": item}
    )


@app.post("/items/{item_id}/recrop")
def apply_recrop(
    item_id: int,
    crop_y_min: str = Form(""),
    crop_x_min: str = Form(""),
    crop_y_max: str = Form(""),
    crop_x_max: str = Form(""),
    skip_crop: str = Form(""),
):
    with db() as conn:
        item = conn.execute(
            "SELECT photo, source_photo, box_id FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if not item:
            raise HTTPException(404)

        source = item["source_photo"] or item["photo"]
        old_photo = item["photo"]

        if skip_crop.strip() == "1":
            # Undo crop — revert to full source image
            new_photo = source
        elif crop_y_min.strip() and crop_x_min.strip() and crop_y_max.strip() and crop_x_max.strip():
            bbox = (int(crop_y_min), int(crop_x_min), int(crop_y_max), int(crop_x_max))
            new_photo = crop_photo(source, bbox)
        else:
            # No change
            return RedirectResponse(f"/boxes/{item['box_id']}#item-{item_id}", status_code=303)

        conn.execute(
            "UPDATE items SET photo = ?, source_photo = ? WHERE id = ?",
            (new_photo, source, item_id),
        )
        conn.commit()
        # Old crop file may now be orphaned
        if old_photo and old_photo != new_photo and old_photo != source:
            _delete_upload_if_orphan(conn, old_photo)
    return RedirectResponse(f"/boxes/{item['box_id']}#item-{item_id}", status_code=303)


@app.post("/items/{item_id}/replace-photo")
async def replace_item_photo(item_id: int, photo: UploadFile = File(...)):
    if not photo or not photo.filename:
        raise HTTPException(400, "Photo required")
    with db() as conn:
        row = conn.execute(
            "SELECT box_id, photo, source_photo FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        new_photo = save_photo(photo)
        conn.execute(
            "UPDATE items SET photo = ?, source_photo = ? WHERE id = ?",
            (new_photo, new_photo, item_id),
        )
        conn.commit()
        for old in {row["photo"], row["source_photo"]}:
            _delete_upload_if_orphan(conn, old)
    return RedirectResponse(f"/boxes/{row['box_id']}#item-{item_id}", status_code=303)


@app.post("/items/{item_id}/delete")
def delete_item(item_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT box_id, photo, source_photo FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        conn.commit()
        for photo_name in {row["photo"], row["source_photo"]}:
            _delete_upload_if_orphan(conn, photo_name)
    return RedirectResponse(f"/boxes/{row['box_id']}", status_code=303)


_EXT_TO_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def _bytes_for_vision(photo_name: str) -> bytes:
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

    Falls back to raw bytes if PIL can't decode the file (test fixtures
    sometimes use synthetic JPEG headers PIL refuses)."""
    src = UPLOAD_DIR / photo_name
    try:
        from PIL import Image, ImageOps
        import io as _io
        with Image.open(src) as opened:
            rotated = ImageOps.exif_transpose(opened)
        if rotated.mode not in ("RGB", "L"):
            rotated = rotated.convert("RGB")
        rotated.info.pop("exif", None)
        buf = _io.BytesIO()
        rotated.save(buf, format="JPEG", quality=JPEG_QUALITY)
        return buf.getvalue()
    except Exception:
        return src.read_bytes()


def process_ingest_job(job_id: int, photo_name: str) -> None:
    """Background worker: vision pass → insert pending items → mark job done."""
    with db() as conn:
        conn.execute("UPDATE ingest_jobs SET status = 'processing' WHERE id = ?", (job_id,))
        conn.commit()
    try:
        image_bytes = _bytes_for_vision(photo_name)
        detected = vision.detect_items(image_bytes, media_type="image/jpeg")
        with db() as conn:
            for item in detected:
                bbox = item.bbox or [None, None, None, None]
                conn.execute(
                    "INSERT INTO pending_items "
                    "(name, description, photo, bbox_y_min, bbox_x_min, bbox_y_max, bbox_x_max) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (item.name, item.description, photo_name, *bbox),
                )
            conn.execute(
                "UPDATE ingest_jobs SET status = 'done', item_count = ?, "
                "completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (len(detected), job_id),
            )
            conn.commit()
    except Exception as e:
        with db() as conn:
            conn.execute(
                "UPDATE ingest_jobs SET status = 'failed', error = ?, "
                "completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (str(e)[:500], job_id),
            )
            conn.commit()


def _ingest_jobs(conn):
    return conn.execute(
        "SELECT * FROM ingest_jobs WHERE status != 'done' "
        "ORDER BY created_at DESC LIMIT 50"
    ).fetchall()


def _ingest_fingerprint(jobs) -> str:
    import hashlib
    payload = "|".join(
        f"{j['id']}:{j['status']}:{j['item_count']}:{j['error']}" for j in jobs
    )
    return hashlib.sha1(payload.encode()).hexdigest()


@app.get("/ingest", response_class=HTMLResponse)
def ingest_form(request: Request):
    with db() as conn:
        jobs = _ingest_jobs(conn)
    return templates.TemplateResponse(
        request, "ingest.html",
        {"jobs": jobs, "fingerprint": _ingest_fingerprint(jobs)},
    )


@app.get("/ingest/state")
def ingest_state():
    """Lightweight poll target so the ingest page can update its job list
    without a full meta-refresh — that one was nuking in-progress file
    picker selections and cancelling uploads mid-stream."""
    with db() as conn:
        jobs = _ingest_jobs(conn)
    has_active = any(j["status"] in ("pending", "processing") for j in jobs)
    return {
        "fingerprint": _ingest_fingerprint(jobs),
        "has_active": has_active,
    }


@app.get("/ingest/jobs", response_class=HTMLResponse)
def ingest_jobs_fragment(request: Request):
    """HTML fragment of just the jobs list — the ingest page swaps this in
    when its fingerprint changes."""
    with db() as conn:
        jobs = _ingest_jobs(conn)
    return templates.TemplateResponse(
        request, "_ingest_jobs.html", {"jobs": jobs},
    )


@app.post("/ingest")
async def ingest(background_tasks: BackgroundTasks, photos: list[UploadFile] = File(...)):
    valid = [p for p in photos if p and p.filename]
    if not valid:
        raise HTTPException(400, "Photo required")

    for photo in valid:
        image_bytes = await photo.read()
        photo_name = save_photo_bytes(image_bytes, photo.filename)

        with db() as conn:
            cur = conn.execute(
                "INSERT INTO ingest_jobs (photo, status) VALUES (?, 'pending')", (photo_name,)
            )
            job_id = cur.lastrowid
            conn.commit()

        background_tasks.add_task(process_ingest_job, job_id, photo_name)

    return RedirectResponse("/ingest", status_code=303)


@app.post("/ingest/{job_id}/retry")
def ingest_retry(background_tasks: BackgroundTasks, job_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT photo FROM ingest_jobs WHERE id = ? AND status = 'failed'", (job_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Job not found or not failed")
        conn.execute(
            "UPDATE ingest_jobs SET status = 'pending', error = NULL, "
            "completed_at = NULL WHERE id = ?",
            (job_id,),
        )
        conn.commit()
    background_tasks.add_task(process_ingest_job, job_id, row["photo"])
    return RedirectResponse("/ingest", status_code=303)


@app.post("/ingest/{job_id}/dismiss")
def ingest_dismiss(job_id: int):
    with db() as conn:
        conn.execute(
            "DELETE FROM ingest_jobs WHERE id = ? AND status IN ('failed', 'done')",
            (job_id,),
        )
        conn.commit()
    return RedirectResponse("/ingest", status_code=303)


@app.get("/queue", response_class=HTMLResponse)
def queue(request: Request):
    with db() as conn:
        pending = conn.execute(
            "SELECT p.*, b.name AS suggested_box_name FROM pending_items p "
            "LEFT JOIN boxes b ON b.id = p.suggested_box_id "
            "ORDER BY p.created_at ASC"
        ).fetchall()
        boxes = conn.execute(
            "SELECT b.id, b.name, b.location, "
            "       r.name AS room_name, "
            "       l.id AS location_id, l.name AS location_name "
            "FROM boxes b "
            "LEFT JOIN rooms r ON r.id = b.room_id "
            "LEFT JOIN locations l ON l.id = r.location_id "
            # Sort so optgroup ordering is stable: location first, then room,
            # then box name within each room.
            "ORDER BY l.name IS NULL, l.name, r.name, b.name"
        ).fetchall()
        all_tags = [r["name"] for r in conn.execute("SELECT name FROM tags ORDER BY name").fetchall()]

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

    import hashlib
    payload = "|".join(
        f"{r['id']}:{r['name']}:{r['description']}:{r['suggested_box_id']}:"
        f"{r['suggested_new_box_name']}:{r['suggestion_reason']}"
        for r in pending
    ) + "||" + "|".join(f"{b['id']}:{b['name']}" for b in boxes)
    fingerprint = hashlib.sha1(payload.encode()).hexdigest()
    return templates.TemplateResponse(
        request, "queue.html",
        {
            "pending": pending,
            "boxes": boxes,
            "boxes_grouped": boxes_grouped,
            "fingerprint": fingerprint,
            "all_tags": all_tags,
        },
    )


@app.post("/queue/{pending_id}/match")
def queue_match(pending_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM pending_items WHERE id = ?", (pending_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        boxes = [
            dict(b) for b in conn.execute(
                "SELECT id, name, location, notes FROM boxes"
            ).fetchall()
        ]

    suggestion = vision.suggest_box(row["name"], row["description"] or "", boxes)

    with db() as conn:
        conn.execute(
            "UPDATE pending_items SET suggested_box_id = ?, suggested_new_box_name = ?, "
            "suggested_new_box_location = ?, suggestion_reason = ? WHERE id = ?",
            (
                suggestion.box_id if suggestion.match == "existing" else None,
                suggestion.new_box_name if suggestion.match == "new" else None,
                suggestion.new_box_location if suggestion.match == "new" else None,
                suggestion.reason,
                pending_id,
            ),
        )
        conn.commit()
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/{pending_id}/assign")
def queue_assign(
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

    with db() as conn:
        row = conn.execute(
            "SELECT photo, bbox_y_min, bbox_x_min, bbox_y_max, bbox_x_max "
            "FROM pending_items WHERE id = ?", (pending_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        target_box_id = int(box_id)
        if not conn.execute("SELECT 1 FROM boxes WHERE id = ?", (target_box_id,)).fetchone():
            raise HTTPException(400, "Unknown box")

        # Use manual crop coords if submitted, fall back to DB bbox, skip if cleared
        source_photo = row["photo"]
        photo = source_photo
        if skip_crop.strip() != "1":
            if crop_y_min.strip() and crop_x_min.strip() and crop_y_max.strip() and crop_x_max.strip():
                bbox = (int(crop_y_min), int(crop_x_min), int(crop_y_max), int(crop_x_max))
                photo = crop_photo(photo, bbox)
            elif photo and row["bbox_y_min"] is not None:
                bbox = (row["bbox_y_min"], row["bbox_x_min"], row["bbox_y_max"], row["bbox_x_max"])
                photo = crop_photo(photo, bbox)

        cur = conn.execute(
            "INSERT INTO items (box_id, name, notes, photo, source_photo) VALUES (?, ?, ?, ?, ?)",
            (target_box_id, name.strip(), description.strip(), photo, source_photo),
        )
        new_item_id = cur.lastrowid
        # Transfer tags from pending to the real item
        conn.execute(
            "INSERT INTO item_tags (item_id, tag_id, value) "
            "SELECT ?, tag_id, value FROM pending_item_tags WHERE pending_item_id = ?",
            (new_item_id, pending_id),
        )
        if tags.strip():
            set_item_tags(conn, new_item_id, parse_tag_input(tags))
        conn.execute("DELETE FROM pending_items WHERE id = ?", (pending_id,))
        conn.commit()
    return RedirectResponse("/queue", status_code=303)


@app.get("/queue/state")
def queue_state():
    """Fingerprint for real-time polling — changes whenever queue content changes."""
    import hashlib
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, description, suggested_box_id, suggested_new_box_name, "
            "suggestion_reason FROM pending_items ORDER BY id"
        ).fetchall()
        boxes = conn.execute("SELECT id, name FROM boxes ORDER BY id").fetchall()
    payload = "|".join(
        f"{r['id']}:{r['name']}:{r['description']}:{r['suggested_box_id']}:"
        f"{r['suggested_new_box_name']}:{r['suggestion_reason']}"
        for r in rows
    ) + "||" + "|".join(f"{b['id']}:{b['name']}" for b in boxes)
    return {"fingerprint": hashlib.sha1(payload.encode()).hexdigest()}


@app.post("/queue/{pending_id}/delete")
def queue_delete(pending_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT photo FROM pending_items WHERE id = ?", (pending_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        conn.execute("DELETE FROM pending_items WHERE id = ?", (pending_id,))
        conn.commit()
        _delete_upload_if_orphan(conn, row["photo"])
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
    q: str, tag: str, location_id: int | None, room_id: int | None,
    box_id: int | None, missing: bool, has_photo: bool,
) -> tuple[str, list]:
    """Compose the WHERE clause + params for the search query. Pulled out so
    both the listing query and the count/facet queries can share it."""
    clauses = ["1=1"]
    params: list = []
    if q.strip():
        like = f"%{q.strip()}%"
        clauses.append("(i.name LIKE ? OR i.notes LIKE ?)")
        params.extend([like, like])
    if tag.strip():
        clauses.append(
            "i.id IN (SELECT it.item_id FROM item_tags it "
            "JOIN tags t ON t.id = it.tag_id WHERE t.name = ?)"
        )
        params.append(tag.strip())
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

    with db() as conn:
        all_tags = [
            r["name"] for r in conn.execute("SELECT name FROM tags ORDER BY name").fetchall()
        ]
        all_locations = [
            dict(r) for r in conn.execute(
                "SELECT id, name FROM locations ORDER BY name"
            ).fetchall()
        ]
        all_rooms = _rooms_for_picker(conn)
        all_boxes = [
            dict(r) for r in conn.execute(
                "SELECT b.id, b.name, "
                "       l.id AS location_id, l.name AS location_name, "
                "       r.id AS room_id, r.name AS room_name "
                "FROM boxes b "
                "LEFT JOIN rooms r ON r.id = b.room_id "
                "LEFT JOIN locations l ON l.id = r.location_id "
                "ORDER BY l.name IS NULL, l.name, r.name, b.name"
            ).fetchall()
        ]

        where, params = _build_search_query(
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
                f"WHERE it.item_id IN ({placeholders}) ORDER BY t.name",
                item_ids,
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
    with db() as conn:
        tags = conn.execute(
            "SELECT t.name, t.id, COUNT(it.item_id) AS item_count "
            "FROM tags t LEFT JOIN item_tags it ON it.tag_id = t.id "
            "GROUP BY t.id ORDER BY t.name"
        ).fetchall()
    return templates.TemplateResponse(request, "tags.html", {"tags": tags})


@app.get("/tags/autocomplete")
def tags_autocomplete(q: str = ""):
    with db() as conn:
        if q:
            rows = conn.execute(
                "SELECT name FROM tags WHERE name LIKE ? ORDER BY name LIMIT 20",
                (f"{q}%",),
            ).fetchall()
        else:
            rows = conn.execute("SELECT name FROM tags ORDER BY name LIMIT 50").fetchall()
    return [r["name"] for r in rows]


def _box_art_path(box_row) -> Path | None:
    """Resolve a box's background art file (or None). Tolerates missing files."""
    art = box_row["background_art"] if "background_art" in box_row.keys() else None
    if not art:
        return None
    p = UPLOAD_DIR / art
    return p if p.exists() else None


@app.get("/boxes/{box_id}/label.svg")
def box_label_svg(box_id: int):
    with db() as conn:
        box = conn.execute("SELECT * FROM boxes WHERE id = ?", (box_id,)).fetchone()
        if not box:
            raise HTTPException(404)
    svg = labels.render_label_svg(
        box["id"], box["name"], box["notes"] or "", PUBLIC_URL,
        background_art=_box_art_path(box),
    )
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Content-Disposition": f'attachment; filename="box-{box_id}-label.svg"'},
    )


def _selected_boxes(conn, box_ids_raw: list[str]) -> list:
    """Return rows for the selected boxes, or all boxes if no selection given.
    Ordering matches the labels page (alpha) so printed sheets are predictable."""
    if box_ids_raw:
        placeholders = ",".join("?" * len(box_ids_raw))
        return conn.execute(
            f"SELECT id, name, notes, background_art FROM boxes "
            f"WHERE id IN ({placeholders}) ORDER BY name",
            [int(b) for b in box_ids_raw],
        ).fetchall()
    return conn.execute(
        "SELECT id, name, notes, background_art FROM boxes ORDER BY name"
    ).fetchall()


@app.get("/labels", response_class=HTMLResponse)
def labels_page(request: Request):
    with db() as conn:
        boxes = conn.execute("SELECT * FROM boxes ORDER BY name").fetchall()
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
    box_ids_raw = request.query_params.getlist("box_ids")
    with db() as conn:
        boxes = _selected_boxes(conn, box_ids_raw)
    svg = labels.render_sheet_svg(
        [dict(b) for b in boxes], PUBLIC_URL, uploads_dir=UPLOAD_DIR,
    )
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Content-Disposition": 'attachment; filename="stash-labels.svg"'},
    )


@app.get("/labels/sheet.pdf")
def labels_sheet_pdf(request: Request):
    """Multi-page vector PDF — fits Avery label sheets directly and is the
    Cricut/print-ready artifact. Each sheet is its own page."""
    box_ids_raw = request.query_params.getlist("box_ids")
    with db() as conn:
        boxes = _selected_boxes(conn, box_ids_raw)
    try:
        pdf_bytes = labels.render_sheet_pdf(
            [dict(b) for b in boxes], PUBLIC_URL, uploads_dir=UPLOAD_DIR,
        )
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
    box_ids_raw = request.query_params.getlist("box_ids")
    with db() as conn:
        boxes = [dict(b) for b in _selected_boxes(conn, box_ids_raw)]
    pages = []
    for chunk_start in range(0, max(len(boxes), 1), labels.LABELS_PER_PAGE):
        chunk = boxes[chunk_start:chunk_start + labels.LABELS_PER_PAGE]
        pages.append(labels.render_single_sheet_svg(
            chunk, PUBLIC_URL, uploads_dir=UPLOAD_DIR,
        ))
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
    with db() as conn:
        box = conn.execute(
            "SELECT id, name, notes, background_art FROM boxes WHERE id = ?",
            (box_id,),
        ).fetchone()
        if not box:
            raise HTTPException(404)
        items = [
            dict(r) for r in conn.execute(
                "SELECT name, notes, photo FROM items WHERE box_id = ? "
                "ORDER BY created_at DESC LIMIT 12",
                (box_id,),
            ).fetchall()
        ]

    # Up to 3 small photo references for the multimodal prompt. Read once so
    # the genai call sees raw bytes; mime sniffed from extension since we
    # always re-encode to JPEG on upload.
    photo_refs: list[tuple[bytes, str]] = []
    for it in items:
        if not it.get("photo"):
            continue
        p = UPLOAD_DIR / it["photo"]
        if not p.exists():
            continue
        photo_refs.append((p.read_bytes(), _EXT_TO_MIME.get(p.suffix.lower(), "image/jpeg")))
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
    (UPLOAD_DIR / new_name).write_bytes(image_bytes)

    with db() as conn:
        old = box["background_art"]
        conn.execute(
            "UPDATE boxes SET background_art = ? WHERE id = ?",
            (new_name, box_id),
        )
        conn.commit()
        if old and old != new_name:
            _delete_upload_if_orphan(conn, old)

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
    with db() as conn:
        row = conn.execute(
            "SELECT background_art FROM boxes WHERE id = ?", (box_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        conn.execute("UPDATE boxes SET background_art = NULL WHERE id = ?", (box_id,))
        conn.commit()
        if row["background_art"]:
            _delete_upload_if_orphan(conn, row["background_art"])

    if _wants_json(request):
        return {"ok": True, "box_id": box_id, "background_art": None}
    return RedirectResponse(
        _safe_internal_redirect(next_url), status_code=303,
    )


@app.post("/boxes/{box_id}/delete")
def delete_box(box_id: int, confirm: str = Form(...)):
    with db() as conn:
        box = conn.execute("SELECT name FROM boxes WHERE id = ?", (box_id,)).fetchone()
        if not box:
            raise HTTPException(404)
        if confirm.strip() != box["name"]:
            raise HTTPException(400, "Type the box name to confirm deletion")
        photos = [r["photo"] for r in conn.execute(
            "SELECT photo FROM items WHERE box_id = ? AND photo IS NOT NULL", (box_id,)
        ).fetchall()]
        conn.execute("DELETE FROM boxes WHERE id = ?", (box_id,))
        conn.commit()
    for p in photos:
        try:
            (UPLOAD_DIR / p).unlink()
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


def _locations_with_room_counts(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT l.*, "
        "       (SELECT COUNT(*) FROM rooms WHERE location_id = l.id) AS room_count, "
        "       (SELECT COUNT(*) FROM boxes b "
        "         JOIN rooms r ON r.id = b.room_id "
        "         WHERE r.location_id = l.id) AS box_count "
        "FROM locations l ORDER BY l.created_at"
    ).fetchall()
    return [dict(r) for r in rows]


def _rooms_for_picker(conn) -> list[dict]:
    """Flat list suitable for an optgroup'd select. Includes floor name so a
    location with two rooms of the same name (e.g. two "Bathroom"s on
    different floors) can be visually disambiguated in the dropdown."""
    rows = conn.execute(
        "SELECT r.id, r.name, "
        "       l.id AS location_id, l.name AS location_name, "
        "       f.name AS floor_name "
        "FROM rooms r "
        "JOIN locations l ON l.id = r.location_id "
        "LEFT JOIN floors f ON f.id = r.floor_id "
        "ORDER BY l.name, f.name IS NULL, f.name, r.name"
    ).fetchall()
    rooms = [dict(r) for r in rows]

    # Mark each room with whether its name collides with another room in the
    # same location — the template uses this flag to append the floor name so
    # the user can tell them apart.
    by_loc_name: dict[tuple, int] = {}
    for r in rooms:
        key = (r["location_id"], r["name"].casefold())
        by_loc_name[key] = by_loc_name.get(key, 0) + 1
    for r in rooms:
        key = (r["location_id"], r["name"].casefold())
        r["needs_floor_disambiguation"] = by_loc_name[key] > 1
    return rooms


@app.get("/locations", response_class=HTMLResponse)
def locations_index(request: Request):
    with db() as conn:
        locs = _locations_with_room_counts(conn)
    return templates.TemplateResponse(
        request, "locations.html", {"locations": locs},
    )


@app.post("/locations")
def create_location(name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    with db() as conn:
        cur = conn.execute("INSERT INTO locations (name) VALUES (?)", (name,))
        conn.commit()
    return RedirectResponse(f"/locations/{cur.lastrowid}", status_code=303)


@app.get("/locations/{location_id}", response_class=HTMLResponse)
def location_detail(
    request: Request,
    location_id: int,
    edit: str = "",
    floor: int | None = None,
):
    with db() as conn:
        loc = conn.execute(
            "SELECT * FROM locations WHERE id = ?", (location_id,),
        ).fetchone()
        if not loc:
            raise HTTPException(404)
        floors = [dict(f) for f in conn.execute(
            "SELECT * FROM floors WHERE location_id = ? "
            "ORDER BY sort_order, id",
            (location_id,),
        ).fetchall()]

        # Default the current floor: explicit ?floor= wins, else the first one.
        current_floor = None
        if floors:
            if floor is not None:
                current_floor = next((f for f in floors if f["id"] == floor), floors[0])
            else:
                current_floor = floors[0]

        rooms = []
        if current_floor:
            rooms = [dict(r) for r in conn.execute(
                "SELECT r.*, "
                "       (SELECT COUNT(*) FROM boxes WHERE room_id = r.id) AS box_count "
                "FROM rooms r WHERE r.floor_id = ? ORDER BY r.name",
                (current_floor["id"],),
            ).fetchall()]
            # Pull every box on this floor in one shot, then bucket by room
            # so each room rect can render its boxes as tiles inside it.
            box_rows = conn.execute(
                "SELECT b.id, b.name, b.room_id, b.color, "
                "       b.created_at, b.last_audited_at, "
                "       (SELECT COUNT(*) FROM items WHERE box_id = b.id) AS item_count "
                "FROM boxes b "
                "JOIN rooms r ON r.id = b.room_id "
                "WHERE r.floor_id = ? "
                "ORDER BY b.name",
                (current_floor["id"],),
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
                "ORDER BY i.box_id, i.created_at DESC",
                (current_floor["id"],),
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
            "       (SELECT COUNT(*) FROM boxes WHERE room_id = r.id) AS box_count "
            "FROM rooms r WHERE r.location_id = ? AND r.floor_id IS NULL "
            "ORDER BY r.name",
            (location_id,),
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
def edit_location(location_id: int, name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM locations WHERE id = ?", (location_id,)).fetchone():
            raise HTTPException(404)
        conn.execute("UPDATE locations SET name = ? WHERE id = ?", (name, location_id))
        conn.commit()
    return RedirectResponse(f"/locations/{location_id}", status_code=303)


@app.post("/locations/{location_id}/delete")
def delete_location(location_id: int, confirm: str = Form(...)):
    with db() as conn:
        loc = conn.execute(
            "SELECT name, floorplan FROM locations WHERE id = ?", (location_id,),
        ).fetchone()
        if not loc:
            raise HTTPException(404)
        if confirm.strip() != loc["name"]:
            raise HTTPException(400, "Type the location name to confirm deletion")
        conn.execute("DELETE FROM locations WHERE id = ?", (location_id,))
        conn.commit()
        if loc["floorplan"]:
            _delete_upload_if_orphan(conn, loc["floorplan"])
    return RedirectResponse("/locations", status_code=303)


@app.post("/locations/{location_id}/floors")
def create_floor(location_id: int, name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM locations WHERE id = ?", (location_id,)).fetchone():
            raise HTTPException(404)
        # Append at the end so floor ordering matches creation order by default.
        next_sort = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM floors WHERE location_id = ?",
            (location_id,),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO floors (location_id, name, sort_order) VALUES (?, ?, ?)",
            (location_id, name, next_sort),
        )
        conn.commit()
    return RedirectResponse(
        f"/locations/{location_id}?floor={cur.lastrowid}&edit=1", status_code=303,
    )


@app.post("/floors/{floor_id}")
def edit_floor(floor_id: int, name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    with db() as conn:
        row = conn.execute(
            "SELECT location_id FROM floors WHERE id = ?", (floor_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        conn.execute("UPDATE floors SET name = ? WHERE id = ?", (name, floor_id))
        conn.commit()
    return RedirectResponse(
        f"/locations/{row['location_id']}?floor={floor_id}", status_code=303,
    )


@app.post("/floors/{floor_id}/floorplan")
async def upload_floor_floorplan(floor_id: int, image: UploadFile = File(...)):
    if not image or not image.filename:
        raise HTTPException(400, "Image required")
    with db() as conn:
        floor = conn.execute(
            "SELECT location_id, floorplan FROM floors WHERE id = ?", (floor_id,),
        ).fetchone()
        if not floor:
            raise HTTPException(404)
    new_name = save_photo_bytes(await image.read(), image.filename)
    with db() as conn:
        old = floor["floorplan"]
        conn.execute(
            "UPDATE floors SET floorplan = ? WHERE id = ?",
            (new_name, floor_id),
        )
        conn.commit()
        if old and old != new_name:
            _delete_upload_if_orphan(conn, old)
    return RedirectResponse(
        f"/locations/{floor['location_id']}?floor={floor_id}&edit=1", status_code=303,
    )


@app.post("/floors/{floor_id}/delete")
def delete_floor(floor_id: int):
    with db() as conn:
        floor = conn.execute(
            "SELECT location_id, floorplan FROM floors WHERE id = ?", (floor_id,),
        ).fetchone()
        if not floor:
            raise HTTPException(404)
        conn.execute("DELETE FROM floors WHERE id = ?", (floor_id,))
        conn.commit()
        if floor["floorplan"]:
            _delete_upload_if_orphan(conn, floor["floorplan"])
    return RedirectResponse(f"/locations/{floor['location_id']}", status_code=303)


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
    with db() as conn:
        floor = conn.execute(
            "SELECT location_id FROM floors WHERE id = ?", (floor_id,),
        ).fetchone()
        if not floor:
            raise HTTPException(404)
        color = _next_room_color(conn, floor["location_id"])
        cur = conn.execute(
            "INSERT INTO rooms (location_id, floor_id, name, x, y, w, h, color) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                floor["location_id"], floor_id, name,
                _clamp01(x), _clamp01(y), _clamp01(w), _clamp01(h), color,
            ),
        )
        conn.commit()
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "id": cur.lastrowid, "color": color, "name": name}
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
    with db() as conn:
        row = conn.execute(
            "SELECT location_id, floor_id, color FROM rooms WHERE id = ?", (room_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        new_color = color_val if color_val is not None else row["color"]
        conn.execute(
            "UPDATE rooms SET name = ?, x = ?, y = ?, w = ?, h = ?, color = ? WHERE id = ?",
            (name, _clamp01(x), _clamp01(y), _clamp01(w), _clamp01(h), new_color, room_id),
        )
        conn.commit()
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "color": new_color}
    target = f"/locations/{row['location_id']}?edit=1"
    if row["floor_id"]:
        target = f"/locations/{row['location_id']}?floor={row['floor_id']}&edit=1"
    return RedirectResponse(target, status_code=303)


@app.post("/rooms/{room_id}/delete")
def delete_room(request: Request, room_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT location_id, floor_id FROM rooms WHERE id = ?", (room_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
        conn.commit()
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True}
    target = f"/locations/{row['location_id']}?edit=1"
    if row["floor_id"]:
        target = f"/locations/{row['location_id']}?floor={row['floor_id']}&edit=1"
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
    with db() as conn:
        row = conn.execute(
            "SELECT r.*, l.name AS location_name, l.id AS location_id "
            "FROM rooms r JOIN locations l ON l.id = r.location_id "
            "WHERE r.id = ?",
            (room_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        boxes = conn.execute(
            "SELECT b.*, COUNT(i.id) AS item_count FROM boxes b "
            "LEFT JOIN items i ON i.box_id = b.id "
            "WHERE b.room_id = ? GROUP BY b.id ORDER BY b.name",
            (room_id,),
        ).fetchall()
        # Same per-box thumb-strip as index.html — limited to this room's
        # boxes to keep the query small.
        thumb_rows = conn.execute(
            "SELECT i.box_id, i.photo FROM items i "
            "JOIN boxes b ON b.id = i.box_id "
            "WHERE b.room_id = ? AND i.photo IS NOT NULL "
            "ORDER BY i.box_id, i.created_at DESC",
            (room_id,),
        ).fetchall()
    thumbs: dict[int, list[str]] = {}
    for r in thumb_rows:
        lst = thumbs.setdefault(r["box_id"], [])
        if len(lst) < 5:
            lst.append(r["photo"])
    return templates.TemplateResponse(
        request, "room_boxes.html",
        {"room": dict(row), "boxes": boxes, "thumbs": thumbs},
    )


def _referenced_uploads() -> set[str]:
    """All upload filenames referenced by any row in the DB, plus their
    derived thumbnail companions so the orphan sweep keeps both halves.

    This is the single source of truth for both /maintenance/cleanup
    (orphan deletion) and /maintenance/export (backup zip).  Any new
    file-bearing column MUST be added here — otherwise a fresh feature
    will silently leak files on cleanup AND drop them from backups.

    DB tables themselves are captured by zipping the whole stash.db, so
    DB-only additions (new tables, new non-file columns) need nothing."""
    refs: set[str] = set()
    with db() as conn:
        for sql in (
            "SELECT photo FROM items WHERE photo IS NOT NULL",
            "SELECT source_photo FROM items WHERE source_photo IS NOT NULL",
            "SELECT photo FROM pending_items WHERE photo IS NOT NULL",
            "SELECT photo FROM ingest_jobs WHERE photo IS NOT NULL",
            "SELECT background_art FROM boxes WHERE background_art IS NOT NULL",
            "SELECT floorplan FROM floors WHERE floorplan IS NOT NULL",
            "SELECT floorplan FROM locations WHERE floorplan IS NOT NULL",
        ):
            refs.update(r[0] for r in conn.execute(sql).fetchall())
    # The thumbs themselves aren't tracked in the DB but should follow
    # whatever their source does.
    refs.update(_thumb_path(name).name for name in list(refs))
    return refs


@app.get("/maintenance", response_class=HTMLResponse)
def maintenance(request: Request, cleaned: str = "", update: str = "", imported: str = ""):
    with db() as conn:
        item_count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        box_count = conn.execute("SELECT COUNT(*) FROM boxes").fetchone()[0]
    on_disk = sum(1 for _ in UPLOAD_DIR.iterdir()) if UPLOAD_DIR.exists() else 0
    referenced = len(_referenced_uploads())
    # Access-control panel: surface what oauth2-proxy hands us so the operator
    # can see who's currently signed in, and reflect the configured allowlist
    # so they don't have to SSH in to remember who has access. No state is
    # stored on stash's side — sessions are owned by the proxy.
    current_email = (request.headers.get("X-Forwarded-Email") or "").strip().lower()
    current_user = (request.headers.get("X-Forwarded-User") or "").strip()
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
            "allowed_emails": sorted(_ALLOWED_EMAILS),
            "fully_public": _FULLY_PUBLIC,
            "current_email": current_email,
            "current_user": current_user,
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
        for path in UPLOAD_DIR.iterdir():
            if path.is_file() and path.name not in refs:
                try:
                    path.unlink()
                    cleaned += 1
                except FileNotFoundError:
                    pass
    return RedirectResponse(f"/maintenance?cleaned={cleaned}", status_code=303)


@app.get("/maintenance/export")
def maintenance_export():
    """Stream a zip of stash.db + every upload file still referenced."""
    import io as _io
    import zipfile
    from datetime import datetime

    refs = _referenced_uploads()
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if DB_PATH.exists():
            zf.write(DB_PATH, arcname="stash.db")
        for name in sorted(refs):
            p = UPLOAD_DIR / name
            if p.exists():
                zf.write(p, arcname=f"uploads/{name}")
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
    through the SQLite library and is safe even with concurrent readers."""
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
    """Extract `uploads/<name>` entries into UPLOAD_DIR. Returns count restored.
    Skips entries with hostile names (path traversal, suspicious chars)."""
    import shutil
    upload_root = UPLOAD_DIR.resolve()
    count = 0
    for name in zf.namelist():
        if not name.startswith("uploads/") or name.endswith("/"):
            continue
        base = name[len("uploads/"):]
        if "/" in base or "\\" in base or ".." in base or not _UPLOAD_NAME_RE.match(base):
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
