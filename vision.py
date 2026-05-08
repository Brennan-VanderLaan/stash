"""Vision pipeline: Gemini for item detection + bounding boxes, Claude for box matching."""

import base64
import io
import json
import os
from typing import Optional
from pydantic import BaseModel, Field
import anthropic
from google import genai

CLAUDE_MODEL = "claude-opus-4-6"
GEMINI_MODEL = "gemini-2.5-flash"
# Nano Banana 2 — Gemini 3 Pro Image. Override via STASH_NANO_BANANA_MODEL if
# Google ships the GA name under a different ID later.
NANO_BANANA_MODEL = os.environ.get("STASH_NANO_BANANA_MODEL", "gemini-3-pro-image-preview")

_anthropic_client: Optional[anthropic.Anthropic] = None
_gemini_client: Optional[genai.Client] = None


def get_anthropic() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


def get_gemini() -> genai.Client:
    """Construct (or return cached) Gemini client.

    A request-level timeout is required: a hung Gemini call wedges
    the ingest worker indefinitely (no exception, no log) which
    leaves the user staring at a "processing" job that never
    finishes.  Default: 120 s — long enough for a normal flash
    detect on a slow link, short enough that a stuck call gets
    converted to a failed-job row the user can retry.

    Override via ``STASH_GEMINI_TIMEOUT_MS`` for environments
    that genuinely need longer (e.g. spotty rural connectivity).
    """
    global _gemini_client
    if _gemini_client is None:
        from google.genai import types as _genai_types
        timeout_ms = int(os.environ.get("STASH_GEMINI_TIMEOUT_MS", "120000"))
        _gemini_client = genai.Client(
            api_key=os.environ.get("GEMINI_API_KEY"),
            http_options=_genai_types.HttpOptions(timeout=timeout_ms),
        )
    return _gemini_client


class DetectedItem(BaseModel):
    name: str = Field(description="Short name for the item")
    description: str = Field(description="Brief description")
    # Bounding box in Gemini's 0-1000 coordinate space (normalized)
    # [y_min, x_min, y_max, x_max]
    bbox: Optional[list[int]] = Field(default=None, description="[y_min, x_min, y_max, x_max] in 0-1000 coords")


class BoxMatch(BaseModel):
    match: str = Field(description="Either 'existing' or 'new'")
    box_id: Optional[int] = Field(default=None)
    new_box_name: Optional[str] = Field(default=None)
    new_box_location: Optional[str] = Field(default=None)
    reason: str = Field(description="One sentence explaining the choice")


def detect_items(image_bytes: bytes, media_type: str = "image/jpeg") -> list[DetectedItem]:
    """Stage 1: Gemini vision — detect items with bounding boxes."""
    response = get_gemini().models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            genai.types.Part.from_bytes(data=image_bytes, mime_type=media_type),
            genai.types.Part.from_text(
                text="List every distinct physical item in this photo that someone might want to "
                "store and catalog. For each item, provide a bounding box.\n\n"
                "Group obvious sets (e.g. 'set of 4 mugs') as one item. "
                "Skip background, furniture, and the container/surface holding items.\n\n"
                "Respond with ONLY valid JSON, no markdown fences:\n"
                '{"items": [{"name": "short name", "description": "brief description", '
                '"bbox": [y_min, x_min, y_max, x_max]}]}\n\n'
                "Bounding box coordinates must be in the range 0-1000 "
                "(normalized to image dimensions)."
            ),
        ],
    )

    text = response.text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    data = json.loads(text)
    items = []
    for entry in data.get("items", []):
        bbox = entry.get("bbox")
        if bbox and len(bbox) == 4:
            bbox = [max(0, min(1000, int(v))) for v in bbox]
        else:
            bbox = None
        items.append(DetectedItem(
            name=entry.get("name", "unknown"),
            description=entry.get("description", ""),
            bbox=bbox,
        ))
    return items


