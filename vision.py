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


class VisionError(RuntimeError):
    """Base class for vision-pipeline failures the ingest worker can
    surface back to the user with a readable explanation.  Carries
    a ``user_message`` separate from ``str(exc)`` so the wire-level
    detail (model name, raw API error) stays in logs while the
    queue card shows something the human can act on."""

    def __init__(self, user_message: str, *, debug: str | None = None):
        super().__init__(debug or user_message)
        self.user_message = user_message


class VisionBlockedError(VisionError):
    """Gemini's content filter refused the image.  Surfaces as the
    "Google blocked this photo" path in the queue UI — the user's
    options are upload-a-different-photo or skip-AI-entirely."""


def _classify_block_reason(prompt_feedback) -> str | None:
    """Convert Gemini's ``BlockReason`` enum to a plain-English
    fragment ("safety filter", "personal information", etc.).
    Returns ``None`` if no recognised block reason is set so the
    caller can fall through to the generic "no response" path."""
    if prompt_feedback is None:
        return None
    raw = getattr(prompt_feedback, "block_reason", None)
    if raw is None:
        return None
    # ``block_reason`` can be an enum, an int, or a string depending
    # on the SDK version.  Stringify defensively and lower-case
    # so the match table doesn't have to enumerate every shape.
    name = str(getattr(raw, "name", raw)).upper().replace("BLOCKREASON.", "")
    mapping = {
        "SAFETY": "safety filter",
        "PROHIBITED_CONTENT": "prohibited-content filter",
        "BLOCKLIST": "Google's content blocklist",
        "OTHER": "Google's content filter",
        "PROHIBITED": "prohibited-content filter",
        "SPII": "personal-information filter",
        "IMAGE_SAFETY": "image safety filter",
    }
    return mapping.get(name, f"Google's content filter ({name.lower()})")


