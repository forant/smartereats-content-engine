"""Render 1080x1920 vertical SmarterEats share-card style PNGs.

The card is ABOUT food_a. food_b is a baseline reference: shown clearly,
but visually quieter and never praised.

Layout (top → bottom), all inside a centered white rounded container with
soft shadow on a light-gray canvas:

  - Green category pill (top-left)
  - Left-aligned bold headline (food_a as subject)
  - Two side-by-side cards, equal width:
      - food_a (left): verdict pill (BEST CHOICE / AVOID) from food_a's
        absolute score, tinted background + strong border, larger score
        circle, full-saturation orange digit
      - food_b (right): light-gray fill, no pill, smaller circle,
        muted-orange digit — context only
  - Up to 2 left-aligned bullets describing food_a only
  - Centered logo
"""

from __future__ import annotations

import os
import re
import sys
from typing import Optional

import pandas as pd
from PIL import Image, ImageDraw, ImageFilter, ImageFont


# --- Canvas / container -----------------------------------------------------

CARD_W = 1080
CARD_H = 1920
SIDE_PAD = 90  # horizontal padding inside the white container

CONTAINER_MARGIN = 40
CONTAINER_X0 = CONTAINER_MARGIN
CONTAINER_Y0 = CONTAINER_MARGIN
CONTAINER_X1 = CARD_W - CONTAINER_MARGIN
CONTAINER_Y1 = CARD_H - CONTAINER_MARGIN
CONTAINER_RADIUS = 40

# --- Palette ----------------------------------------------------------------

CANVAS_TOP    = (252, 252, 253)   # subtle vertical gradient — top
CANVAS_BOTTOM = (242, 243, 246)   # subtle vertical gradient — bottom
CANVAS_BG    = (247, 247, 247)    # fallback when gradient is disabled
CONTAINER_BG = (255, 255, 255)
TEXT_DARK    = (17, 24, 39)
TEXT_BODY    = (55, 65, 81)
MUTED        = (107, 114, 128)
ACCENT_GREEN = (22, 163, 74)
GREEN_SOFT   = (220, 252, 231)
GREEN_BORDER = (134, 239, 172)
WARN_AMBER   = (217, 119, 6)
WARN_AMBER_SOFT = (254, 243, 199)
WARN_AMBER_BORDER = (252, 211, 77)
DANGER_RED   = (220, 38, 38)
DANGER_SOFT  = (254, 226, 226)
DANGER_BORDER = (252, 165, 165)
ORANGE       = (234, 88, 12)
ORANGE_MUTED = (180, 110, 60)
WARM_BG      = (255, 237, 213)
WARM_BG_DIM  = (245, 235, 220)
WARM_BORDER  = (253, 186, 116)
WARM_BORDER_DIM = (220, 200, 180)
REFERENCE_BG = (244, 244, 245)
REFERENCE_BORDER = (228, 228, 231)
SHADOW_RGBA  = (0, 0, 0, 55)

# --- Layout constants -------------------------------------------------------

PILL_X = SIDE_PAD
PILL_Y = 110
PILL_PAD_X = 24
PILL_PAD_Y = 12

HOOK_X = SIDE_PAD
HOOK_Y_TOP = 220
HOOK_MAX_W = 880
HOOK_MAX_LINES = 3

# Side-by-side comparison cards (equal width).
CARDS_Y_TOP = 680
CARDS_HEIGHT = 600
CARD_GAP = 40
CARD_W_INNER = (CARD_W - SIDE_PAD * 2 - CARD_GAP) // 2  # 425
CARD_RADIUS = 36

# Both cards share the same content y-bands so names + circles + /10
# horizontally align across the pair. food_a uses the pill band; food_b
# leaves it empty.
CARD_PILL_Y_LOCAL = 30
CARD_NAME_Y_LOCAL = 120
CARD_CIRCLE_CY_LOCAL = 350
CARD_SUFFIX_Y_LOCAL = 510

CIRCLE_R_FOOD_A = 130   # subject — slightly bigger
CIRCLE_R_FOOD_B = 105   # baseline — visually quieter

