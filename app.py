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

app = FastAPI()
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")


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
        conn.commit()


init_db()
migrate_db()


def save_photo(photo: UploadFile | None) -> str | None:
    if not photo or not photo.filename:
        return None
    return save_photo_bytes(photo.file.read(), photo.filename)


def save_photo_bytes(data: bytes, filename: str) -> str:
    ext = Path(filename).suffix.lower() or ".jpg"
    name = f"{secrets.token_hex(8)}{ext}"
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


def crop_photo(photo_name: str, bbox: tuple[int, int, int, int]) -> str:
    """Crop a photo using bbox (y_min, x_min, y_max, x_max in 0-1000 coords).
    Returns the filename of the cropped image saved to UPLOAD_DIR."""
    from PIL import Image
    src = UPLOAD_DIR / photo_name
    img = Image.open(src)
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


@app.get("/uploads/{name}")
def serve_upload(name: str):
    p = UPLOAD_DIR / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with db() as conn:
        boxes = conn.execute(
            "SELECT b.*, COUNT(i.id) AS item_count FROM boxes b "
            "LEFT JOIN items i ON i.box_id = b.id GROUP BY b.id ORDER BY b.created_at DESC"
        ).fetchall()
    return templates.TemplateResponse(request, "index.html", {"boxes": boxes})


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
    return RedirectResponse(f"/boxes/{box_id}", status_code=303)


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
            "INSERT INTO items (box_id, name, notes, photo) VALUES (?, ?, ?, ?)",
            (box_id, name.strip(), notes.strip(), photo_name),
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
    return RedirectResponse(f"/boxes/{row['box_id']}", status_code=303)


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
    return RedirectResponse(f"/boxes/{row['box_id']}", status_code=303)


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

        if skip_crop.strip() == "1":
            # Undo crop — revert to full source image
            new_photo = source
        elif crop_y_min.strip() and crop_x_min.strip() and crop_y_max.strip() and crop_x_max.strip():
            bbox = (int(crop_y_min), int(crop_x_min), int(crop_y_max), int(crop_x_max))
            new_photo = crop_photo(source, bbox)
        else:
            # No change
            return RedirectResponse(f"/boxes/{item['box_id']}", status_code=303)

        conn.execute(
            "UPDATE items SET photo = ? WHERE id = ?", (new_photo, item_id)
        )
        conn.commit()
    return RedirectResponse(f"/boxes/{item['box_id']}", status_code=303)


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
        # Clean up photo files that are no longer referenced anywhere
        for photo_name in {row["photo"], row["source_photo"]}:
            if not photo_name:
                continue
            still_used = conn.execute(
                "SELECT 1 FROM items WHERE photo = ? OR source_photo = ? "
                "UNION SELECT 1 FROM pending_items WHERE photo = ? LIMIT 1",
                (photo_name, photo_name, photo_name),
            ).fetchone()
            if not still_used:
                try:
                    (UPLOAD_DIR / photo_name).unlink()
                except FileNotFoundError:
                    pass
    return RedirectResponse(f"/boxes/{row['box_id']}", status_code=303)


def process_ingest_job(job_id: int, photo_name: str, media_type: str) -> None:
    """Background worker: vision pass → insert pending items → mark job done."""
    with db() as conn:
        conn.execute("UPDATE ingest_jobs SET status = 'processing' WHERE id = ?", (job_id,))
        conn.commit()
    try:
        image_bytes = (UPLOAD_DIR / photo_name).read_bytes()
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
async def ingest(background_tasks: BackgroundTasks, photo: UploadFile = File(...)):
    if not photo or not photo.filename:
        raise HTTPException(400, "Photo required")
    image_bytes = await photo.read()
    media_type = photo.content_type or "image/jpeg"
    photo_name = save_photo_bytes(image_bytes, photo.filename)

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO ingest_jobs (photo, status) VALUES (?, 'pending')", (photo_name,)
        )
        job_id = cur.lastrowid
        conn.commit()

    background_tasks.add_task(process_ingest_job, job_id, photo_name, media_type)
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
    import hashlib
    payload = "|".join(
        f"{r['id']}:{r['name']}:{r['description']}:{r['suggested_box_id']}:"
        f"{r['suggested_new_box_name']}:{r['suggestion_reason']}"
        for r in pending
    ) + "||" + "|".join(f"{b['id']}:{b['name']}" for b in boxes)
    fingerprint = hashlib.sha1(payload.encode()).hexdigest()
    return templates.TemplateResponse(
        request, "queue.html",
        {"pending": pending, "boxes": boxes, "fingerprint": fingerprint},
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
        # Only delete photo file if no other pending or item references it
        photo = row["photo"]
        if photo:
            still_used = conn.execute(
                "SELECT 1 FROM pending_items WHERE photo = ? "
                "UNION SELECT 1 FROM items WHERE photo = ? LIMIT 1",
                (photo, photo),
            ).fetchone()
            if not still_used:
                try:
                    (UPLOAD_DIR / photo).unlink()
                except FileNotFoundError:
                    pass
        conn.commit()
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


@app.get("/boxes/{box_id}/label.svg")
def box_label_svg(box_id: int):
    with db() as conn:
        box = conn.execute("SELECT * FROM boxes WHERE id = ?", (box_id,)).fetchone()
        if not box:
            raise HTTPException(404)
    svg = labels.render_label_svg(box["id"], box["name"], box["location"] or "")
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Content-Disposition": f'attachment; filename="box-{box_id}-label.svg"'},
    )


@app.get("/labels", response_class=HTMLResponse)
def labels_page(request: Request):
    with db() as conn:
        boxes = conn.execute("SELECT * FROM boxes ORDER BY name").fetchall()
    return templates.TemplateResponse(request, "labels.html", {"boxes": boxes})


@app.get("/labels/sheet.svg")
def labels_sheet(request: Request):
    box_ids_raw = request.query_params.getlist("box_ids")
    with db() as conn:
        if box_ids_raw:
            placeholders = ",".join("?" * len(box_ids_raw))
            boxes = conn.execute(
                f"SELECT id, name, location FROM boxes WHERE id IN ({placeholders}) ORDER BY name",
                [int(b) for b in box_ids_raw],
            ).fetchall()
        else:
            boxes = conn.execute(
                "SELECT id, name, location FROM boxes ORDER BY name"
            ).fetchall()
    svg = labels.render_sheet_svg([dict(b) for b in boxes])
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Content-Disposition": 'attachment; filename="stash-labels.svg"'},
    )


@app.post("/boxes/{box_id}/delete")
def delete_box(box_id: int):
    with db() as conn:
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
