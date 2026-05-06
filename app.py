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
        # Backfill source_photo for items created before this column existed
        conn.execute(
            "UPDATE items SET source_photo = photo WHERE source_photo IS NULL"
        )
        conn.commit()


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
        return name
    except HTTPException:
        raise
    except Exception:
        name = f"{secrets.token_hex(8)}.jpg"
        (UPLOAD_DIR / name).write_bytes(data)
        return name


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
    Returns the filename of the cropped image saved to UPLOAD_DIR."""
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
    ext = Path(photo_name).suffix.lower() or ".jpg"
    crop_name = f"{secrets.token_hex(8)}{ext}"
    cropped.save(UPLOAD_DIR / crop_name)
    return crop_name


def _photo_still_referenced(conn, photo_name: str) -> bool:
    """True if any row still points at this upload file."""
    if not photo_name:
        return True
    return conn.execute(
        "SELECT 1 FROM items WHERE photo = ? OR source_photo = ? "
        "UNION SELECT 1 FROM pending_items WHERE photo = ? "
        "UNION SELECT 1 FROM ingest_jobs WHERE photo = ? "
        "UNION SELECT 1 FROM boxes WHERE background_art = ? LIMIT 1",
        (photo_name, photo_name, photo_name, photo_name, photo_name),
    ).fetchone() is not None


def _delete_upload_if_orphan(conn, photo_name: str) -> None:
    if not photo_name or _photo_still_referenced(conn, photo_name):
        return
    try:
        (UPLOAD_DIR / photo_name).unlink()
    except FileNotFoundError:
        pass


import re as _re
_UPLOAD_NAME_RE = _re.compile(r"^[A-Za-z0-9._-]+$")


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


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with db() as conn:
        boxes = conn.execute(
            "SELECT b.*, COUNT(i.id) AS item_count FROM boxes b "
            "LEFT JOIN items i ON i.box_id = b.id GROUP BY b.id ORDER BY b.created_at DESC"
        ).fetchall()
        # Up to 5 most-recent item photos per box, for the preview strip
        thumb_rows = conn.execute(
            "SELECT box_id, photo FROM items "
            "WHERE photo IS NOT NULL ORDER BY box_id, created_at DESC"
        ).fetchall()
    thumbs: dict[int, list[str]] = {}
    for r in thumb_rows:
        lst = thumbs.setdefault(r["box_id"], [])
        if len(lst) < 5:
            lst.append(r["photo"])
    return templates.TemplateResponse(
        request, "index.html", {"boxes": boxes, "thumbs": thumbs},
    )


@app.post("/boxes")
def create_box(name: str = Form(...), location: str = Form(""), notes: str = Form("")):
    with db() as conn:
        conn.execute(
            "INSERT INTO boxes (name, location, notes) VALUES (?, ?, ?)",
            (name.strip(), location.strip(), notes.strip()),
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
        box = conn.execute("SELECT * FROM boxes WHERE id = ?", (box_id,)).fetchone()
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
        all_tags = conn.execute("SELECT name FROM tags ORDER BY name").fetchall()
    return templates.TemplateResponse(
        request, "box.html",
        {
            "box": box,
            "items_with_tags": items_with_tags,
            "other_boxes": other_boxes,
            "locations": locations,
            "all_tags": [r["name"] for r in all_tags],
        },
    )


@app.post("/boxes/{box_id}/edit")
def edit_box(
    box_id: int,
    name: str = Form(...),
    location: str = Form(""),
    notes: str = Form(""),
):
    if not name.strip():
        raise HTTPException(400, "Name required")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM boxes WHERE id = ?", (box_id,)).fetchone():
            raise HTTPException(404)
        conn.execute(
            "UPDATE boxes SET name = ?, location = ?, notes = ? WHERE id = ?",
            (name.strip(), location.strip(), notes.strip(), box_id),
        )
        conn.commit()
    return RedirectResponse(f"/boxes/{box_id}", status_code=303)


@app.post("/items/{item_id}/move")
def move_item(item_id: int, box_id: int = Form(...)):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
            raise HTTPException(404)
        if not conn.execute("SELECT 1 FROM boxes WHERE id = ?", (box_id,)).fetchone():
            raise HTTPException(400, "Unknown box")
        conn.execute("UPDATE items SET box_id = ? WHERE id = ?", (box_id, item_id))
        conn.commit()
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


def process_ingest_job(job_id: int, photo_name: str) -> None:
    """Background worker: vision pass → insert pending items → mark job done."""
    with db() as conn:
        conn.execute("UPDATE ingest_jobs SET status = 'processing' WHERE id = ?", (job_id,))
        conn.commit()
    try:
        image_bytes = (UPLOAD_DIR / photo_name).read_bytes()
        media_type = _EXT_TO_MIME.get(Path(photo_name).suffix.lower(), "image/jpeg")
        detected = vision.detect_items(image_bytes, media_type=media_type)
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


@app.get("/ingest", response_class=HTMLResponse)
def ingest_form(request: Request):
    with db() as conn:
        jobs = conn.execute(
            "SELECT * FROM ingest_jobs WHERE status != 'done' "
            "ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    return templates.TemplateResponse(request, "ingest.html", {"jobs": jobs})


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
            "SELECT id, name, location FROM boxes ORDER BY name"
        ).fetchall()
        all_tags = [r["name"] for r in conn.execute("SELECT name FROM tags ORDER BY name").fetchall()]
    import hashlib
    payload = "|".join(
        f"{r['id']}:{r['name']}:{r['description']}:{r['suggested_box_id']}:"
        f"{r['suggested_new_box_name']}:{r['suggestion_reason']}"
        for r in pending
    ) + "||" + "|".join(f"{b['id']}:{b['name']}" for b in boxes)
    fingerprint = hashlib.sha1(payload.encode()).hexdigest()
    return templates.TemplateResponse(
        request, "queue.html",
        {"pending": pending, "boxes": boxes, "fingerprint": fingerprint, "all_tags": all_tags},
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


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", tag: str = ""):
    with db() as conn:
        all_tags = [
            r["name"] for r in conn.execute("SELECT name FROM tags ORDER BY name").fetchall()
        ]
        clauses = ["1=1"]
        params: list = []
        if q.strip():
            clauses.append("(i.name LIKE ? OR i.notes LIKE ?)")
            like = f"%{q.strip()}%"
            params.extend([like, like])
        if tag.strip():
            clauses.append(
                "i.id IN (SELECT it.item_id FROM item_tags it "
                "JOIN tags t ON t.id = it.tag_id WHERE t.name = ?)"
            )
            params.append(tag.strip())
        where = " AND ".join(clauses)
        results = conn.execute(
            f"SELECT i.*, b.name AS box_name, b.id AS bid "
            f"FROM items i JOIN boxes b ON b.id = i.box_id "
            f"WHERE {where} ORDER BY i.name LIMIT 200",
            params,
        ).fetchall()
    return templates.TemplateResponse(
        request, "search.html",
        {"results": results, "q": q, "tag": tag, "all_tags": all_tags},
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


@app.post("/boxes/{box_id}/generate-art")
def generate_box_art(box_id: int, next_url: str = Form("/labels")):
    """Synchronously generate label background art via Nano Banana 2.

    Synchronous because it's user-triggered (one box at a time from the
    labels page) and the model takes ~10-20s — short enough to wait. The
    old image, if any, is cleaned up only after the new one writes
    successfully so a failed generation doesn't strand the box without art."""
    with db() as conn:
        box = conn.execute(
            "SELECT id, name, notes, background_art FROM boxes WHERE id = ?",
            (box_id,),
        ).fetchone()
        if not box:
            raise HTTPException(404)
    try:
        image_bytes = vision.generate_label_art(box["name"], box["notes"] or "")
    except Exception as e:
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

    # Only redirect to whitelisted internal targets — never honor an absolute
    # URL or one that escapes the app, even with the form coming from us.
    target = next_url if next_url.startswith("/") and not next_url.startswith("//") else "/labels"
    return RedirectResponse(target, status_code=303)


@app.post("/boxes/{box_id}/clear-art")
def clear_box_art(box_id: int, next_url: str = Form("/labels")):
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
    target = next_url if next_url.startswith("/") and not next_url.startswith("//") else "/labels"
    return RedirectResponse(target, status_code=303)


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


def _referenced_uploads() -> set[str]:
    """All upload filenames referenced by any row in the DB."""
    refs: set[str] = set()
    with db() as conn:
        for sql in (
            "SELECT photo FROM items WHERE photo IS NOT NULL",
            "SELECT source_photo FROM items WHERE source_photo IS NOT NULL",
            "SELECT photo FROM pending_items WHERE photo IS NOT NULL",
            "SELECT photo FROM ingest_jobs WHERE photo IS NOT NULL",
            "SELECT background_art FROM boxes WHERE background_art IS NOT NULL",
        ):
            refs.update(r[0] for r in conn.execute(sql).fetchall())
    return refs


@app.get("/maintenance", response_class=HTMLResponse)
def maintenance(request: Request, cleaned: str = "", update: str = "", imported: str = ""):
    with db() as conn:
        item_count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        box_count = conn.execute("SELECT COUNT(*) FROM boxes").fetchone()[0]
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
