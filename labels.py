"""Generate label SVGs for boxes — sized for printable Avery
shipping-label sheets, not Cricut SVG cutting.

The Avery pivot:

We previously tried to feed Cricut Design Space the raw label SVG
+ a multi-page PDF.  Cricut's importer is hostile to embedded
text + base64 imagery, and the round-trip through "Cut Image"
loses sharpness.  Pivoting to Avery sheets means an honest workflow:
download the PDF, drop the Avery sheet in the printer, hit print.

Two axes of control:

* **Format**: which Avery template the sheet targets.  Different
  cell sizes + columns/rows.  Default is 5523 (2"×4", 10/sheet,
  shipping-label-grade UltraHold) since that's the user's actual
  SKU.  5160 (1"×2.625", 30/sheet) and 5164 (3.33"×4", 6/sheet)
  cover address-label and bigger-shipping cases.
* **Orientation**: per-box, ``landscape`` or ``portrait``.  The
  cell footprint stays fixed (an Avery 5523 cell is always 2"×4");
  what changes is the reading direction of the content within
  the cell.  A ``portrait`` 2"×4" label has the QR + text rotated
  90° so the long axis runs vertically — for slapping on the
  narrow side of a tall box.

All label content (QR code, name, notes, ID badge, optional
background art) renders to fit the smaller of the two cell
dimensions so a single ``_label_content`` shape can be rotated
without overflowing.
"""

from __future__ import annotations

import base64
import dataclasses
import io
from xml.etree import ElementTree as ET

import qrcode
import qrcode.image.svg


# ── Avery format registry ──────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class AveryFormat:
    """One Avery template = sheet dimensions + cell grid + cell
    size.  All distances in millimetres so the SVG ``viewBox``
    units are consistent across formats."""
    sku: str            # e.g. ``"5523"``
    description: str    # human-readable: ``"2\" × 4\", 10 per sheet"``
    sheet_w_mm: float
    sheet_h_mm: float
    label_w_mm: float
    label_h_mm: float
    cols: int
    rows: int
    margin_top_mm: float
    margin_left_mm: float
    col_gap_mm: float
    row_gap_mm: float

    @property
    def labels_per_page(self) -> int:
        return self.cols * self.rows

    def cell_xy(self, index: int) -> tuple[float, float]:
        """Top-left corner of the ``index``-th cell on the sheet."""
        col = index % self.cols
        row = index // self.cols
        x = self.margin_left_mm + col * (self.label_w_mm + self.col_gap_mm)
        y = self.margin_top_mm + row * (self.label_h_mm + self.row_gap_mm)
        return x, y


# US Letter is 215.9 × 279.4 mm.  Each format below is a literal
# transcription of the Avery template spec — verified against
# Avery's printable PDF templates so a printed sheet lines up
# without manual nudging.
AVERY_FORMATS: dict[str, AveryFormat] = {
    # 2" × 4", 10 per sheet, 2 cols × 5 rows.  WaterProof
    # UltraHold — the SKU 5523 the user is targeting today.
    # Same physical layout as 5163 / 5263 / 8163 / 18163.
    "5523": AveryFormat(
        sku="5523",
        description='2" × 4" — 10 per sheet (shipping)',
        sheet_w_mm=215.9, sheet_h_mm=279.4,
        label_w_mm=101.6, label_h_mm=50.8,
        cols=2, rows=5,
        margin_top_mm=12.7,                 # 0.5"
        margin_left_mm=4.76,                # ~0.1875"
        col_gap_mm=4.76,                    # ~0.1875"
        row_gap_mm=0,                       # rows touch
    ),
    # 1" × 2-5/8", 30 per sheet, 3 cols × 10 rows.  Address
    # labels — useful for itemising small bins.
    "5160": AveryFormat(
        sku="5160",
        description='1" × 2⅝" — 30 per sheet (address)',
        sheet_w_mm=215.9, sheet_h_mm=279.4,
        label_w_mm=66.7, label_h_mm=25.4,
        cols=3, rows=10,
        margin_top_mm=12.7,                 # 0.5"
        margin_left_mm=4.76,                # ~0.1875"
        col_gap_mm=3.05,                    # ~0.12"
        row_gap_mm=0,
    ),
    # 3-1/3" × 4", 6 per sheet, 2 cols × 3 rows.  Bigger
    # shipping label — for big tubs and totes.
    "5164": AveryFormat(
        sku="5164",
        description='3⅓" × 4" — 6 per sheet (large shipping)',
        sheet_w_mm=215.9, sheet_h_mm=279.4,
        label_w_mm=101.6, label_h_mm=84.7,
        cols=2, rows=3,
        margin_top_mm=12.7,
        margin_left_mm=4.76,
        col_gap_mm=4.76,
        row_gap_mm=0,
    ),
}