BULLETS_Y_TOP = 1330
BULLET_DOT_R = 9
BULLETS_MAX = 2
BULLET_LINE_GAP = 14

LOGO_TARGET_H = 144   # ~1.5x the prior 96
LOGO_Y = 1660

# Shadow / glow presets — subtle and consistent.
CARD_SHADOW_HERO    = dict(offset_y=14, blur=22, alpha=55)
CARD_SHADOW_SUBJECT = dict(offset_y=10, blur=16, alpha=50)
CARD_SHADOW_QUIET   = dict(offset_y=5,  blur=10, alpha=25)
PILL_SHADOW         = dict(offset_y=3,  blur=5,  alpha=35)
CIRCLE_GLOW_STRONG  = dict(blur=22, alpha=70, expand=18)
CIRCLE_GLOW_QUIET   = dict(blur=14, alpha=28, expand=8)

# --- Font loading -----------------------------------------------------------

FONT_BOLD_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]
FONT_REGULAR_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]


def _font_path(candidates: list[str]) -> Optional[str]:
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


_BOLD_PATH = _font_path(FONT_BOLD_CANDIDATES)
_REGULAR_PATH = _font_path(FONT_REGULAR_CANDIDATES)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = _BOLD_PATH if bold else _REGULAR_PATH
    if path is None:
        return ImageFont.load_default()
    return ImageFont.truetype(path, size=size)


# --- Text helpers -----------------------------------------------------------

def _tw(draw, text: str, font) -> int:
    return int(draw.textlength(text, font=font))


def _th(font, text: str = "Hg") -> int:
    bbox = font.getbbox(text)
    return bbox[3] - bbox[1]


def _wrap_if_fits(draw, text: str, font, max_width: int, max_lines: int) -> Optional[list[str]]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for w in words:
        if _tw(draw, w, font) > max_width:
            return None
        candidate = (current + " " + w).strip()
        if _tw(draw, candidate, font) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            if len(lines) >= max_lines:
                return None
        current = w
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        return None
    return lines


def _truncate(draw, text: str, font, max_width: int) -> str:
    if _tw(draw, text, font) <= max_width:
        return text
    while text and _tw(draw, text + "…", font) > max_width:
        text = text[:-1].rstrip()
    return text + "…"


def _wrap_force(draw, text: str, font, max_width: int, max_lines: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for w in words:
        candidate = (current + " " + w).strip()
        if _tw(draw, candidate, font) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            if len(lines) == max_lines:
                lines[-1] = _truncate(draw, lines[-1] + " " + w, font, max_width)
                return lines
        current = w
    if current and len(lines) < max_lines:
        lines.append(current)
    return lines or [_truncate(draw, text, font, max_width)]


def _fit(draw, text: str, max_width: int, max_lines: int, sizes: tuple[int, ...], bold: bool = True
         ) -> tuple[list[str], ImageFont.FreeTypeFont, int]:
    for size in sizes:
        font = _font(size, bold=bold)
        lines = _wrap_if_fits(draw, text, font, max_width, max_lines)
        if lines is not None:
            return lines, font, int(size * 1.18)
    font = _font(sizes[-1], bold=bold)
    return _wrap_force(draw, text, font, max_width, max_lines), font, int(sizes[-1] * 1.18)


# --- Canvas + shadow / glow effects -----------------------------------------

def _gradient_canvas() -> Image.Image:
    """Return a fresh canvas with a very light vertical gradient."""
    img = Image.new("RGB", (CARD_W, CARD_H))
    draw = ImageDraw.Draw(img)
    h = CARD_H - 1
    for y in range(CARD_H):
        t = y / h
        r = round(CANVAS_TOP[0] + (CANVAS_BOTTOM[0] - CANVAS_TOP[0]) * t)
        g = round(CANVAS_TOP[1] + (CANVAS_BOTTOM[1] - CANVAS_TOP[1]) * t)
        b = round(CANVAS_TOP[2] + (CANVAS_BOTTOM[2] - CANVAS_TOP[2]) * t)
        draw.line([(0, y), (CARD_W, y)], fill=(r, g, b))
    return img


def _composite_layer(canvas: Image.Image, layer: Image.Image) -> None:
    """Composite an RGBA layer onto an RGB canvas (in place)."""
    base = canvas.convert("RGBA")
    base = Image.alpha_composite(base, layer)
    canvas.paste(base.convert("RGB"), (0, 0))


def _add_rect_shadow(canvas: Image.Image, x0: int, y0: int, x1: int, y1: int,
                     radius: int, *, offset_y: int, blur: int, alpha: int) -> None:
    """Drop a soft shadow under a rounded rect, then composite onto canvas."""
    layer = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        (x0 + 2, y0 + offset_y, x1 + 2, y1 + offset_y),
        radius=radius, fill=(0, 0, 0, alpha),
    )
    layer = layer.filter(ImageFilter.GaussianBlur(radius=blur))
    _composite_layer(canvas, layer)