def generate_label_art(
    name: str,
    description: str = "",
    items: list[dict] | None = None,
    item_photos: list[tuple[bytes, str]] | None = None,
) -> bytes:
    """Generate playful background art for a printed label using Nano Banana 2.

    The result is downscaled and JPEG-encoded so it fits cleanly in a label
    SVG (embedded as a base64 data URI). Returns the raw image bytes.

    `items` is a list of {name, notes} dicts pulled from the box's contents —
    they ground the prompt in what's actually inside instead of guessing from
    the box name alone. `item_photos` is up to a few (bytes, mime_type) pairs
    sent as multimodal Parts so the model can match the actual look of the
    contents, not just their text labels."""
    items = items or []
    item_photos = item_photos or []

    items_text = ""
    if items:
        listing = "\n".join(
            "- " + (it.get("name") or "?") +
            (f": {it['notes']}" if it.get("notes") else "")
            for it in items[:12]
        )
        items_text = (
            "\n\nKnown items in this box (use these to ground the illustration in "
            "what's actually inside; pick a recognizable subset, don't try to draw "
            "all of them):\n"
            f"{listing}"
        )

    photos_note = ""
    if item_photos:
        photos_note = (
            "\n\nReference photos of items currently in the box are attached. "
            "Match the general vibe of the contents (kind of stuff, color palette, "
            "vibe) — but render them in the watercolor sketch style described "
            "below, not photographically. Don't redraw photo backgrounds, just "
            "the items themselves."
        )

    prompt = (
        "Create a playful illustration for the BACKGROUND of a storage-box label. "
        "It will be composited at ~32% opacity behind the box name and a QR code, "
        "so the image must read clearly even when faded.\n\n"
        f"Box name: {name}\n"
        f"Likely contents: {description or '(unspecified — surprise me with something fun based on the name)'}"
        f"{items_text}"
        f"{photos_note}"
        "\n\nStyle requirements (apply consistently across every label):\n"
        "- Pencil and pen sketch with watercolor paints for color. Visible graphite "
        "and ink lines, loose hand-drawn feel, soft watercolor washes for fills with "
        "a little bleed at the edges.\n"
        "- Bright clean white paper background. Cheerful, well-saturated colors but "
        "no heavy dark fills.\n"
        "- NO TEXT, NO LETTERS, NO NUMBERS in the image — label typography sits over the same area.\n"
        "- Wide aspect ratio (around 16:9).\n"
        "- Compose the subject so it remains recognizable when the right ~30% of the image is partially obscured by typography.\n"
        "- One clear focal idea — not a busy collage."
    )

    contents = [prompt]
    for photo_bytes, mime in item_photos[:3]:
        contents.append(genai.types.Part.from_bytes(data=photo_bytes, mime_type=mime))

    response = get_gemini().models.generate_content(
        model=NANO_BANANA_MODEL,
        contents=contents,
    )

    image_bytes = _extract_image_bytes(response)
    if image_bytes is None:
        raise RuntimeError("Nano Banana 2 returned no image data")

    # Downscale + JPEG so the embedded data URI doesn't bloat the label SVG.
    from PIL import Image
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((1024, 1024), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=82, optimize=True)
    return out.getvalue()


def _extract_image_bytes(response) -> bytes | None:
    """Pull image bytes out of a genai response across SDK versions.

    Newer SDKs expose `part.as_image()` returning a PIL Image. Older ones
    surface raw bytes via `part.inline_data.data`. Try both."""
    candidates = getattr(response, "candidates", None) or []
    for cand in candidates:
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            if inline is not None:
                data = getattr(inline, "data", None)
                if isinstance(data, (bytes, bytearray)):
                    return bytes(data)
                if isinstance(data, str):
                    # SDKs that hand you back base64
                    try:
                        return base64.b64decode(data)
                    except Exception:
                        pass
    return None


def suggest_box(item_name: str, item_description: str, boxes: list[dict]) -> BoxMatch:
    """Stage 2: Claude matching — pick best existing box or propose a new one."""
    if boxes:
        listing = "\n".join(
            f"- id={b['id']}: {b['name']}"
            + (f" (location: {b['location']})" if b.get("location") else "")
            + (f" — notes: {b['notes']}" if b.get("notes") else "")
            for b in boxes
        )
    else:
        listing = "(no existing boxes yet)"

    prompt = (
        f"Item to file away:\n  name: {item_name}\n  description: {item_description}\n\n"
        f"Existing boxes:\n{listing}\n\n"
        "Pick the single best existing box for this item, or propose a new box if none "
        "are a reasonable fit. Prefer existing boxes when they're plausible — only propose "
        "a new box when the item really doesn't belong with any current group."
    )

    response = get_anthropic().messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
        output_format=BoxMatch,
    )
    return response.parsed_output
