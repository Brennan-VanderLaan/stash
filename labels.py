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
    # Avery sheets are coated for the printer they're sold for —
    # the laser-only adhesives can melt or jam an inkjet head,
    # and the inkjet papers smudge under laser heat.  Surfacing
    # this in the picker matters: a user who runs out and grabs
    # the wrong sheet at Staples can waste a whole pack before
    # they figure it out (feedback #23 — "5523 doesn't work with
    # ink jet, only with laser printers").
    # Values: "laser", "inkjet", or "both".
    printer_compat: str = "both"

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
    # LASER ONLY — the UltraHold adhesive layer is rated for
    # laser fusing; inkjet heads jam on the coating.
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
        printer_compat="laser",
    ),
    # 1" × 2-5/8", 30 per sheet, 3 cols × 10 rows.  Address
    # labels — useful for itemising small bins.  LASER ONLY.
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
        printer_compat="laser",
    ),
    # 3-1/3" × 4", 6 per sheet, 2 cols × 3 rows.  Bigger
    # shipping label — for big tubs and totes.  LASER ONLY.
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
        printer_compat="laser",
    ),
    # ── INKJET variants ────────────────────────────────────────
    # Same physical layouts as the 5xxx series above but with
    # inkjet-rated paper + adhesive.  Added so home printers
    # (which are overwhelmingly inkjet) aren't forced into a
    # laser-only SKU and a wasted pack of labels — feedback #23.
    #
    # 1" × 2-5/8", 30 per sheet — inkjet equivalent of 5160.
    "8160": AveryFormat(
        sku="8160",
        description='1" × 2⅝" — 30 per sheet (address, inkjet)',
        sheet_w_mm=215.9, sheet_h_mm=279.4,
        label_w_mm=66.7, label_h_mm=25.4,
        cols=3, rows=10,
        margin_top_mm=12.7,
        margin_left_mm=4.76,
        col_gap_mm=3.05,
        row_gap_mm=0,
        printer_compat="inkjet",
    ),
    # 2" × 4", 10 per sheet — inkjet equivalent of 5163's
    # physical layout (and the closest analogue to 5523 for
    # inkjet owners; UltraHold has no inkjet counterpart, this
    # is the regular paper version).
    "8163": AveryFormat(
        sku="8163",
        description='2" × 4" — 10 per sheet (shipping, inkjet)',
        sheet_w_mm=215.9, sheet_h_mm=279.4,
        label_w_mm=101.6, label_h_mm=50.8,
        cols=2, rows=5,
        margin_top_mm=12.7,
        margin_left_mm=4.76,
        col_gap_mm=4.76,
        row_gap_mm=0,
        printer_compat="inkjet",
    ),
    # 3-1/3" × 4", 6 per sheet — inkjet equivalent of 5164.
    "8164": AveryFormat(
        sku="8164",
        description='3⅓" × 4" — 6 per sheet (large shipping, inkjet)',
        sheet_w_mm=215.9, sheet_h_mm=279.4,
        label_w_mm=101.6, label_h_mm=84.7,
        cols=2, rows=3,
        margin_top_mm=12.7,
        margin_left_mm=4.76,
        col_gap_mm=4.76,
        row_gap_mm=0,
        printer_compat="inkjet",
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
# Font fractions bumped up (0.22 → 0.26 name, 0.13 → 0.16 desc, 0.10 →
# 0.12 id) — printed in-person at arm's length the previous sizes
# read as "small print".  textLength squeeze still guarantees fit, so
# bigger ideal sizes don't risk overflow; they just shrink fewer
# characters before kicking in.
_NAME_FONT_FRACTION = 0.26
_DESC_FONT_FRACTION = 0.16
_ID_FONT_FRACTION = 0.12
# Body text colors.  Name stays near-black; description was #666 (a
# soft mid-grey that disappeared on printed labels) bumped to #333 so
# notes stay legible from across the room.  ID badge follows suit.
_NAME_FILL = "#111"
_DESC_FILL = "#333"
_ID_FILL = "#333"
_CHARS_PER_FONT_UNIT = 1.7   # rough sans-serif width / font-size — used by
                             # _fit_font for honest shrink-to-fit estimates.
# Wrap uses a slightly more generous chars-per-em estimate than
# _fit_font.  Sans-serif glyphs average a hair under 0.55 em wide so
# 1.85 keeps the wrap from breaking lines a few characters earlier
# than needed and ellipsising names that would otherwise fit.
# _fit_font keeps the conservative 1.7 because under-shrinking
# overflows the cell, whereas under-wrapping just truncates needlessly.
_CHARS_PER_FONT_UNIT_WRAP = 1.85

# Portrait labels: QR is smaller (so name has room to wrap below),
# text fonts are slightly smaller (the column is narrower than in
# landscape so wrapping needs to actually fit), and the ID badge
# is much bigger because portrait has the headroom and the ID
# doubles as a "find this box from across the room" marker.
_QR_FRACTION_PORTRAIT = 0.72
_NAME_FONT_FRACTION_PORTRAIT = 0.17
_DESC_FONT_FRACTION_PORTRAIT = 0.12
_ID_FONT_FRACTION_PORTRAIT = 0.20


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


def _squeeze_attrs(text: str, max_width: float, font_size: float) -> str:
    """Return SVG attributes that force a single-line text element to
    fit ``max_width`` if our width estimate says it won't.

    ``_fit_font`` shrinks toward fit but bottoms out at 40 % of the
    ideal size to keep text legible; on tiny cells (Avery 5160) a
    long name still spills past the cell edge after the shrink hits
    that floor.  ``textLength`` + ``lengthAdjust="spacingAndGlyphs"``
    is SVG's native squeeze — we only apply it when the estimate
    says we'd overflow, so short names keep their natural spacing.
    """
    if not text or max_width <= 0:
        return ""
    width = (len(text) / _CHARS_PER_FONT_UNIT) * font_size
    if width <= max_width:
        return ""
    return f' textLength="{max_width:.3f}" lengthAdjust="spacingAndGlyphs"'


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


def _wrap_text(text: str, max_width: float, font_size: float,
               max_lines: int = 3) -> list[str]:
    """Greedy word-wrap → list of lines that each fit ``max_width``.

    Portrait labels in particular have a *narrow* text column, so
    a naive single-line render of "Holiday Decorations - Garage
    Bay 2" gets clipped at the cell edge.  Wrapping into 2-3 lines
    is far more legible than a font shrink that ends up unreadable.

    Width estimate is the same approximation ``_fit_font`` uses
    (chars / ``_CHARS_PER_FONT_UNIT`` × font_size).  A word longer
    than ``max_width`` on its own keeps its line — clipping one
    word is still better than dropping it; in practice this only
    fires on URLs or long compound nouns.
    """
    text = (text or "").strip()
    if not text:
        return []
    words = text.split()
    chars_per_mm = _CHARS_PER_FONT_UNIT_WRAP / font_size
    max_chars = max(1, int(max_width * chars_per_mm))
    lines: list[str] = []
    cur = ""
    for word in words:
        candidate = f"{cur} {word}".strip()
        if len(candidate) <= max_chars or not cur:
            cur = candidate
            continue
        lines.append(cur)
        if len(lines) >= max_lines:
            cur = ""
            break
        cur = word
    if cur and len(lines) < max_lines:
        lines.append(cur)
    # If words remain unrendered, append an ellipsis to the last
    # visible line so the user knows it's truncated.
    used_words = sum(len(line.split()) for line in lines)
    if used_words < len(words) and lines:
        tail = lines[-1]
        if not tail.endswith("..."):
            while tail and len(tail) + 3 > max_chars:
                tail = tail[:-1]
            lines[-1] = tail.rstrip() + "..."
    return lines


def _background_art_inner(art_bytes: bytes | None,
                          x: float, y: float,
                          w: float, h: float) -> str:
    """Background-art layer placed within an explicit rect (the
    text region of the label).  Centring on the text region, not
    the whole cell, keeps the art's focal point behind the name +
    description where the user is actually reading.  The rect
    butts flush against the QR's edge so there's no white strip
    between QR and art (which earlier rendered as a "loading
    bug" hard line).  The QR's black squares stay 100% opaque
    on top of the faded art and scan fine — the text + QR are
    still the focal points, the art is wallpaper."""
    if not art_bytes or w <= 0 or h <= 0:
        return ""
    mime = "image/jpeg" if art_bytes[:3] == b"\xff\xd8\xff" else "image/png"
    b64 = base64.b64encode(art_bytes).decode("ascii")
    # Opacity: bumped from 0.3 → 0.5 because at 0.3 the AI-
    # generated art was nearly invisible on most papers; 0.5 keeps
    # text + QR readable while making the art a real visual cue.
    return (
        f'<image href="data:{mime};base64,{b64}" '
        f'x="{x}" y="{y}" width="{w}" height="{h}" '
        f'preserveAspectRatio="xMidYMid slice" opacity="0.5"/>'
    )


_COLOR_HEX_RE = None  # initialised lazily to avoid an import at module load


def _sanitize_color(color: str | None) -> str | None:
    """Whitelist hex-color strings (``#abc`` / ``#abcdef``) so a
    forged box.color from a malformed write can't inject arbitrary
    SVG attributes into the rendered label.  Anything that doesn't
    match the regex returns None and the renderer skips the tint."""
    global _COLOR_HEX_RE
    if _COLOR_HEX_RE is None:
        import re
        _COLOR_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
    if not color:
        return None
    color = color.strip()
    return color if _COLOR_HEX_RE.match(color) else None


def _color_tint_rect(color: str | None,
                     canvas_w: float, canvas_h: float,
                     margin: float) -> str:
    """Pastel wash of the room color sitting between the white
    label background and the (optional) AI art.  Drawn at 18%
    opacity so QR + text contrast stays intact; full saturation
    would make text hard to read on a printed sticker."""
    sanitized = _sanitize_color(color)
    if not sanitized:
        return ""
    return (
        f'<rect x="{margin}" y="{margin}" '
        f'width="{canvas_w - 2 * margin}" '
        f'height="{canvas_h - 2 * margin}" '
        f'rx="1.2" ry="1.2" '
        f'fill="{sanitized}" opacity="0.18"/>'
    )


def _label_inner_landscape(
    box_id: int,
    name: str,
    description: str,
    public_url: str,
    canvas_w: float,
    canvas_h: float,
    background_art: bytes | None = None,
    color_tint: str | None = None,
) -> str:
    """Landscape-canvas layout: QR on the left, name + notes
    fill the right.  Used for the canvas being wider than tall —
    Avery 5523's natural cell shape (101.6 × 50.8)."""
    # ``layout_dim`` is the short side, capped at half the long side
    # so near-square cells (Avery 5164 is 101.6 × 84.7 — only 1.2 : 1)
    # don't end up with a QR that eats the entire long axis.  Sizing
    # the QR + fonts off it leaves enough room for text alongside the
    # QR on every Avery template we support.
    short_dim = canvas_h
    layout_dim = min(short_dim, canvas_w * 0.5)
    margin = layout_dim * _MARGIN_FRACTION
    qr_size = layout_dim * _QR_FRACTION
    qr_y = (canvas_h - qr_size) / 2
    name_size = layout_dim * _NAME_FONT_FRACTION
    desc_size = layout_dim * _DESC_FONT_FRACTION
    id_size = layout_dim * _ID_FONT_FRACTION

    text_x = margin + qr_size + margin
    # Reserve space for the box-ID badge proportional to the actual
    # ID width — the old fixed 5 × id_size allowance stole ~25 mm of
    # the 5523 cell for a 5-char "#1234" badge that only needs ~15
    # mm, and made small cells (5160) overflow before they even tried
    # to render the name.
    id_text = f"#{box_id}"
    id_width = (len(id_text) / _CHARS_PER_FONT_UNIT) * id_size
    id_reserve = id_width + margin
    text_max = canvas_w - text_x - margin - id_reserve

    name_size = _fit_font(name, text_max, name_size)
    name_y = canvas_h / 2 - (1 if description else 0)

    qr_path, qr_vb = _qr_svg_path(_qr_data_for_box(box_id, public_url))
    vb_w = float(qr_vb.split()[2])
    vb_h = float(qr_vb.split()[3])

    # Label background — white fill, no visible stroke.  The previous
    # 0.25 mm grey stroke drew the cell perimeter, but any printer
    # registration drift cropped it unevenly and looked like a
    # printing defect.  The Avery cell itself is the physical edge;
    # we don't need to draw a second one.
    parts = [
        f'<rect width="{canvas_w}" height="{canvas_h}" '
        f'rx="1.5" ry="1.5" fill="white"/>',
    ]
    # Pastel room-color wash sits directly above the white base
    # rect so the AI art (drawn next) layers on top and gets
    # tinted naturally without us having to blend manually.
    tint = _color_tint_rect(color_tint, canvas_w, canvas_h, margin)
    if tint:
        parts.append(tint)
    # Background art rect: butts flush against the QR's right edge
    # (no white strip = no hard cut line) and extends to the right
    # margin.  Vertical span is the cell minus margins.  Centred on
    # the text region so the art's focal point is behind the name.
    art_x = margin + qr_size
    art = _background_art_inner(
        background_art, art_x, margin,
        canvas_w - margin - art_x, canvas_h - 2 * margin,
    )
    if art:
        parts.append(art)
    parts.extend([
        f'<g transform="translate({margin},{qr_y}) '
        f'scale({qr_size / vb_w},{qr_size / vb_h})">',
        f'  <path d="{qr_path}" fill="black"/>',
        f'</g>',
        f'<text x="{canvas_w - margin}" y="{margin + id_size * 0.9}" '
        f'font-family="ui-monospace, Menlo, monospace" '
        f'font-size="{id_size}" fill="{_ID_FILL}" text-anchor="end">'
        f'{_escape(id_text)}</text>',
        f'<text x="{text_x}" y="{name_y}" '
        f'font-family="sans-serif" font-size="{name_size}" '
        f'font-weight="bold" fill="{_NAME_FILL}"'
        f'{_squeeze_attrs(name, text_max, name_size)} '
        f'dominant-baseline="central">{_escape(name)}</text>',
    ])
    if description:
        desc_size = _fit_font(description, text_max, desc_size)
        desc_y = name_y + name_size * 0.6 + desc_size + 0.5
        parts.append(
            f'<text x="{text_x}" y="{desc_y}" '
            f'font-family="sans-serif" font-size="{desc_size}" '
            f'fill="{_DESC_FILL}"'
            f'{_squeeze_attrs(description, text_max, desc_size)}>'
            f'{_escape(description)}</text>'
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
    color_tint: str | None = None,
) -> str:
    """Portrait-canvas layout: QR on TOP, name + notes stack
    below.  Used inside a cell after a 90° rotation so the long
    axis of the printed cell ends up reading vertically.  Canvas
    here is "tall" — narrower than tall.

    The narrow axis is the short side of the printed cell.  We
    size QR off the short side; text margins are ``margin`` from
    each edge so words wrap *inside* the cell instead of running
    off the printed edge.  Box-ID badge is bumped to a 1.6×
    multiplier of the landscape size since portrait has the
    headroom and the ID is the "find this box from across the
    room" handle.
    """
    # Same near-square-cell cap as landscape, applied along the
    # portrait axis: cap by half the *long* (canvas_h) dim so the QR
    # doesn't claim the whole top of the cell on 5164 portrait.
    short_dim = canvas_w
    layout_dim = min(short_dim, canvas_h * 0.5)
    margin = layout_dim * _MARGIN_FRACTION
    qr_size = layout_dim * _QR_FRACTION_PORTRAIT
    qr_x = (canvas_w - qr_size) / 2
    qr_y = margin
    # Portrait's text column is much narrower than landscape's
    # (50.8 mm wide vs ~58 mm) so we start name slightly smaller
    # and rely on word-wrap to fill the vertical space rather than
    # font-fit shrinking the name into illegibility.
    name_size = layout_dim * _NAME_FONT_FRACTION_PORTRAIT
    desc_size = layout_dim * _DESC_FONT_FRACTION_PORTRAIT
    id_size = layout_dim * _ID_FONT_FRACTION_PORTRAIT

    text_x = margin
    text_max = canvas_w - 2 * margin
    # ID badge claims its own row at the bottom-right of the
    # portrait canvas; reserve a strip above it so wrapped text
    # can't collide with it.
    id_band_y = canvas_h - margin
    text_floor = id_band_y - id_size * 1.4

    qr_path, qr_vb = _qr_svg_path(_qr_data_for_box(box_id, public_url))
    vb_w = float(qr_vb.split()[2])
    vb_h = float(qr_vb.split()[3])

    # Label background — white fill, no visible stroke (see landscape
    # for the rationale: any printer drift makes the stroke look
    # cropped, and the Avery cell already has a physical edge).
    parts = [
        f'<rect width="{canvas_w}" height="{canvas_h}" '
        f'rx="1.5" ry="1.5" fill="white"/>',
    ]
    tint = _color_tint_rect(color_tint, canvas_w, canvas_h, margin)
    if tint:
        parts.append(tint)
    # Background art rect spans the text region: from just under
    # the QR down to the bottom margin (above the ID strip), full
    # text-column width.  Centred on the text region so the focal
    # point sits behind the name.  Butts flush against the QR's
    # bottom so there's no white strip / hard cut line.
    art_y = qr_y + qr_size
    art = _background_art_inner(
        background_art, margin, art_y,
        canvas_w - 2 * margin, id_band_y - art_y,
    )
    if art:
        parts.append(art)
    id_text = f"#{box_id}"
    parts.extend([
        f'<g transform="translate({qr_x},{qr_y}) '
        f'scale({qr_size / vb_w},{qr_size / vb_h})">',
        f'  <path d="{qr_path}" fill="black"/>',
        f'</g>',
        # Larger ID badge: portrait has the room and this is the
        # "find from across the room" handle.
        f'<text x="{canvas_w - margin}" y="{id_band_y}" '
        f'font-family="ui-monospace, Menlo, monospace" '
        f'font-size="{id_size}" fill="{_ID_FILL}" '
        f'font-weight="bold" text-anchor="end">'
        f'{_escape(id_text)}</text>',
    ])

    # Name wrap.  Up to 4 lines (was 3) — the extra line meaningfully
    # cuts down on the ``...`` truncation that hit names like
    # "Holiday Decorations Garage Bay 2" before the last word fit.
    # Any line that still over-shoots (rare, multi-syllable single
    # words) gets squeezed via textLength so the ink never leaves
    # the cell.
    name_lines = _wrap_text(name, text_max, name_size, max_lines=4)
    name_y = qr_y + qr_size + margin + name_size
    line_height = name_size * 1.1
    for i, line in enumerate(name_lines):
        y = name_y + i * line_height
        if y > text_floor:
            break
        parts.append(
            f'<text x="{text_x}" y="{y}" '
            f'font-family="sans-serif" font-size="{name_size}" '
            f'font-weight="bold" fill="{_NAME_FILL}"'
            f'{_squeeze_attrs(line, text_max, name_size)}>'
            f'{_escape(line)}</text>'
        )
    last_name_y = name_y + max(0, len(name_lines) - 1) * line_height

    if description:
        # Description wraps too; sits right below the last name
        # line in a lighter weight, smaller size.
        desc_lines = _wrap_text(description, text_max, desc_size, max_lines=2)
        desc_y0 = last_name_y + desc_size + 1.0
        desc_lh = desc_size * 1.1
        for i, line in enumerate(desc_lines):
            y = desc_y0 + i * desc_lh
            if y > text_floor:
                break
            parts.append(
                f'<text x="{text_x}" y="{y}" '
                f'font-family="sans-serif" font-size="{desc_size}" '
                f'fill="{_DESC_FILL}"'
                f'{_squeeze_attrs(line, text_max, desc_size)}>'
                f'{_escape(line)}</text>'
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
    # ``color_tint`` is the resolved hex string the caller plumbed
    # through ("box.color, fallback room.color" — see
    # ``_attach_color_tint`` in app.py).  Already sanitised by
    # ``_color_tint_rect`` at render time, so a None here just
    # skips the wash.
    color_tint = box.get("color_tint")
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
            canvas_w, canvas_h, art_bytes, color_tint,
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
        fmt.label_w_mm, fmt.label_h_mm, art_bytes, color_tint,
    )


def _empty_cell(fmt: AveryFormat) -> str:
    """Unused cells on the last sheet render as nothing — the
    physical Avery label is already there and printing a dashed
    placeholder over it leaves visible marks on the leftover
    stickers when the user prints a partial sheet."""
    return ""


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
    color_tint: str | None = None,
) -> str:
    """Single-cell SVG, sized to the chosen format's cell.  Used
    for the per-box label preview thumbnails on /labels and the
    /boxes/{id}/label.svg download.

    For portrait orientation the SVG is rendered *upright* — i.e.
    the viewBox is the portrait canvas (50.8 × 101.6 for 5523)
    and no rotation transform is applied — so the preview is
    readable without tilting the user's head.  The sheet/PDF
    paths still rotate the content into the physical landscape
    cell because the printed sheet is landscape-celled regardless
    of orientation choice.
    """
    fmt = fmt or AVERY_FORMATS[DEFAULT_FORMAT_SKU]
    o = (orientation or "landscape").lower()
    if o == "portrait":
        canvas_w = fmt.label_h_mm
        canvas_h = fmt.label_w_mm
        inner = _label_inner_portrait(
            box_id, box_name, description or "", public_url,
            canvas_w, canvas_h, background_art, color_tint,
        )
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     width="{canvas_w}mm" height="{canvas_h}mm"
     viewBox="0 0 {canvas_w} {canvas_h}">
  {inner}
</svg>"""
    inner = _label_inner_landscape(
        box_id, box_name, description or "", public_url,
        fmt.label_w_mm, fmt.label_h_mm, background_art, color_tint,
    )
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