def _add_circle_glow(canvas: Image.Image, cx: int, cy: int, radius: int,
                     color: tuple, *, blur: int, alpha: int, expand: int) -> None:
    """Soft warm halo behind a score circle."""
    layer = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    r = radius + expand
    ImageDraw.Draw(layer).ellipse(
        (cx - r, cy - r, cx + r, cy + r),
        fill=color + (alpha,),
    )
    layer = layer.filter(ImageFilter.GaussianBlur(radius=blur))
    _composite_layer(canvas, layer)


# --- Filename ---------------------------------------------------------------

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "x"


def _filename(pair_id: str, a_name: str, b_name: str) -> str:
    pid = re.sub(r"[^A-Za-z0-9]", "", str(pair_id)) or "x"
    pid = pid.zfill(3)
    return f"pair_{pid}_{_slug(a_name)}-vs-{_slug(b_name)}.png"


# --- Pill / category text ---------------------------------------------------

PILL_LABELS = {
    "snack": "Snack", "meal": "Meal", "breakfast": "Breakfast",
    "dessert": "Dessert", "treat": "Treat",
    "post_workout": "Post-Workout", "pre_workout": "Pre-Workout",
    "ingredient": "Ingredient", "beverage": "Beverage",
    "health_halo": "Health Halo", "kids_food": "Kids Food",
    "sugar_shock": "Sugar Shock",
}


def _pill_label(framing: str, purpose: str) -> str:
    framing = (framing or "").strip().lower()
    if framing and framing in PILL_LABELS:
        return PILL_LABELS[framing]
    if framing:
        return framing.replace("_", " ").title()
    purpose = (purpose or "").strip().lower()
    if purpose and purpose in PILL_LABELS:
        return PILL_LABELS[purpose]
    return purpose.replace("_", " ").title() if purpose else "Snack"


def _category_pill_label(fmt: str, framing: str, purpose: str) -> str:
    """Top-left pill text — overrides per format."""
    fmt = (fmt or "").lower()
    if fmt == "exposure":
        if (framing or "").lower() == "sugar_shock":
            return "Hidden Sugar"
        return "Not What You Think"
    if fmt == "swap":
        return "Better Choice"
    return _pill_label(framing, purpose)


# --- Verdict (food_a absolute) ----------------------------------------------

def _verdict(score: Optional[int]) -> tuple[str, tuple, tuple, tuple, tuple]:
    """Return (label, pill_bg, pill_fg, card_bg, card_border) for food_a,
    keyed off food_a's *absolute* score. Binary verdict per spec."""
    if score is None:
        return ("", REFERENCE_BG, MUTED, REFERENCE_BG, REFERENCE_BORDER)
    if score >= 7:
        return ("BEST CHOICE", ACCENT_GREEN, (255, 255, 255),
                GREEN_SOFT, ACCENT_GREEN)
    return ("AVOID", DANGER_RED, (255, 255, 255),
            DANGER_SOFT, DANGER_BORDER)


# --- Bullet cleanup ---------------------------------------------------------