DEFAULT_FORMAT_SKU = "5523"


def get_format(sku: str | None) -> AveryFormat:
    """Resolve a format SKU to the registry entry, falling back
    to the default if unknown.  Templates use this to translate
    a query-string ``?format=…`` into a real layout without
    risking a KeyError on a mistyped SKU."""
    if not sku:
        return AVERY_FORMATS[DEFAULT_FORMAT_SKU]
    return AVERY_FORMATS.get(sku, AVERY_FORMATS[DEFAULT_FORMAT_SKU])


# ── Per-label content ──────────────────────────────────────────────


# Internal layout reserves: relative to the *short* side of the
# cell so we can rotate the same shape into a portrait orientation
# without re-laying-out.  All sizes are fractions of ``min_dim``
# (the shorter of cell width / height) so smaller cells get
# proportionally smaller text.
_QR_FRACTION = 0.85          # QR fills ~85% of the short side
_MARGIN_FRACTION = 0.08
_NAME_FONT_FRACTION = 0.22
_DESC_FONT_FRACTION = 0.13
_ID_FONT_FRACTION = 0.10
_CHARS_PER_FONT_UNIT = 1.7   # rough sans-serif width / font-size


def _qr_data_for_box(box_id: int, public_url: str) -> str:
    """URL the QR points at.  With STASH_PUBLIC_URL set, scanning
    the printed code goes straight to the box detail page on
    your phone.  Without it, the ``stash:box:N`` custom scheme
    is a clear "you forgot to set PUBLIC_URL" signal."""
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


def _fit_font(text: str, max_width: float, ideal: float,
              minimum: float | None = None) -> float:
    """Shrink font size to fit width.  Returns the chosen size in
    the same units as ``ideal`` + ``max_width``."""
    if not text:
        return ideal
    if minimum is None:
        minimum = ideal * 0.4
    width = len(text) / _CHARS_PER_FONT_UNIT * ideal
    if width <= max_width:
        return ideal
    return max(ideal * max_width / width, minimum)


def _background_art_inner(art_bytes: bytes | None,
                          canvas_w: float, canvas_h: float,
                          margin: float) -> str:
    """Background-art layer covering the full cell minus the
    rounded-corner margin.  Earlier versions clipped the art to
    the right of the QR which left a hard vertical cut line
    (looked like a partly-loaded image).  The QR's black
    squares stay 100% opaque on top of the faded art and
    scan fine — the text + QR are still the focal points,
    the art is wallpaper."""
    if not art_bytes:
        return ""
    mime = "image/jpeg" if art_bytes[:3] == b"\xff\xd8\xff" else "image/png"
    b64 = base64.b64encode(art_bytes).decode("ascii")
    return (
        f'<image href="data:{mime};base64,{b64}" '
        f'x="{margin}" y="{margin}" '
        f'width="{canvas_w - 2 * margin}" '
        f'height="{canvas_h - 2 * margin}" '
        f'preserveAspectRatio="xMidYMid slice" opacity="0.3"/>'
    )


