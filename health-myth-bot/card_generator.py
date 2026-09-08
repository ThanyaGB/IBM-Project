"""
card_generator.py — Generate shareable myth-fact verified card images (stretch goal 4.6).

Uses Pillow to render a branded PNG card showing:
  - The common myth (struck through / muted)
  - The verified fact (highlighted)
  - The source citation
  - A "Health Myth-Bot | Verified" footer badge

Cards are saved to ./static/cards/ and the file path is returned.
The caller (app.py) is responsible for serving the file at a publicly
accessible URL and passing that URL to Twilio as media_url.

If Pillow is not installed or any rendering step fails, the function
raises an exception and app.py catches it gracefully (non-fatal).
"""

import os
import textwrap
from pathlib import Path

# Output directory for generated cards
CARDS_DIR = Path(os.environ.get("CARDS_DIR", "./static/cards"))

# ── Design constants ─────────────────────────────────────────────────────────
CARD_WIDTH = 800
CARD_HEIGHT = 500
PADDING = 40

# Palette (mirrors dashboard.py and index.html)
COLOR_BG = (245, 247, 249)           # off-white
COLOR_DEEP_BLUE = (26, 58, 92)       # deep-blue header
COLOR_TEAL = (26, 127, 122)          # teal accent
COLOR_MYTH_BG = (253, 243, 243)      # light red tint
COLOR_MYTH_TEXT = (122, 48, 48)      # dark red
COLOR_MYTH_BORDER = (192, 57, 43)    # emergency red
COLOR_FACT_BG = (240, 250, 244)      # light green tint
COLOR_FACT_TEXT = (26, 74, 46)       # dark green
COLOR_FACT_BORDER = (39, 174, 96)    # green
COLOR_MUTED = (87, 96, 106)          # muted text
COLOR_WHITE = (255, 255, 255)
COLOR_BADGE_BG = (26, 127, 122)      # teal
STRIPE_HEIGHT = 8


def _get_font(size: int, bold: bool = False):
    """
    Load a TrueType font.  Falls back to Pillow's built-in bitmap font
    if no TTF is available on the system (common in minimal environments).
    The fallback is small and fixed-size, so size/bold are ignored — but
    the card will still render rather than crash.
    """
    from PIL import ImageFont  # noqa: PLC0415

    # Try common system font paths
    candidates_bold = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    candidates_regular = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    candidates = candidates_bold if bold else candidates_regular
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    # Final fallback
    return ImageFont.load_default()


def _draw_rounded_rect(draw, xy, radius, fill, outline=None, outline_width=2):
    """Draw a rounded rectangle using Pillow's Draw API."""
    from PIL import ImageDraw  # noqa: PLC0415 (already imported via caller)

    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=radius, fill=fill,
                           outline=outline, width=outline_width)


def _wrap_text(text: str, width: int) -> list[str]:
    """Wrap text at approximately `width` characters."""
    return textwrap.wrap(text, width=width)


def generate_myth_card(record: dict) -> str:
    """
    Render a branded myth/fact PNG card for the given health_facts record.

    Parameters
    ----------
    record : dict
        A single record from health_facts.json with keys:
        topic, common_myth, verified_fact, source, category.

    Returns
    -------
    str
        Absolute path to the saved PNG file.

    Raises
    ------
    ImportError  — if Pillow is not installed.
    Any PIL exception propagated to the caller.
    """
    from PIL import Image, ImageDraw  # noqa: PLC0415

    CARDS_DIR.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), COLOR_BG)
    draw = ImageDraw.Draw(img)

    # ── Header stripe ────────────────────────────────────────────────────────
    draw.rectangle([0, 0, CARD_WIDTH, STRIPE_HEIGHT], fill=COLOR_TEAL)

    # ── Header area ──────────────────────────────────────────────────────────
    header_h = 70
    draw.rectangle([0, STRIPE_HEIGHT, CARD_WIDTH, STRIPE_HEIGHT + header_h],
                   fill=COLOR_DEEP_BLUE)

    font_header = _get_font(13, bold=True)
    font_small = _get_font(11)
    header_text = f"🏥  HEALTH MYTH-BOT  ·  {record.get('category', '').upper()}"
    draw.text((PADDING, STRIPE_HEIGHT + 14), header_text,
              font=font_header, fill=COLOR_WHITE)

    topic = record.get("topic", "")
    font_topic = _get_font(18, bold=True)
    draw.text((PADDING, STRIPE_HEIGHT + 36), topic,
              font=font_topic, fill=(122, 232, 227))

    # ── Body: two columns ────────────────────────────────────────────────────
    body_top = STRIPE_HEIGHT + header_h + 20
    col_w = (CARD_WIDTH - PADDING * 3) // 2
    col1_x = PADDING
    col2_x = PADDING * 2 + col_w
    col_h = CARD_HEIGHT - body_top - 80  # leave space for footer

    # Myth column
    _draw_rounded_rect(
        draw,
        [col1_x, body_top, col1_x + col_w, body_top + col_h],
        radius=10, fill=COLOR_MYTH_BG,
        outline=COLOR_MYTH_BORDER, outline_width=2,
    )

    font_badge = _get_font(10, bold=True)
    draw.text((col1_x + 14, body_top + 14), "✗  MYTH",
              font=font_badge, fill=COLOR_MYTH_BORDER)

    myth_lines = _wrap_text(record.get("common_myth", ""), 38)
    font_body = _get_font(13)
    y_offset = body_top + 38
    for line in myth_lines[:5]:
        draw.text((col1_x + 14, y_offset), line,
                  font=font_body, fill=COLOR_MYTH_TEXT)
        y_offset += 20

    # Fact column
    _draw_rounded_rect(
        draw,
        [col2_x, body_top, col2_x + col_w, body_top + col_h],
        radius=10, fill=COLOR_FACT_BG,
        outline=COLOR_FACT_BORDER, outline_width=2,
    )

    draw.text((col2_x + 14, body_top + 14), "✓  VERIFIED FACT",
              font=font_badge, fill=COLOR_FACT_BORDER)

    fact_lines = _wrap_text(record.get("verified_fact", ""), 38)
    y_offset = body_top + 38
    for line in fact_lines[:6]:
        draw.text((col2_x + 14, y_offset), line,
                  font=font_body, fill=COLOR_FACT_TEXT)
        y_offset += 20

    # ── Footer / citation ────────────────────────────────────────────────────
    footer_y = CARD_HEIGHT - 60
    draw.line([PADDING, footer_y, CARD_WIDTH - PADDING, footer_y],
              fill=COLOR_MUTED, width=1)

    source_text = f"📚  {record.get('source', '')[:90]}"
    font_source = _get_font(10)
    draw.text((PADDING, footer_y + 10), source_text,
              font=font_source, fill=COLOR_MUTED)

    # Badge
    badge_text = "  ✅ Health Myth-Bot · Verified  "
    badge_w, badge_h = 200, 22
    badge_x = CARD_WIDTH - PADDING - badge_w
    badge_y = footer_y + 8
    _draw_rounded_rect(
        draw,
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
        radius=4, fill=COLOR_BADGE_BG,
    )
    draw.text((badge_x + 6, badge_y + 4), badge_text,
              font=font_source, fill=COLOR_WHITE)

    # ── Save ─────────────────────────────────────────────────────────────────
    safe_topic = "".join(c if c.isalnum() else "_" for c in record.get("topic", "card"))
    filename = f"myth_card_{safe_topic}.png"
    out_path = CARDS_DIR / filename
    img.save(str(out_path), format="PNG", optimize=True)

    return str(out_path.resolve())