def _clean_bullet(text: str) -> str:
    if not text:
        return ""
    s = str(text).strip()
    s = re.sub(r"\s+than\s+(the\s+)?competitors?\b.*$", "", s, flags=re.I)
    s = re.sub(r"\b(?:the\s+)?competitors?\b", "alternatives", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip().rstrip(".")
    if s:
        s = s[0].upper() + s[1:]
    return s


def _split_helps(raw: str) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"\s*;\s*", str(raw))
    cleaned = [_clean_bullet(p) for p in parts]
    return [p for p in cleaned if p]


# --- Drawers ----------------------------------------------------------------

def _draw_container(canvas: Image.Image) -> ImageDraw.ImageDraw:
    """Soft shadow + white rounded container."""
    shadow = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rounded_rectangle(
        (CONTAINER_X0 + 6, CONTAINER_Y0 + 14,
         CONTAINER_X1 + 6, CONTAINER_Y1 + 14),
        radius=CONTAINER_RADIUS,
        fill=SHADOW_RGBA,
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=22))
    base = canvas.convert("RGBA")
    base = Image.alpha_composite(base, shadow)
    canvas.paste(base.convert("RGB"), (0, 0))

    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (CONTAINER_X0, CONTAINER_Y0, CONTAINER_X1, CONTAINER_Y1),
        radius=CONTAINER_RADIUS,
        fill=CONTAINER_BG,
    )
    return draw