def detect_items(
    image_bytes: bytes,
    media_type: str = "image/jpeg",
    *,
    scope: str = "auto",
) -> list[DetectedItem]:
    """Stage 1: Gemini vision — detect items with bounding boxes.

    ``scope`` hints the prompt:

    * ``"auto"`` (default) — list every distinct item.  Used when
      the photo's contents are unknown.
    * ``"single"`` — assume the photo shows ONE item, return
      exactly one entry.  Use when the user knows they shot a
      single thing on a counter and the AI was over-detecting
      (e.g. seeing the texture of the bag as another item).
    * ``"many"`` — explicitly tell the AI to expect a pile, list
      everything, group sets generously.  Same baseline behaviour
      as auto but stronger framing for crowded photos.
    """
    s = (scope or "auto").lower()
    if s == "single":
        instruction = (
            "This photo shows ONE physical item the user wants to catalog. "
            "Return exactly ONE entry — the primary subject — with a bounding "
            "box around it. Do NOT split it into parts, do not detect packaging "
            "or backgrounds as separate items.\n\n"
            "Respond with ONLY valid JSON, no markdown fences:\n"
            '{"items": [{"name": "short name", "description": "brief description", '
            '"bbox": [y_min, x_min, y_max, x_max]}]}\n\n'
            "Bounding box coordinates must be in the range 0-1000 "
            "(normalized to image dimensions)."
        )
    elif s == "many":
        instruction = (
            "This photo shows MANY items spread out for cataloging. "
            "List every distinct physical item. Group obvious sets "
            "(e.g. 'set of 4 mugs') as one item. Skip background, "
            "furniture, and the container/surface holding items. "
            "Be thorough but do not invent items that aren't visible.\n\n"
            "Respond with ONLY valid JSON, no markdown fences:\n"
            '{"items": [{"name": "short name", "description": "brief description", '
            '"bbox": [y_min, x_min, y_max, x_max]}]}\n\n'
            "Bounding box coordinates must be in the range 0-1000 "
            "(normalized to image dimensions)."
        )
    else:
        instruction = (
            "List every distinct physical item in this photo that someone might want to "
            "store and catalog. For each item, provide a bounding box.\n\n"
            "Group obvious sets (e.g. 'set of 4 mugs') as one item. "
            "Skip background, furniture, and the container/surface holding items.\n\n"
            "Respond with ONLY valid JSON, no markdown fences:\n"
            '{"items": [{"name": "short name", "description": "brief description", '
            '"bbox": [y_min, x_min, y_max, x_max]}]}\n\n'
            "Bounding box coordinates must be in the range 0-1000 "
            "(normalized to image dimensions)."
        )
    response = get_gemini().models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            genai.types.Part.from_bytes(data=image_bytes, mime_type=media_type),
            genai.types.Part.from_text(text=instruction),
        ],
    )

    # ── Failure shapes that don't raise but produce no usable text ──
    # The previous failure mode was that ``response.text`` returned
    # ``None`` when Gemini blocked the image for content reasons,
    # and the next line ``response.text.strip()`` crashed with
    # "'NoneType' object has no attribute 'strip'" — a cryptic
    # AttributeError that bubbled all the way to the user as the
    # ingest job's error message ("missing strip error" per the
    # operator report).  Real users now get a readable message
    # naming the actual problem.
    text = response.text
    if text is None or not str(text).strip():
        # Reason 1: explicit content-filter block.  Easiest to
        # detect via the ``prompt_feedback`` block_reason.
        block_label = _classify_block_reason(
            getattr(response, "prompt_feedback", None)
        )
        if block_label:
            raise VisionBlockedError(
                f"Gemini refused this photo via {block_label}.  "
                "Re-shoot the item from a different angle, crop the "
                "frame tighter on the object, or replace the photo "
                "with one that's less likely to trip the filter.  "
                "You can also skip the AI suggestion and enter the "
                "item details by hand.",
                debug=f"prompt_feedback={response.prompt_feedback!r}",
            )
        # Reason 2: per-candidate finish_reason carries the block on
        # some SDK versions even when prompt_feedback is empty.
        finish_reason = None
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            finish_reason = getattr(candidates[0], "finish_reason", None)
        finish_name = str(
            getattr(finish_reason, "name", finish_reason)
        ).upper()
        if "SAFETY" in finish_name or "PROHIBITED" in finish_name:
            raise VisionBlockedError(
                "Gemini stopped processing this photo for content-"
                "safety reasons.  Re-shoot the item from a different "
                "angle, crop tighter on the object, or enter the item "
                "details by hand and skip the AI suggestion.",
                debug=f"finish_reason={finish_reason!r}",
            )
        # Reason 3: empty response with no obvious block.  Could be
        # a model glitch, rate limit, or a corrupted image.  At
        # least name the symptom so the user knows it's worth a
        # retry instead of staring at a NoneType error.
        raise VisionError(
            "Gemini returned an empty response for this photo — no "
            "items detected and no explanation given.  Hit Retry to "
            "try again, or replace the photo if the issue persists.",
            debug=f"response={response!r}",
        )

    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # Gemini sometimes returns prose explaining why it didn't
        # comply ("I can't help with that image…") instead of JSON.
        # Surface a hint at the first 200 chars so the user can see
        # what Gemini actually said.
        snippet = text[:200].replace("\n", " ").strip()
        raise VisionError(
            "Gemini's response wasn't valid JSON — the model returned "
            f'free-form text instead.  Snippet: "{snippet}".  Retry, or '
            "replace the photo with a clearer shot.",
            debug=f"json_error={exc!r} text={text!r}",
        ) from exc
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


class TagSuggestions(BaseModel):
    tags: list[str] = Field(
        description=(
            "3–5 short, lowercase, kebab-case tags.  Prefer tags from "
            "the existing catalog when applicable; only invent a new "
            "name when none of the existing ones fit."
        ),
    )


_TAG_SUGGEST_INSTRUCTIONS = (
    "You're helping organise a household inventory.  Suggest 3-5 short "
    "tags (lowercase, kebab-case, one or two words each — e.g. "
    "'kitchen', 'fragile', 'serial:numbered'). Prefer tags from the "
    "existing catalog when they fit so the user's tag namespace stays "
    "tight; only propose a new tag when none of the existing ones do.  "
    "Skip generic words like 'item', 'thing', 'stuff'.  Don't include "
    "spaces — use hyphens.  Don't repeat tags."
)


