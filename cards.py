"""Render 1080x1920 vertical SmarterEats share-card style PNGs.

Reads output/results.csv, renders one card per ok row, sorted by
content_priority desc, into output/cards/.

Layout (top → bottom):
  - Green category pill (top-left)
  - Left-aligned bold headline (the hook)
  - Two side-by-side comparison cards
      - Winner: light green tint + green border + "BEST CHOICE" label
      - Loser:  light gray + subtle border
      - Each card has a soft-warm score circle with the score in orange
  - Optional bullet reasons under the winner (from whatHelps)
  - Centered gray summary line
  - Centered footer (logo if assets/logo.png exists, else "SmarterEats")
"""

from __future__ import annotations

import os
import re
import sys
from typing import Optional

import pandas as pd
from PIL import Image, ImageDraw, ImageFont


# --- Canvas -----------------------------------------------------------------

CARD_W = 1080
CARD_H = 1920
SIDE_PAD = 80

# --- Palette ----------------------------------------------------------------

BG_COLOR     = (255, 255, 255)
TEXT_DARK    = (17, 24, 39)       # near-black headline / titles
TEXT_BODY    = (55, 65, 81)       # bullet body
MUTED        = (107, 114, 128)    # summary, /10, footer
ACCENT_GREEN = (22, 163, 74)      # pill, BEST CHOICE, bullet dot, winner border
ACCENT_GREEN_SOFT = (220, 252, 231)  # winner card fill (light green)
ACCENT_GREEN_BORDER_LIGHT = (134, 239, 172)
ORANGE       = (234, 88, 12)      # score number
WARM_BG      = (255, 237, 213)    # score circle fill (warm peach)
WARM_BORDER  = (253, 186, 116)    # score circle border
LOSER_BG     = (244, 244, 245)    # neutral light gray card
LOSER_BORDER = (228, 228, 231)

# --- Layout constants -------------------------------------------------------

PILL_X = 80
PILL_Y = 80
PILL_PAD_X = 28
PILL_PAD_Y = 14

HOOK_X = 80
HOOK_Y_TOP = 220
HOOK_MAX_W = 920
HOOK_MAX_LINES = 3

CARDS_Y_TOP = 720
CARDS_HEIGHT = 600
CARD_GAP = 40
CARD_W_INNER = (CARD_W - SIDE_PAD * 2 - CARD_GAP) // 2  # 440
CARD_RADIUS = 36

CIRCLE_R = 130

BULLETS_Y_TOP = 1380
BULLET_DOT_R = 9
BULLETS_MAX = 2
BULLET_LINE_GAP = 16

SUMMARY_Y = 1700
FOOTER_Y = 1830

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
    """Wrap into <= max_lines lines that each fit max_width, else None."""
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
    "snack": "Snack",
    "meal": "Meal",
    "breakfast": "Breakfast",
    "dessert": "Dessert",
    "treat": "Treat",
    "post_workout": "Post-Workout",
    "pre_workout": "Pre-Workout",
    "ingredient": "Ingredient",
    "beverage": "Beverage",
    "health_halo": "Health Halo",
    "kids_food": "Kids Food",
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


# --- Drawers ----------------------------------------------------------------