def _label_inner_landscape(
    box_id: int,
    name: str,
    description: str,
    public_url: str,
    canvas_w: float,
    canvas_h: float,
    background_art: bytes | None = None,
) -> str:
    """Landscape-canvas layout: QR on the left, name + notes
    fill the right.  Used for the canvas being wider than tall —
    Avery 5523's natural cell shape (101.6 × 50.8)."""
    short_dim = canvas_h
    margin = short_dim * _MARGIN_FRACTION
    qr_size = short_dim * _QR_FRACTION
    qr_y = (canvas_h - qr_size) / 2
    name_size = short_dim * _NAME_FONT_FRACTION
    desc_size = short_dim * _DESC_FONT_FRACTION
    id_size = short_dim * _ID_FONT_FRACTION

    text_x = margin + qr_size + margin
    id_reserve = id_size * 5
    text_max = canvas_w - text_x - margin - id_reserve

    name_size = _fit_font(name, text_max, name_size)
    name_y = canvas_h / 2 - (1 if description else 0)

    qr_path, qr_vb = _qr_svg_path(_qr_data_for_box(box_id, public_url))
    vb_w = float(qr_vb.split()[2])
    vb_h = float(qr_vb.split()[3])

    parts = [
        f'<rect width="{canvas_w}" height="{canvas_h}" '
        f'rx="1.5" ry="1.5" fill="white" stroke="#bbb" stroke-width="0.25"/>',
    ]
    art = _background_art_inner(background_art, canvas_w, canvas_h, margin)
    if art:
        parts.append(art)
    parts.extend([
        f'<g transform="translate({margin},{qr_y}) '
        f'scale({qr_size / vb_w},{qr_size / vb_h})">',
        f'  <path d="{qr_path}" fill="black"/>',
        f'</g>',
        f'<text x="{canvas_w - margin}" y="{margin + id_size * 0.9}" '
        f'font-family="ui-monospace, Menlo, monospace" '
        f'font-size="{id_size}" fill="#666" text-anchor="end">'
        f'#{box_id}</text>',
        f'<text x="{text_x}" y="{name_y}" '
        f'font-family="sans-serif" font-size="{name_size}" '
        f'font-weight="bold" fill="#111" '
        f'dominant-baseline="central">{_escape(name)}</text>',
    ])
    if description:
        desc_size = _fit_font(description, text_max, desc_size)
        desc_y = name_y + name_size * 0.6 + desc_size + 0.5
        parts.append(
            f'<text x="{text_x}" y="{desc_y}" '
            f'font-family="sans-serif" font-size="{desc_size}" '
            f'fill="#666">{_escape(description)}</text>'
        )
    return "\n    ".join(parts)


def _label_inner_portrait(
    box_id: int,
    name: str,
    description: str,
    public_url: str,
    canvas_w: float,
    canvas_h: float,
    background_art: bytes | None = None,
) -> str:
    """Portrait-canvas layout: QR on TOP, name + notes stack
    below.  Used inside a cell after a 90° rotation so the long
    axis of the printed cell ends up reading vertically.  Canvas
    here is "tall" — narrower than tall.

    Sized off ``canvas_w`` (the short side of the rotated canvas,
    which becomes the short side of the printed cell after
    rotation) so QR + text proportions match what landscape
    produces in the same cell."""
    short_dim = canvas_w
    margin = short_dim * _MARGIN_FRACTION
    qr_size = short_dim * _QR_FRACTION
    qr_x = (canvas_w - qr_size) / 2
    qr_y = margin
    name_size = short_dim * _NAME_FONT_FRACTION
    desc_size = short_dim * _DESC_FONT_FRACTION
    id_size = short_dim * _ID_FONT_FRACTION

    text_x = margin
    text_max = canvas_w - 2 * margin
    # First text line sits a full ``margin`` below the QR.
    name_y = qr_y + qr_size + margin + name_size

    name_size = _fit_font(name, text_max, name_size)

    qr_path, qr_vb = _qr_svg_path(_qr_data_for_box(box_id, public_url))
    vb_w = float(qr_vb.split()[2])
    vb_h = float(qr_vb.split()[3])

    parts = [
        f'<rect width="{canvas_w}" height="{canvas_h}" '
        f'rx="1.5" ry="1.5" fill="white" stroke="#bbb" stroke-width="0.25"/>',
    ]
    art = _background_art_inner(background_art, canvas_w, canvas_h, margin)
    if art:
        parts.append(art)
    parts.extend([
        f'<g transform="translate({qr_x},{qr_y}) '
        f'scale({qr_size / vb_w},{qr_size / vb_h})">',
        f'  <path d="{qr_path}" fill="black"/>',
        f'</g>',
        # ID badge sits at the bottom-right of the portrait
        # canvas so it's away from the QR but still on a fixed
        # corner you can find at a glance.
        f'<text x="{canvas_w - margin}" '
        f'y="{canvas_h - margin}" '
        f'font-family="ui-monospace, Menlo, monospace" '
        f'font-size="{id_size}" fill="#666" text-anchor="end">'
        f'#{box_id}</text>',
        f'<text x="{text_x}" y="{name_y}" '
        f'font-family="sans-serif" font-size="{name_size}" '
        f'font-weight="bold" fill="#111">{_escape(name)}</text>',
    ])
    if description:
        desc_size = _fit_font(description, text_max, desc_size)
        desc_y = name_y + desc_size + 1.5
        parts.append(
            f'<text x="{text_x}" y="{desc_y}" '
            f'font-family="sans-serif" font-size="{desc_size}" '
            f'fill="#666">{_escape(description)}</text>'
        )
    return "\n    ".join(parts)