def _format_existing_tags(existing_tags: list[str] | None) -> str:
    if not existing_tags:
        return "(no existing tags yet)"
    # Soft cap so a stash with 500 tags doesn't blow the prompt budget.
    head = existing_tags[:120]
    listing = ", ".join(head)
    if len(existing_tags) > len(head):
        listing += f", … ({len(existing_tags) - len(head)} more)"
    return listing


def suggest_tags_for_item(
    name: str,
    description: str,
    photo_bytes: bytes | None = None,
    existing_tags: list[str] | None = None,
) -> list[str]:
    """Gemini-suggested tags for a single item.  Sends the item's name
    + notes + optional photo + the tenant's existing tag catalog;
    returns 3-5 tag names, deduplicated and stripped.

    The Pydantic schema constraint forces a parseable list; if Gemini
    hallucinates an empty response we return an empty list rather
    than raising — the caller surfaces "no suggestions" instead of
    a 502.
    """
    from google.genai import types as _genai_types
    prompt = (
        f"{_TAG_SUGGEST_INSTRUCTIONS}\n\n"
        f"Item:\n  name: {name}\n  notes: {description or '(none)'}\n\n"
        f"Existing tag catalog: {_format_existing_tags(existing_tags)}"
    )
    parts: list = [prompt]
    if photo_bytes:
        parts.append(_genai_types.Part.from_bytes(
            data=photo_bytes, mime_type="image/jpeg",
        ))
    response = get_gemini().models.generate_content(
        model=GEMINI_MODEL,
        contents=parts,
        config=_genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=TagSuggestions,
        ),
    )
    parsed = _parse_tag_response(response)
    return _clean_tag_list(parsed)


def suggest_tags_for_box(
    box_name: str,
    box_notes: str,
    items: list[dict],
    existing_tags: list[str] | None = None,
) -> list[str]:
    """Tags that apply across the items in a box — e.g. a box of
    kitchenware gets ('kitchen', 'utensils', 'cookware') back.

    ``items`` is a list of ``{name, notes}`` dicts (notes optional).
    Photos aren't threaded through to keep the prompt cheap and the
    cost predictable on large boxes; the per-item flow is the way
    to incorporate photo context.
    """
    from google.genai import types as _genai_types
    if not items:
        return []
    head = items[:30]  # soft cap; 30 items is plenty of context
    listing = "\n".join(
        f"- {it.get('name', '(unnamed)')}" + (
            f" — {it['notes']}" if it.get("notes") else ""
        )
        for it in head
    )
    if len(items) > len(head):
        listing += f"\n…(+{len(items) - len(head)} more items)"

    prompt = (
        f"{_TAG_SUGGEST_INSTRUCTIONS}\n\n"
        f"Box name: {box_name}\n"
        f"Box notes: {box_notes or '(none)'}\n\n"
        f"Items in the box:\n{listing}\n\n"
        "Suggest tags that apply across MOST of these items — broad "
        "themes the user can stamp on every item at once.  Don't "
        "include item-specific oddities that wouldn't generalise.\n\n"
        f"Existing tag catalog: {_format_existing_tags(existing_tags)}"
    )
    response = get_gemini().models.generate_content(
        model=GEMINI_MODEL,
        contents=[prompt],
        config=_genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=TagSuggestions,
        ),
    )
    parsed = _parse_tag_response(response)
    return _clean_tag_list(parsed)


def _parse_tag_response(response) -> list[str]:
    """Extract the ``tags`` list from a Gemini ``generate_content``
    response.  Returns ``[]`` for any shape we can't parse rather
    than raising — the caller treats "no suggestions" as a normal
    outcome."""
    text = getattr(response, "text", None) or ""
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        tags = payload.get("tags")
    else:
        tags = payload
    if not isinstance(tags, list):
        return []
    return [str(t) for t in tags if t]


def _clean_tag_list(tags: list[str]) -> list[str]:
    """Trim, lowercase, dedupe (preserve order), strip stray
    whitespace + leading/trailing punctuation that the model
    occasionally appends ('kitchen,' instead of 'kitchen')."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in tags:
        t = raw.strip().strip(".,;:").lower()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out[:5]


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
