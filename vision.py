"""Vision pipeline: Gemini for item detection + bounding boxes, Claude for box matching."""

import base64
import json
import os
from typing import Optional
from pydantic import BaseModel, Field
import anthropic
from google import genai

CLAUDE_MODEL = "claude-opus-4-6"
GEMINI_MODEL = "gemini-2.0-flash"

_anthropic_client: Optional[anthropic.Anthropic] = None
_gemini_client: Optional[genai.Client] = None


def get_anthropic() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


def get_gemini() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
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
                "List every distinct physical item in this photo that someone might want to "
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