def _label_group(
    fmt: AveryFormat,
    box: dict,
    public_url: str,
) -> str:
    """One cell's worth of SVG, sized to the format's cell
    dimensions.  Picks the right layout for the box's
    orientation:

    * ``landscape`` — QR + text laid out across the cell's
      natural (101.6 × 50.8 for 5523) shape.
    * ``portrait`` — QR + text laid out into a tall narrow
      canvas (50.8 × 101.6 for 5523), then rotated 90°
      clockwise + translated so it fits the actual landscape
      cell on the sheet.  After rotation the rendered shape's
      original "top" lands on the cell's right edge — peel the
      label, rotate it 90° CCW in your hand, and the text
      reads horizontally on a vertical surface.
    """
    orientation = (box.get("label_orientation") or "landscape").lower()
    name = box.get("name", "")
    description = box.get("notes") or ""
    art_bytes = box.get("art_bytes")
    box_id = box["id"]

    if orientation == "portrait":
        # Portrait canvas is "tall" — width = cell height,
        # height = cell width.  After we render the inner
        # shape, a 90°-CW rotation followed by a translation
        # by the cell width along X makes the (tall) shape
        # fit the (wide) cell.
        canvas_w = fmt.label_h_mm    # 50.8 for 5523
        canvas_h = fmt.label_w_mm    # 101.6 for 5523
        inner = _label_inner_portrait(
            box_id, name, description, public_url,
            canvas_w, canvas_h, art_bytes,
        )
        # SVG transform reads right-to-left for application
        # order: rotate first, then translate.  rotate(90)
        # takes (x, y) → (-y, x); the (canvas_w, 0) translate
        # shifts the rotated shape so its bounding box lands
        # at (0,0)..(canvas_h, canvas_w) — which equals
        # (label_w_mm, label_h_mm) — perfectly matching the
        # cell.
        return (
            f'<g transform="translate({fmt.label_w_mm},0) rotate(90)">'
            f'{inner}</g>'
        )

    return _label_inner_landscape(
        box_id, name, description, public_url,
        fmt.label_w_mm, fmt.label_h_mm, art_bytes,
    )


def _empty_cell(fmt: AveryFormat) -> str:
    """Dashed placeholder for unused cells on the last sheet —
    purely visual so the print preview shows the grid alignment."""
    return (
        f'<rect width="{fmt.label_w_mm}" height="{fmt.label_h_mm}" '
        f'rx="1.5" ry="1.5" fill="white" stroke="#ddd" '
        f'stroke-width="0.25" stroke-dasharray="2,2"/>'
    )


# ── Sheet rendering ────────────────────────────────────────────────


def page_count(num_boxes: int, fmt: AveryFormat) -> int:
    if num_boxes <= 0:
        return 1
    return (num_boxes + fmt.labels_per_page - 1) // fmt.labels_per_page


def render_label_svg(
    box_id: int,
    box_name: str,
    description: str = "",
    public_url: str = "",
    background_art: bytes | None = None,
    *,
    fmt: AveryFormat | None = None,
    orientation: str = "landscape",
) -> str:
    """Single-cell SVG, sized to the chosen format's cell.  Used
    for the per-box label preview thumbnails on /labels and the
    /boxes/{id}/label.svg download."""
    fmt = fmt or AVERY_FORMATS[DEFAULT_FORMAT_SKU]
    box = {
        "id": box_id,
        "name": box_name,
        "notes": description,
        "art_bytes": background_art,
        "label_orientation": orientation,
    }
    inner = _label_group(fmt, box, public_url)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     width="{fmt.label_w_mm}mm" height="{fmt.label_h_mm}mm"
     viewBox="0 0 {fmt.label_w_mm} {fmt.label_h_mm}">
  {inner}
