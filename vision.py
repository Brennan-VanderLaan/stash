import base64
from typing import Optional
from pydantic import BaseModel, Field
import anthropic

MODEL = "claude-opus-4-6"
_client: Optional[anthropic.Anthropic] = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


class DetectedItem(BaseModel):
    name: str = Field(description="Short name for the item, e.g. 'wooden spatula'")
    description: str = Field(description="One-sentence description: material, color, size, distinguishing features")


class DetectedItems(BaseModel):
    items: list[DetectedItem]


class BoxMatch(BaseModel):
    match: str = Field(description="Either 'existing' or 'new'")
    box_id: Optional[int] = Field(default=None, description="ID of an existing box if match='existing'")
    new_box_name: Optional[str] = Field(default=None, description="Suggested name for a new box if match='new'")
    new_box_location: Optional[str] = Field(default=None, description="Suggested storage location for the new box")
    reason: str = Field(description="One sentence explaining the choice")


def detect_items(image_bytes: bytes, media_type: str = "image/jpeg") -> list[DetectedItem]:
    """Stage 1: vision pass — list every distinct item in the photo."""
    response = get_client().messages.parse(
        model=MODEL,
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.standard_b64encode(image_bytes).decode(),
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "List every distinct physical item visible in this photo that someone might want to "
                        "store and catalog. Group obvious sets (e.g. 'set of 4 mugs') as one item. Skip "
                        "background, furniture, and the container/surface holding the items."
                    ),
                },
            ],
        }],
        output_format=DetectedItems,
    )
    return response.parsed_output.items


def suggest_box(item_name: str, item_description: str, boxes: list[dict]) -> BoxMatch:
    """Stage 2: matching pass — pick best existing box or propose a new one."""
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

    response = get_client().messages.parse(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
        output_format=BoxMatch,
    )
    return response.parsed_output
