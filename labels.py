"""Generate SVG labels for boxes — individual and full-sheet grids."""

import io
import qrcode
import qrcode.image.svg
from xml.etree import ElementTree as ET


LABEL_W_MM = 203.2   # 8 inches
LABEL_H_MM = 63.5    # 2.5 inches
QR_SIZE_MM = 50
MARGIN_MM = 6

# Sheet: letter-size, 1 column × 4 rows = 4 labels
SHEET_W_MM = 215.9
SHEET_H_MM = 279.4
COLS = 1
ROWS = 4
COL_GAP_MM = (SHEET_W_MM - COLS * LABEL_W_MM) / (COLS + 1)
ROW_GAP_MM = (SHEET_H_MM - ROWS * LABEL_H_MM) / (ROWS + 1)

# Text layout — all units in mm (matches the viewBox)
TEXT_X_MM = MARGIN_MM + QR_SIZE_MM + MARGIN_MM * 1.5
NAME_FONT_MM = 10
DESC_FONT_MM = 6
ID_FONT_MM = 5
CHARS_PER_MM = 1.7  # approximate sans-serif chars per mm at font-size=1mm
# Reserve space on the right for the #ID badge so the name doesn't run into it.
ID_BADGE_RESERVE_MM = 16
TEXT_MAX_W_MM = LABEL_W_MM - TEXT_X_MM - MARGIN_MM - ID_BADGE_RESERVE_MM


def _qr_data_for_box(box_id: int, public_url: str) -> str:
    """Build the URL the QR code resolves to.

    With a public URL configured (production), phones scanning the code go
    straight to the box detail page. Without one (local dev) we fall back to
    the `stash:box:N` custom scheme — clearly broken for end users, which is
    the point: it signals that STASH_PUBLIC_URL needs to be set before
    printing labels for real use.
    """
    if public_url:
        return f"{public_url.rstrip('/')}/boxes/{box_id}"
    return f"stash:box:{box_id}"


def _qr_svg_path(data: str) -> tuple[str, str]:
    factory = qrcode.image.svg.SvgPathImage
    img = qrcode.make(data, image_factory=factory, box_size=10, border=0)
    buf = io.BytesIO()
    img.save(buf)
    root = ET.fromstring(buf.getvalue())
    path_el = root.find(".//{http://www.w3.org/2000/svg}path")
    d = path_el.get("d", "") if path_el is not None else ""
    vb = root.get("viewBox", "0 0 100 100")
    return d, vb


def _fit_font_size(text: str, max_width_mm: float, ideal_size_mm: float, min_size_mm: float = 3.5) -> float:
    """Shrink font size if text would overflow, down to min_size_mm."""
    text_width = len(text) / CHARS_PER_MM * ideal_size_mm
    if text_width <= max_width_mm:
        return ideal_size_mm
    scaled = ideal_size_mm * max_width_mm / text_width
    return max(scaled, min_size_mm)


def _label_content(box_id: int, name: str, description: str, public_url: str) -> str:
    qr_path, qr_vb = _qr_svg_path(_qr_data_for_box(box_id, public_url))
    vb_w = float(qr_vb.split()[2])
    vb_h = float(qr_vb.split()[3])

    qr_y = (LABEL_H_MM - QR_SIZE_MM) / 2
    name_size = _fit_font_size(name, TEXT_MAX_W_MM, NAME_FONT_MM)
    name_y = LABEL_H_MM / 2 - (2 if description else 0)

    parts = [
        f'<rect width="{LABEL_W_MM}" height="{LABEL_H_MM}" rx="2" ry="2" '
        f'fill="white" stroke="#bbb" stroke-width="0.3"/>',
        f'<g transform="translate({MARGIN_MM},{qr_y}) '
        f'scale({QR_SIZE_MM / vb_w},{QR_SIZE_MM / vb_h})">',
        f'  <path d="{qr_path}" fill="black"/>',
        f'</g>',
        # Box ID badge in the top-right corner — small, monospace, easy to
        # read across the room without scanning ("grab box 12").
        f'<text x="{LABEL_W_MM - MARGIN_MM}" y="{MARGIN_MM + ID_FONT_MM * 0.8}" '
        f'font-family="ui-monospace, Menlo, monospace" font-size="{ID_FONT_MM}" '
        f'fill="#666" text-anchor="end">#{box_id}</text>',
        f'<text x="{TEXT_X_MM}" y="{name_y}" '
        f'font-family="sans-serif" font-size="{name_size}" font-weight="bold" '
        f'fill="#111" dominant-baseline="central">'
        f'{_escape(name)}</text>',
    ]

    if description:
        desc_size = _fit_font_size(description, TEXT_MAX_W_MM, DESC_FONT_MM)
        desc_y = name_y + name_size * 0.6 + desc_size + 1
        parts.append(
            f'<text x="{TEXT_X_MM}" y="{desc_y}" '
            f'font-family="sans-serif" font-size="{desc_size}" '
            f'fill="#666">{_escape(description)}</text>'
        )

    return "\n    ".join(parts)


def render_label_svg(box_id: int, box_name: str, description: str = "", public_url: str = "") -> str:
    inner = _label_content(box_id, box_name, description, public_url)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     width="{LABEL_W_MM}mm" height="{LABEL_H_MM}mm"
     viewBox="0 0 {LABEL_W_MM} {LABEL_H_MM}">
  {inner}
</svg>"""


def render_sheet_svg(boxes: list[dict], public_url: str = "") -> str:
    slots = ROWS * COLS
    cells = []
    for i in range(slots):
        col = i % COLS
        row = i // COLS
        x = COL_GAP_MM + col * (LABEL_W_MM + COL_GAP_MM)
        y = ROW_GAP_MM + row * (LABEL_H_MM + ROW_GAP_MM)

        if i < len(boxes):
            b = boxes[i]
            inner = _label_content(
                b["id"], b["name"], b.get("notes") or "", public_url,
            )
        else:
            inner = (
                f'<rect width="{LABEL_W_MM}" height="{LABEL_H_MM}" rx="2" ry="2" '
                f'fill="white" stroke="#ddd" stroke-width="0.3" stroke-dasharray="2,2"/>'
            )

        cells.append(f'<g transform="translate({x},{y})">{inner}</g>')

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     width="{SHEET_W_MM}mm" height="{SHEET_H_MM}mm"
     viewBox="0 0 {SHEET_W_MM} {SHEET_H_MM}">
  <rect width="{SHEET_W_MM}" height="{SHEET_H_MM}" fill="white"/>
  {"".join(cells)}
</svg>"""


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