def _draw_pill(canvas: Image.Image, draw, text: str) -> None:
    font = _font(34, bold=True)
    text_w = _tw(draw, text, font)
    text_h = _th(font, text)
    pill_w = text_w + PILL_PAD_X * 2
    pill_h = text_h + PILL_PAD_Y * 2 + 4
    x0, y0 = PILL_X, PILL_Y
    x1, y1 = x0 + pill_w, y0 + pill_h
    _add_rect_shadow(canvas, x0, y0, x1, y1, radius=pill_h // 2, **PILL_SHADOW)
    draw.rounded_rectangle((x0, y0, x1, y1), radius=pill_h // 2, fill=ACCENT_GREEN)
    bbox = font.getbbox(text)
    text_y = y0 + (pill_h - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((x0 + PILL_PAD_X, text_y), text, font=font, fill=(255, 255, 255))


def _draw_hook(draw, hook: str) -> None:
    if not hook:
        return
    lines, font, line_h = _fit(
        draw, hook,
        max_width=HOOK_MAX_W,
        max_lines=HOOK_MAX_LINES,
        sizes=(96, 88, 80, 72, 64, 56),
        bold=True,
    )
    y = HOOK_Y_TOP
    for line in lines:
        draw.text((HOOK_X, y), line, font=font, fill=TEXT_DARK)
        y += line_h


def _draw_capsule(canvas: Image.Image, draw, text: str, x_center: int, y: int,
                  fill, fg, *, font_size: int = 28) -> int:
    font = _font(font_size, bold=True)
    label_w = _tw(draw, text, font)
    label_h = _th(font, text)
    pad_x, pad_y = 16, 9
    cap_w = label_w + pad_x * 2
    cap_h = label_h + pad_y * 2 + 2
    cap_x = x_center - cap_w // 2
    _add_rect_shadow(canvas, cap_x, y, cap_x + cap_w, y + cap_h,
                     radius=cap_h // 2, **PILL_SHADOW)
    draw.rounded_rectangle(
        (cap_x, y, cap_x + cap_w, y + cap_h),
        radius=cap_h // 2, fill=fill,
    )
    bbox = font.getbbox(text)
    ty = y + (cap_h - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((cap_x + pad_x, ty), text, font=font, fill=fg)
    return cap_h


def _draw_score_circle(canvas: Image.Image, draw, cx: int, cy: int,
                       score: Optional[int], *,
                       radius: int, fill, border, color,
                       glow: Optional[dict] = None) -> None:
    if glow is not None:
        # Halo color matches the score color so the circle reads "lit".
        _add_circle_glow(canvas, cx, cy, radius, color, **glow)
    r = radius
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill, outline=border, width=2)
    text = str(score if score is not None else "—")
    target = int(r * 1.6)
    for size in (240, 220, 200, 180, 160, 140):
        font = _font(size, bold=True)
        if _tw(draw, text, font) <= target:
            break
    bbox = font.getbbox(text)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    text_x = cx - text_w // 2 - bbox[0]
    text_y = cy - text_h // 2 - bbox[1]
    draw.text((text_x, text_y), text, font=font, fill=color)


def _draw_comparison_card(
    canvas: Image.Image,
    draw,
    x0: int,
    display_name: str,
    score: Optional[int],
    *,
    is_subject: bool,
) -> None:
    """One card in the side-by-side pair.

    is_subject=True  → food_a, the focus: tinted bg, BEST CHOICE / AVOID
                        pill, full-color score.
    is_subject=False → food_b, the baseline: light gray, no pill, muted
                        score, smaller circle.
    """
    y0 = CARDS_Y_TOP
    x1 = x0 + CARD_W_INNER
    y1 = y0 + CARDS_HEIGHT
    cx = x0 + CARD_W_INNER // 2

    if is_subject:
        label, pill_bg, pill_fg, card_bg, card_border = _verdict(score)
        border_w = 4
        score_color, circle_fill, circle_border = ORANGE, WARM_BG, WARM_BORDER
        radius = CIRCLE_R_FOOD_A
        name_sizes = (54, 48, 44, 40, 36)
        name_color = TEXT_DARK
        shadow = CARD_SHADOW_SUBJECT
        glow = CIRCLE_GLOW_STRONG
    else:
        label = ""
        card_bg, card_border = REFERENCE_BG, REFERENCE_BORDER
        border_w = 2
        score_color, circle_fill, circle_border = ORANGE_MUTED, WARM_BG_DIM, WARM_BORDER_DIM
        radius = CIRCLE_R_FOOD_B
        name_sizes = (44, 40, 36, 32, 28)
        name_color = TEXT_BODY
        shadow = CARD_SHADOW_QUIET
        glow = CIRCLE_GLOW_QUIET

    _add_rect_shadow(canvas, x0, y0, x1, y1, radius=CARD_RADIUS, **shadow)
    draw.rounded_rectangle((x0, y0, x1, y1), radius=CARD_RADIUS,
                           fill=card_bg, outline=card_border, width=border_w)

    # Pill band — only the subject card uses it; the baseline leaves the
    # band empty so the two cards' name/circle/suffix lines stay aligned.
    if is_subject and label:
        _draw_capsule(canvas, draw, label, cx, y0 + CARD_PILL_Y_LOCAL,
                      pill_bg, pill_fg, font_size=30)

    # Display name (auto-fit, up to 2 lines, centered horizontally).
    name_max_w = CARD_W_INNER - 56
    name_lines, name_font, name_line_h = _fit(
        draw, display_name or "",
        max_width=name_max_w, max_lines=2,
        sizes=name_sizes,
        bold=True,
    )
    name_block_h = name_line_h * len(name_lines)
    name_y = y0 + CARD_NAME_Y_LOCAL - name_block_h // 2 + name_line_h // 2
    cur_y = name_y
    for ln in name_lines:
        lw = _tw(draw, ln, name_font)
        draw.text((cx - lw // 2, cur_y), ln, font=name_font, fill=name_color)
        cur_y += name_line_h

    # Score circle — both cards center on the same y for alignment.
    circle_cy = y0 + CARD_CIRCLE_CY_LOCAL
    _draw_score_circle(canvas, draw, cx, circle_cy, score,
                       radius=radius, fill=circle_fill,
                       border=circle_border, color=score_color, glow=glow)

    # / 10 below the circle.
    suffix_font = _font(36, bold=True)
    suffix = "/ 10"
    sw = _tw(draw, suffix, suffix_font)
    suffix_color = MUTED
    draw.text((cx - sw // 2, y0 + CARD_SUFFIX_Y_LOCAL),
              suffix, font=suffix_font, fill=suffix_color)


def _draw_bullets(draw, bullets: list[str], dot_color: tuple) -> None:
    if not bullets:
        return
    bullets = bullets[:BULLETS_MAX]
    font = _font(34, bold=False)
    line_h = int(font.size * 1.25)
    max_text_w = CARD_W - SIDE_PAD * 2 - 60

    y = BULLETS_Y_TOP
    for b in bullets:
        lines = _wrap_if_fits(draw, b, font, max_text_w, 2) or _wrap_force(draw, b, font, max_text_w, 2)
        first = True
        for ln in lines:
            if first:
                dot_cx = SIDE_PAD + BULLET_DOT_R + 4
                dot_cy = y + line_h // 2 + 2
                draw.ellipse(
                    (dot_cx - BULLET_DOT_R, dot_cy - BULLET_DOT_R,
                     dot_cx + BULLET_DOT_R, dot_cy + BULLET_DOT_R),
                    fill=dot_color,
                )
                text_x = dot_cx + BULLET_DOT_R + 18
            else:
                text_x = SIDE_PAD + (BULLET_DOT_R * 2) + 22
            draw.text((text_x, y), ln, font=font, fill=TEXT_BODY)
            y += line_h
            first = False
        y += BULLET_LINE_GAP


# --- Logo / footer ----------------------------------------------------------

LOGO_CANDIDATES = [
    "assets/logo.png",
    "assets/logo.jpg",
    "assets/smartereats.png",
    "logo.png",
]


def _logo_path() -> Optional[str]:
    for p in LOGO_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _draw_footer(canvas: Image.Image, draw: ImageDraw.ImageDraw) -> None:
    logo = _logo_path()
    if logo:
        try:
            with Image.open(logo) as src:
                src = src.convert("RGBA")
                target_h = LOGO_TARGET_H
                ratio = target_h / src.height
                target_w = int(src.width * ratio)
                src = src.resize((target_w, target_h), Image.LANCZOS)
            lx = (CARD_W - target_w) // 2
            canvas.paste(src, (lx, LOGO_Y), src)
            return
        except Exception:
            pass
    text = "SmarterEats"
    font = _font(40, bold=True)
    w = _tw(draw, text, font)
    draw.text(((CARD_W - w) // 2, LOGO_Y + 16), text, font=font, fill=TEXT_DARK)


# --- Compose card -----------------------------------------------------------

def _to_int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        if isinstance(v, float) and v != v:
            return None
    except TypeError:
        pass
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _draw_hero_card(canvas: Image.Image, draw, display_name: str,
                    score: Optional[int]) -> None:
    """Single full-width card spanning both side-by-side slots — used for
    `format=exposure`. Verdict pill comes from food_a's absolute score."""
    label, pill_bg, pill_fg, card_bg, card_border = _verdict(score)

    x0 = SIDE_PAD
    y0 = CARDS_Y_TOP
    x1 = CARD_W - SIDE_PAD
    y1 = y0 + CARDS_HEIGHT
    width = x1 - x0
    cx = (x0 + x1) // 2

    _add_rect_shadow(canvas, x0, y0, x1, y1, radius=CARD_RADIUS, **CARD_SHADOW_HERO)
    draw.rounded_rectangle((x0, y0, x1, y1), radius=CARD_RADIUS,
                           fill=card_bg, outline=card_border, width=4)

    # Vertical flow: pill → name → score circle → /10. Use explicit gaps so
    # a 2-line name doesn't bleed into the circle.
    cur_y = y0 + 30
    if label:
        cap_font = _font(32, bold=True)
        cap_h = _th(cap_font, label) + 9 * 2 + 2  # matches _draw_capsule sizing
        _draw_capsule(canvas, draw, label, cx, cur_y, pill_bg, pill_fg, font_size=32)
        cur_y += cap_h + 30

    name_max_w = width - 80
    name_lines, name_font, name_line_h = _fit(
        draw, display_name or "",
        max_width=name_max_w, max_lines=2,
        sizes=(64, 56, 50, 44, 40),
        bold=True,
    )
    for ln in name_lines:
        lw = _tw(draw, ln, name_font)
        draw.text((cx - lw // 2, cur_y), ln, font=name_font, fill=TEXT_DARK)
        cur_y += name_line_h
    cur_y += 18

    radius = 150
    circle_cy = cur_y + radius
    _draw_score_circle(canvas, draw, cx, circle_cy, score,
                       radius=radius, fill=WARM_BG,
                       border=WARM_BORDER, color=ORANGE,
                       glow=CIRCLE_GLOW_STRONG)

    suffix_font = _font(40, bold=True)
    suffix = "/ 10"
    sw = _tw(draw, suffix, suffix_font)
    sy = circle_cy + radius + 18
    if sy + _th(suffix_font, suffix) > y1 - 20:
        sy = y1 - _th(suffix_font, suffix) - 24
    draw.text((cx - sw // 2, sy), suffix, font=suffix_font, fill=MUTED)


def _draw_swap_arrow(draw) -> None:
    """Centered arrow between the swap cards."""
    cx = CARD_W // 2
    cy = CARDS_Y_TOP + CARD_CIRCLE_CY_LOCAL
    font = _font(96, bold=True)
    text = "→"
    w = _tw(draw, text, font)
    bbox = font.getbbox(text)
    h = bbox[3] - bbox[1]
    draw.text((cx - w // 2, cy - h // 2 - bbox[1]), text, font=font, fill=ACCENT_GREEN)


def _draw_swap_card(canvas: Image.Image, draw, x0: int, display_name: str,
                    score: Optional[int], *, role: str) -> None:
    """A single card for the swap layout. role is 'avoid' or 'pick'."""
    y0 = CARDS_Y_TOP
    x1 = x0 + CARD_W_INNER
    y1 = y0 + CARDS_HEIGHT
    cx = x0 + CARD_W_INNER // 2

    if role == "pick":
        label = "PICK THIS"
        pill_bg, pill_fg = ACCENT_GREEN, (255, 255, 255)
        card_bg, card_border = GREEN_SOFT, ACCENT_GREEN
        score_color, circle_fill, circle_border = ORANGE, WARM_BG, WARM_BORDER
        radius = CIRCLE_R_FOOD_A
        name_color = TEXT_DARK
        border_w = 4
        shadow = CARD_SHADOW_SUBJECT
        glow = CIRCLE_GLOW_STRONG
    else:  # avoid
        label = "SWAP OUT"
        pill_bg, pill_fg = DANGER_RED, (255, 255, 255)
        card_bg, card_border = DANGER_SOFT, DANGER_BORDER
        score_color, circle_fill, circle_border = ORANGE_MUTED, WARM_BG_DIM, WARM_BORDER_DIM
        radius = CIRCLE_R_FOOD_B
        name_color = TEXT_BODY
        border_w = 3
        shadow = CARD_SHADOW_QUIET
        glow = CIRCLE_GLOW_QUIET

    _add_rect_shadow(canvas, x0, y0, x1, y1, radius=CARD_RADIUS, **shadow)
    draw.rounded_rectangle((x0, y0, x1, y1), radius=CARD_RADIUS,
                           fill=card_bg, outline=card_border, width=border_w)

    _draw_capsule(canvas, draw, label, cx, y0 + CARD_PILL_Y_LOCAL,
                  pill_bg, pill_fg, font_size=28)

    name_max_w = CARD_W_INNER - 56
    sizes = (54, 48, 44, 40, 36) if role == "pick" else (44, 40, 36, 32, 28)
    name_lines, name_font, name_line_h = _fit(
        draw, display_name or "",
        max_width=name_max_w, max_lines=2,
        sizes=sizes, bold=True,
    )
    name_block_h = name_line_h * len(name_lines)
    name_y = y0 + CARD_NAME_Y_LOCAL - name_block_h // 2 + name_line_h // 2
    cur_y = name_y
    for ln in name_lines:
        lw = _tw(draw, ln, name_font)
        draw.text((cx - lw // 2, cur_y), ln, font=name_font, fill=name_color)
        cur_y += name_line_h

    circle_cy = y0 + CARD_CIRCLE_CY_LOCAL
    _draw_score_circle(canvas, draw, cx, circle_cy, score,
                       radius=radius, fill=circle_fill,
                       border=circle_border, color=score_color, glow=glow)

    suffix_font = _font(36, bold=True)
    suffix = "/ 10"
    sw = _tw(draw, suffix, suffix_font)
    draw.text((cx - sw // 2, y0 + CARD_SUFFIX_Y_LOCAL),
              suffix, font=suffix_font, fill=MUTED)


def render_card(row: dict) -> Image.Image:
    canvas = _gradient_canvas()
    draw = _draw_container(canvas)

    fmt = (row.get("format") or "comparison").strip().lower() or "comparison"
    hook = (row.get("hook") or "").strip()
    purpose = (row.get("purpose") or "").strip()
    framing = (row.get("framing_type") or "").strip()
    a_display = (row.get("food_a_display_name") or row.get("food_a_name") or "").strip()
    b_display = (row.get("food_b_display_name") or row.get("food_b_name") or "").strip()
    a_score = _to_int(row.get("food_a_score"))
    b_score = _to_int(row.get("food_b_score"))

    a_helps = _split_helps(row.get("food_a_what_helps") or "")
    a_hurts = _split_helps(row.get("food_a_what_hurts") or "")
    b_helps = _split_helps(row.get("food_b_what_helps") or "")

    _draw_pill(canvas, draw, _category_pill_label(fmt, framing, purpose))
    _draw_hook(draw, hook)

    if fmt == "exposure":
        _draw_hero_card(canvas, draw, a_display, a_score)
        # Exposure bullets: lead with what's wrong with food_a.
        if a_score is not None and a_score < 7 and a_hurts:
            bullets, dot_color = a_hurts, DANGER_RED
        else:
            bullets, dot_color = a_helps, ACCENT_GREEN
    elif fmt == "swap":
        # food_a is what you should swap OUT; food_b is what to PICK.
        a_x = SIDE_PAD
        b_x = SIDE_PAD + CARD_W_INNER + CARD_GAP
        _draw_swap_card(canvas, draw, a_x, a_display, a_score, role="avoid")
        _draw_swap_card(canvas, draw, b_x, b_display, b_score, role="pick")
        _draw_swap_arrow(draw)
        bullets, dot_color = b_helps, ACCENT_GREEN
    else:  # comparison (default)
        a_x = SIDE_PAD
        b_x = SIDE_PAD + CARD_W_INNER + CARD_GAP
        _draw_comparison_card(canvas, draw, a_x, a_display, a_score, is_subject=True)
        _draw_comparison_card(canvas, draw, b_x, b_display, b_score, is_subject=False)
        # Bullets describe food_a's reality. Never mix in food_b's helps.
        if a_score is not None and a_score < 5:
            bullets, dot_color = a_hurts, DANGER_RED
        else:
            bullets, dot_color = a_helps, ACCENT_GREEN

    _draw_bullets(draw, bullets, dot_color)
    _draw_footer(canvas, draw)

    return canvas


# --- Public entrypoint ------------------------------------------------------

def render_cards(results_csv_path: str = "output/results.csv",
                 output_dir: str = "output/cards") -> int:
    df = pd.read_csv(results_csv_path, dtype=str, keep_default_na=False)
    df = df[df["status"] == "ok"].copy()
    if df.empty:
        return 0

    df["content_priority"] = pd.to_numeric(df["content_priority"], errors="coerce").fillna(0)
    df = df.sort_values("content_priority", ascending=False)

    os.makedirs(output_dir, exist_ok=True)

    rendered = 0
    for _, row in df.iterrows():
        row_dict = row.to_dict()
        try:
            img = render_card(row_dict)
            a_disp = row_dict.get("food_a_display_name") or row_dict.get("food_a_name") or "a"
            b_disp = row_dict.get("food_b_display_name") or row_dict.get("food_b_name") or "b"
            path = os.path.join(output_dir, _filename(row_dict.get("pair_id", "x"), a_disp, b_disp))
            img.save(path, "PNG")
            rendered += 1
        except Exception as e:
            print(f"[cards] render failed for pair {row_dict.get('pair_id')}: {e}", file=sys.stderr)
            continue
    return rendered


if __name__ == "__main__":
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "output/results.csv"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "output/cards"
    n = render_cards(csv_path, out_dir)
    print(f"Rendered {n} card(s) to {out_dir}")