</svg>"""


def render_single_sheet_svg(
    boxes: list[dict],
    public_url: str = "",
    *,
    fmt: AveryFormat | None = None,
) -> str:
    """One physical Avery sheet — dimensions + cell positions
    pulled from ``fmt``."""
    fmt = fmt or AVERY_FORMATS[DEFAULT_FORMAT_SKU]
    cells = [
        f'<rect width="{fmt.sheet_w_mm}" height="{fmt.sheet_h_mm}" '
        f'fill="white"/>',
    ]
    for i in range(fmt.labels_per_page):
        x, y = fmt.cell_xy(i)
        if i < len(boxes):
            inner = _label_group(fmt, boxes[i], public_url)
        else:
            inner = _empty_cell(fmt)
        cells.append(f'<g transform="translate({x},{y})">{inner}</g>')
    return f"""<svg xmlns="http://www.w3.org/2000/svg"
     width="{fmt.sheet_w_mm}mm" height="{fmt.sheet_h_mm}mm"
     viewBox="0 0 {fmt.sheet_w_mm} {fmt.sheet_h_mm}">
  {"".join(cells)}
</svg>"""


def render_sheet_pdf(
    boxes: list[dict],
    public_url: str = "",
    *,
    fmt: AveryFormat | None = None,
) -> bytes:
    """Multi-page PDF — one Avery sheet per page.  cairosvg
    rasterises each sheet to a vector PDF page; pypdf merges
    the pages into one downloadable artifact.

    This is the *primary* output path for printing — drop the
    Avery sheet in the printer, open the PDF, hit print.  No
    Cricut, no SVG-import dance."""
    import io as _io
    import cairosvg
    from pypdf import PdfWriter, PdfReader

    fmt = fmt or AVERY_FORMATS[DEFAULT_FORMAT_SKU]
    if not boxes:
        boxes = []

    pages = page_count(len(boxes), fmt)
    writer = PdfWriter()
    for page_idx in range(pages):
        chunk = boxes[
            page_idx * fmt.labels_per_page:
            (page_idx + 1) * fmt.labels_per_page
        ]
        sheet_svg = render_single_sheet_svg(chunk, public_url, fmt=fmt)
        if not sheet_svg.lstrip().startswith("<?xml"):
            sheet_svg = '<?xml version="1.0" encoding="UTF-8"?>\n' + sheet_svg
        pdf_bytes = cairosvg.svg2pdf(bytestring=sheet_svg.encode("utf-8"))
        reader = PdfReader(_io.BytesIO(pdf_bytes))
        for page in reader.pages:
            writer.add_page(page)
    out = _io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )


# ── Backwards-compat shims ─────────────────────────────────────────


# Pre-Avery code referenced these constants directly.  Keep them
# pointing at the new default format so any straggler import path
# still works.
LABEL_W_MM = AVERY_FORMATS[DEFAULT_FORMAT_SKU].label_w_mm
LABEL_H_MM = AVERY_FORMATS[DEFAULT_FORMAT_SKU].label_h_mm
SHEET_W_MM = AVERY_FORMATS[DEFAULT_FORMAT_SKU].sheet_w_mm
SHEET_H_MM = AVERY_FORMATS[DEFAULT_FORMAT_SKU].sheet_h_mm
LABELS_PER_PAGE = AVERY_FORMATS[DEFAULT_FORMAT_SKU].labels_per_page


def render_sheet_svg(
    boxes: list[dict],
    public_url: str = "",
    *,
    fmt: AveryFormat | None = None,
) -> str:
    """All boxes, stacked across as many sheets as needed, in a
    single SVG — for an "everything in one file" download.  The
    HTML print page uses :func:`render_single_sheet_svg` per page
    instead so each sheet sits in its own page-break div."""
    fmt = fmt or AVERY_FORMATS[DEFAULT_FORMAT_SKU]
    pages = page_count(len(boxes), fmt)
    total_h = pages * fmt.sheet_h_mm
    cells = [f'<rect width="{fmt.sheet_w_mm}" height="{total_h}" '
             f'fill="white"/>']
    for page_idx in range(pages):
        y_offset = page_idx * fmt.sheet_h_mm
        chunk = boxes[
            page_idx * fmt.labels_per_page:
            (page_idx + 1) * fmt.labels_per_page
        ]
        for i in range(fmt.labels_per_page):
            x, y = fmt.cell_xy(i)
            y += y_offset
            if i < len(chunk):
                inner = _label_group(fmt, chunk[i], public_url)
            else:
                inner = _empty_cell(fmt)
            cells.append(f'<g transform="translate({x},{y})">{inner}</g>')
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     width="{fmt.sheet_w_mm}mm" height="{total_h}mm"
     viewBox="0 0 {fmt.sheet_w_mm} {total_h}">
  {"".join(cells)}
</svg>"""