def _draw_pill(draw, text: str) -> int:
    """Top-left green pill. Returns its bottom-y (for layout reference)."""
    font = _font(34, bold=True)
    text_w = _tw(draw, text, font)
    text_h = _th(font, text)
    pill_w = text_w + PILL_PAD_X * 2
    pill_h = text_h + PILL_PAD_Y * 2 + 4
    x0, y0 = PILL_X, PILL_Y
    x1, y1 = x0 + pill_w, y0 + pill_h
    draw.rounded_rectangle((x0, y0, x1, y1), radius=pill_h // 2, fill=ACCENT_GREEN)
    # Vertical text centering uses the font's actual top offset.
    bbox = font.getbbox(text)
    text_y = y0 + (pill_h - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((x0 + PILL_PAD_X, text_y), text, font=font, fill=(255, 255, 255))
    return y1


def _draw_hook(draw, hook: str) -> None:
    """Left-aligned bold headline."""
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


def _draw_score_circle(draw, cx: int, cy: int, score: Optional[int]) -> None:
    r = CIRCLE_R
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=WARM_BG, outline=WARM_BORDER, width=2)
    text = str(score if score is not None else "—")
    # Auto-fit the digit (1-2 chars typically).
    for size in (200, 180, 160, 140):
        font = _font(size, bold=True)
        if _tw(draw, text, font) <= r * 1.6:
            break
    bbox = font.getbbox(text)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    text_x = cx - text_w // 2 - bbox[0]
    text_y = cy - text_h // 2 - bbox[1]
    draw.text((text_x, text_y), text, font=font, fill=ORANGE)


def _draw_comparison_card(
    draw,
    x0: int,
    y0: int,
    width: int,
    height: int,
    display_name: str,
    score: Optional[int],
    is_winner: bool,
) -> None:
    x1, y1 = x0 + width, y0 + height
    if is_winner:
        fill, border, border_w = ACCENT_GREEN_SOFT, ACCENT_GREEN, 4
    else:
        fill, border, border_w = LOSER_BG, LOSER_BORDER, 2
    draw.rounded_rectangle((x0, y0, x1, y1), radius=CARD_RADIUS, fill=fill,
                           outline=border, width=border_w)

    inner_pad = 28
    # Reserve the top band for BEST CHOICE so winner and loser titles align.
    BEST_CHOICE_BAND = 70  # vertical space taken by capsule + gap
    cur_y = y0 + 30

    if is_winner:
        label = "BEST CHOICE"
        label_font = _font(28, bold=True)
        label_w = _tw(draw, label, label_font)
        label_h = _th(label_font, label)
        cap_pad_x, cap_pad_y = 16, 8
        cap_w = label_w + cap_pad_x * 2
        cap_h = label_h + cap_pad_y * 2 + 2
        cap_x = x0 + (width - cap_w) // 2
        cap_y = cur_y
        draw.rounded_rectangle(
            (cap_x, cap_y, cap_x + cap_w, cap_y + cap_h),
            radius=cap_h // 2, fill=ACCENT_GREEN,
        )
        bbox = label_font.getbbox(label)
        ty = cap_y + (cap_h - (bbox[3] - bbox[1])) // 2 - bbox[1]
        draw.text((cap_x + cap_pad_x, ty), label, font=label_font, fill=(255, 255, 255))
    cur_y += BEST_CHOICE_BAND  # advance past the reserved band on both cards

    # Display name — auto-fit, up to 2 lines, centered.
    name_max_w = width - inner_pad * 2
    name_lines, name_font, name_line_h = _fit(
        draw, display_name or "",
        max_width=name_max_w, max_lines=2,
        sizes=(48, 42, 36, 32, 28),
        bold=True,
    )
    for ln in name_lines:
        lw = _tw(draw, ln, name_font)
        draw.text((x0 + (width - lw) // 2, cur_y), ln, font=name_font, fill=TEXT_DARK)
        cur_y += name_line_h
    cur_y += 14

    # Score circle
    cx = x0 + width // 2
    circle_cy = cur_y + CIRCLE_R + 6
    _draw_score_circle(draw, cx, circle_cy, score)

    # /10 below circle
    suffix = "/ 10"
    suffix_font = _font(36, bold=True)
    sw = _tw(draw, suffix, suffix_font)
    sy = circle_cy + CIRCLE_R + 26
    draw.text((cx - sw // 2, sy), suffix, font=suffix_font, fill=MUTED)


def _split_helps(raw: str) -> list[str]:
    if not raw:
        return []
    # results.csv stores helps joined by " ; "
    parts = re.split(r"\s*;\s*", str(raw))
    return [p.strip() for p in parts if p and p.strip()]


def _draw_bullets(draw, bullets: list[str]) -> int:
    """Render up to BULLETS_MAX bullets under the cards. Returns y after."""
    if not bullets:
        return BULLETS_Y_TOP
    bullets = bullets[:BULLETS_MAX]
    font = _font(34, bold=False)
    line_h = int(font.size * 1.25)
    max_text_w = CARD_W - SIDE_PAD * 2 - 60  # room for dot + gap

    y = BULLETS_Y_TOP
    for b in bullets:
        lines = _wrap_if_fits(draw, b, font, max_text_w, 2) or _wrap_force(draw, b, font, max_text_w, 2)
        # Bullet dot on the first line.
        first = True
        for ln in lines:
            if first:
                dot_cx = SIDE_PAD + BULLET_DOT_R + 4
                dot_cy = y + line_h // 2 + 2
                draw.ellipse(
                    (dot_cx - BULLET_DOT_R, dot_cy - BULLET_DOT_R,
                     dot_cx + BULLET_DOT_R, dot_cy + BULLET_DOT_R),
                    fill=ACCENT_GREEN,
                )
                text_x = dot_cx + BULLET_DOT_R + 18
            else:
                text_x = SIDE_PAD + (BULLET_DOT_R * 2) + 22  # indent continuation
            draw.text((text_x, y), ln, font=font, fill=TEXT_BODY)
            y += line_h
            first = False
        y += BULLET_LINE_GAP
    return y


def _make_summary(
    winner_helps: list[str],
    loser_helps: list[str],
    explicit_summary: str,
    a_score: Optional[int],
    b_score: Optional[int],
) -> str:
    if explicit_summary:
        return explicit_summary
    if a_score is not None and b_score is not None and a_score == b_score:
        return "Closer than you think."
    if not winner_helps:
        return ""
    # Distill: keep the first comma-separated phrase of up to 2 helps.
    parts: list[str] = []
    for h in winner_helps[:2]:
        first = h.split(",")[0].strip().rstrip(".")
        # Trim trailing prepositional clauses for punchiness.
        first = re.sub(r"\s+(supports|with|than|per|in)\s+.*$", "", first, flags=re.I)
        if first and first.lower() not in (p.lower() for p in parts):
            parts.append(first)
    if not parts:
        return ""
    out = parts[0]
    for p in parts[1:]:
        out += ", " + (p[:1].lower() + p[1:] if len(p) > 1 else p.lower())
    return out


def _draw_summary(draw, text: str) -> None:
    if not text:
        return
    lines, font, line_h = _fit(
        draw, text,
        max_width=CARD_W - SIDE_PAD * 2,
        max_lines=2,
        sizes=(34, 30, 28),
        bold=False,
    )
    y = SUMMARY_Y
    for ln in lines:
        lw = _tw(draw, ln, font)
        draw.text(((CARD_W - lw) // 2, y), ln, font=font, fill=MUTED)
        y += line_h


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


def _draw_footer(img: Image.Image, draw: ImageDraw.ImageDraw) -> None:
    logo = _logo_path()
    if logo:
        try:
            with Image.open(logo) as src:
                src = src.convert("RGBA")
                target_h = 72
                ratio = target_h / src.height
                target_w = int(src.width * ratio)
                src = src.resize((target_w, target_h), Image.LANCZOS)
            wm = "SmarterEats"
            wm_font = _font(30, bold=True)
            ww = _tw(draw, wm, wm_font)
            gap = 14
            total_w = target_w + gap + ww
            if total_w <= CARD_W - SIDE_PAD * 2:
                lx = (CARD_W - total_w) // 2
                img.paste(src, (lx, FOOTER_Y), src)
                bbox = wm_font.getbbox(wm)
                ty = FOOTER_Y + (target_h - (bbox[3] - bbox[1])) // 2 - bbox[1]
                draw.text((lx + target_w + gap, ty), wm, font=wm_font, fill=TEXT_DARK)
            else:
                lx = (CARD_W - target_w) // 2
                img.paste(src, (lx, FOOTER_Y), src)
            return
        except Exception:
            pass  # fall through to text fallback
    text = "SmarterEats"
    font = _font(34, bold=True)
    w = _tw(draw, text, font)
    draw.text(((CARD_W - w) // 2, FOOTER_Y + 16), text, font=font, fill=TEXT_DARK)


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


def render_card(row: dict) -> Image.Image:
    img = Image.new("RGB", (CARD_W, CARD_H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    hook = (row.get("hook") or "").strip()
    purpose = (row.get("purpose") or "").strip()
    framing = (row.get("framing_type") or "").strip()
    a_display = (row.get("food_a_display_name") or row.get("food_a_name") or "").strip()
    b_display = (row.get("food_b_display_name") or row.get("food_b_name") or "").strip()
    a_score = _to_int(row.get("food_a_score"))
    b_score = _to_int(row.get("food_b_score"))
    a_helps = _split_helps(row.get("food_a_what_helps") or "")
    b_helps = _split_helps(row.get("food_b_what_helps") or "")

    # Determine winner.
    if a_score is None or b_score is None:
        winner = None
    elif a_score > b_score:
        winner = "a"
    elif b_score > a_score:
        winner = "b"
    else:
        winner = None  # tie → no highlighted winner

    # Pill text.
    pill = _pill_label(framing, purpose)
    _draw_pill(draw, pill)

    # Hook headline.
    _draw_hook(draw, hook)

    # Comparison cards (side-by-side).
    a_x = SIDE_PAD
    b_x = SIDE_PAD + CARD_W_INNER + CARD_GAP
    _draw_comparison_card(
        draw, a_x, CARDS_Y_TOP, CARD_W_INNER, CARDS_HEIGHT,
        display_name=a_display, score=a_score, is_winner=(winner == "a"),
    )
    _draw_comparison_card(
        draw, b_x, CARDS_Y_TOP, CARD_W_INNER, CARDS_HEIGHT,
        display_name=b_display, score=b_score, is_winner=(winner == "b"),
    )

    # Bullets under winner.
    if winner == "a":
        winner_helps = a_helps
    elif winner == "b":
        winner_helps = b_helps
    else:
        winner_helps = []
    _draw_bullets(draw, winner_helps[:BULLETS_MAX])

    # Summary line.
    explicit_summary = (row.get("summary") or "").strip()
    summary = _make_summary(
        winner_helps,
        loser_helps=(b_helps if winner == "a" else a_helps),
        explicit_summary=explicit_summary,
        a_score=a_score, b_score=b_score,
    )
    _draw_summary(draw, summary)

    # Footer (logo if assets/logo.png exists, else text).
    _draw_footer(img, draw)

    return img


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
