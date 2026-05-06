"""SmarterEats content engine.

Reads pairs of foods from input.csv, looks each barcode up in a local SQLite
copy of the Open Food Facts data, calls the SmarterEats backend /score
endpoint for each, and writes a results CSV with deterministic hooks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests


DEFAULT_DB_PATH = "./foodscore.db"
SCORE_TIMEOUT_SECONDS = 60


# ---------------------------------------------------------------------------
# DB lookup
# ---------------------------------------------------------------------------

def open_db(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise FileNotFoundError(
            f"Food DB not found at {db_path}. "
            "Set FOOD_DB_PATH or place the SQLite file at ./foodscore.db."
        )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def _barcode_variants(barcode: str) -> list[str]:
    """Return barcode variants to try when looking up.

    OFF stores 13-digit EAN. US UPCs (12 digits) are typically stored with
    a leading '0'. Spreadsheet apps (Excel, Numbers, Google Sheets) strip
    leading zeros aggressively, so 11-digit input is often a 13-digit code
    with two leading zeros lost — try every plausible padding so the user
    doesn't have to hand-fix every row."""
    out: list[str] = [barcode]
    if barcode.isdigit():
        # Zero-pad up to 12- and 13-digit forms (no-op when already that long).
        for n in (12, 13):
            padded = barcode.zfill(n)
            if padded != barcode:
                out.append(padded)
        # Strip a single leading zero too (covers the inverse case).
        if len(barcode) >= 2 and barcode.startswith("0"):
            out.append(barcode[1:])
    # Dedupe while preserving order.
    seen = set()
    return [b for b in out if not (b in seen or seen.add(b))]


def lookup_product(conn: sqlite3.Connection, barcode: str) -> Optional[dict]:
    """Return a dict with keys: code, name, brand, nutriments (dict),
    serving_size, serving_quantity, ingredients_text. None if not found."""
    barcode = barcode.strip()
    if not barcode:
        return None

    has_canonical = _table_exists(conn, "canonical_products")
    has_products = _table_exists(conn, "products")

    for variant in _barcode_variants(barcode):
        if has_canonical:
            row = conn.execute(
                "SELECT code, canonical_name AS name, canonical_brand AS brand, "
                "nutriments, serving_size, serving_quantity, ingredients_text "
                "FROM canonical_products WHERE code = ?",
                (variant,),
            ).fetchone()
            if row:
                return _row_to_product(row)

        if has_products:
            row = conn.execute(
                "SELECT code, product_name AS name, brands AS brand, "
                "nutriments, serving_size, serving_quantity, ingredients_text "
                "FROM products WHERE code = ?",
                (variant,),
            ).fetchone()
            if row:
                return _row_to_product(row)

    return None


def _row_to_product(row: sqlite3.Row) -> dict:
    nutriments_raw = row["nutriments"]
    nutriments: dict = {}
    if nutriments_raw:
        try:
            nutriments = json.loads(nutriments_raw)
        except (json.JSONDecodeError, TypeError):
            nutriments = {}
    return {
        "code": row["code"],
        "name": row["name"] or "",
        "brand": row["brand"] or "",
        "nutriments": nutriments,
        "serving_size": row["serving_size"],
        "serving_quantity": row["serving_quantity"],
        "ingredients_text": row["ingredients_text"],
    }


# ---------------------------------------------------------------------------
# Mapping product -> /score request body
# ---------------------------------------------------------------------------

def _f(value: Any) -> Optional[float]:
    """Coerce numeric-ish values to float; None if not numeric."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _per_serving(per_100g: Optional[float], grams: Optional[float]) -> Optional[float]:
    if per_100g is None or grams is None:
        return None
    return round(per_100g * grams / 100.0, 2)


def _parse_ingredients(text: Optional[str]) -> list[str]:
    if not text:
        return []
    # OFF ingredients_text uses commas; parens and underscores around allergens.
    cleaned = re.sub(r"[_*]", "", text)
    parts = re.split(r"[,;]", cleaned)
    return [p.strip() for p in parts if p and p.strip()]


def build_extracted_nutrition(product: dict) -> dict:
    nutri = product.get("nutriments") or {}
    serving_g = _f(product.get("serving_quantity"))

    # If we have a serving_quantity in grams, scale per-100g values to per serving.
    # Otherwise, prefer any explicit _serving keys, else fall back to per-100g
    # values reported as-is with servingSizeGrams=100.
    def per_serving(name_100g: str, name_serving: str) -> Optional[float]:
        if (v := _f(nutri.get(name_serving))) is not None:
            return round(v, 2)
        per_100 = _f(nutri.get(name_100g))
        if serving_g is not None:
            return _per_serving(per_100, serving_g)
        return round(per_100, 2) if per_100 is not None else None

    serving_size_grams = serving_g if serving_g is not None else (
        100.0 if any(k.endswith("_100g") for k in nutri) else None
    )

    sodium_g = per_serving("sodium_100g", "sodium_serving")
    sodium_mg = round(sodium_g * 1000.0, 2) if sodium_g is not None else None

    name = product.get("name") or ""
    brand = product.get("brand") or ""
    db_display = f"{brand} {name}".strip() if brand else name

    return {
        "productName": db_display or None,
        "calories": per_serving("energy-kcal_100g", "energy-kcal_serving"),
        "servingSizeGrams": serving_size_grams,
        "proteinGrams": per_serving("proteins_100g", "proteins_serving"),
        "fiberGrams": per_serving("fiber_100g", "fiber_serving"),
        "addedSugarGrams": per_serving("added-sugars_100g", "added-sugars_serving"),
        "totalSugarGrams": per_serving("sugars_100g", "sugars_serving"),
        "totalFatGrams": per_serving("fat_100g", "fat_serving"),
        "saturatedFatGrams": per_serving("saturated-fat_100g", "saturated-fat_serving"),
        "ingredientList": _parse_ingredients(product.get("ingredients_text")),
        "readable": True,
        "confidence": "medium",
        "servings": None,
        "carbGrams": per_serving("carbohydrates_100g", "carbohydrates_serving"),
        "sodiumMg": sodium_mg,
        "isEstimated": False,
    }


# ---------------------------------------------------------------------------
# Backend call
# ---------------------------------------------------------------------------

class ScoreError(Exception):
    pass


def call_score(base_url: str, extracted: dict, purpose: str, title: str) -> dict:
    url = f"{base_url.rstrip('/')}/score"
    payload = {
        "extractedNutrition": extracted,
        "purpose": purpose or "snack",
        "title": title,
        "userGoal": None,
    }
    try:
        resp = requests.post(url, json=payload, timeout=SCORE_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        raise ScoreError(f"backend request failed: {e}") from e

    if resp.status_code >= 400:
        raise ScoreError(f"backend returned {resp.status_code}: {resp.text[:200]}")

    try:
        body = resp.json()
    except ValueError as e:
        raise ScoreError(f"backend returned non-JSON: {e}") from e

    if not isinstance(body, dict) or "score" not in body:
        raise ScoreError(f"malformed backend response: missing 'score' field")

    return body


# ---------------------------------------------------------------------------
# CSV value helpers
# ---------------------------------------------------------------------------

def clean_cell(value: Any) -> str:
    """Normalize a CSV cell to a stripped string. Treats None, NaN, and the
    literal string 'nan' as empty."""
    if value is None:
        return ""
    try:
        if isinstance(value, float) and value != value:  # NaN
            return ""
    except TypeError:
        pass
    s = str(value).strip()
    if s.lower() == "nan":
        return ""
    return s


# ---------------------------------------------------------------------------
# Hook generation (framing-aware, deterministic)
# ---------------------------------------------------------------------------

REQUIRED_NUTRITION_FIELDS = ["calories", "proteinGrams", "totalFatGrams", "carbGrams"]


def score_relation(score_a: int, score_b: int) -> str:
    """One of: 'tie', 'a_loses', 'a_barely_wins', 'a_wins'."""
    if score_a == score_b:
        return "tie"
    if score_a < score_b:
        return "a_loses"
    if score_a - score_b == 1:
        return "a_barely_wins"
    return "a_wins"  # diff >= 2


def resolve_names(row: dict) -> tuple[str, str, str, str]:
    """Return (a_display, b_display, a_label, b_label) using the fallback chain:
    display_name → name; label → display_name → name."""
    a_name = clean_cell(row.get("food_a_name"))
    b_name = clean_cell(row.get("food_b_name"))
    a_display = clean_cell(row.get("food_a_display_name")) or a_name
    b_display = clean_cell(row.get("food_b_display_name")) or b_name
    a_label = clean_cell(row.get("food_a_label")) or a_display
    b_label = clean_cell(row.get("food_b_label")) or b_display
    return a_display, b_display, a_label, b_label


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

VALID_FORMATS = ("comparison", "exposure", "swap", "ranking", "evaluation")


def resolve_format(row: dict) -> str:
    raw = clean_cell(row.get("format")).lower()
    if raw in VALID_FORMATS:
        return raw
    return "comparison"  # default for legacy rows


# ---------------------------------------------------------------------------
# Health-halo detection (used for postability + exposure framing)
# ---------------------------------------------------------------------------

_HALO_KEYWORDS = (
    "smoothie", "juice", "yogurt", "granola", "cereal", "protein bar",
    "energy bar", "fruit snack", "kombucha", "almond milk", "oat milk",
    "vitamin water", "fiber", "natural", "organic",
    "naked", "clif", "kind", "rxbar", "chobani", "yoplait", "activia",
    "special k", "honey nut", "cheerios",
)


def is_health_halo(row: dict) -> bool:
    framing = clean_cell(row.get("framing_type")).lower()
    if framing in ("health_halo", "kids_food", "sugar_shock"):
        return True
    text = " ".join([
        clean_cell(row.get("food_a_label")),
        clean_cell(row.get("food_a_display_name")),
        clean_cell(row.get("food_a_name")),
    ]).lower()
    return any(k in text for k in _HALO_KEYWORDS)


# ---------------------------------------------------------------------------
# Headline generation
#
# food_a is always the subject. food_b is a baseline reference. Headlines
# select one of four modes:
#
#   shock     — small score gap; "wait, these are the same?"
#   exposure  — food_a has a health halo; debunk the halo
#   dominance — large score gap; punchy, decisive
#   swap      — explicitly recommend food_b as the alternative
#
# Mode selection (food_a is the subject; halo is precomputed):
#
#   format == 'swap'                → 'swap'
#   format == 'exposure'            → 'exposure'
#   abs(diff) <= 1                  → 'shock'
#   halo                            → 'exposure'
#   abs(diff) >= 3                  → 'dominance'
#   else                            → 'shock'
#
# Templates are picked deterministically by pair_id mod template count,
# so the same pair always renders the same headline across runs.
# ---------------------------------------------------------------------------

# Hedging language the spec forbids in any headline. Validated at import.
FORBIDDEN_HEADLINE_WORDS = (
    "slightly", "somewhat", "edges", "a bit", "kind of",
    "not by much", "relatively",
)

HEADLINE_TEMPLATES: dict[str, list[str]] = {
    "shock": [
        "Wait… this is basically the same?",
        "You’re not making a better choice",
        "This isn’t actually healthier",
        "These are closer than you think",
        "Don’t kid yourself — this is the same",
        "Not the win you wanted",
        "{a} isn’t the upgrade you think",
    ],
    "exposure": [
        "Looks healthy. Isn’t.",
        "This isn’t as healthy as you think",
        "This ‘healthy’ snack is misleading",
        "Parents think this is healthy",
        "The ‘healthy’ option isn’t",
        "Healthy on the label. Not on the inside.",
        "Don’t fall for the health halo",
        "{a} isn’t the health food you think",
    ],
    # Dominance is split internally by direction so templates can be
    # decisive without contradicting the card visuals. The public mode
    # name is still 'dominance'.
    "dominance_a_wins": [
        "{a} is the better choice. Period.",
        "This isn’t even close!",
        "Pick {a}. It’s not subtle.",
        "The numbers don’t lie",
        "There’s no contest",
        "{a} wins. Hard.",
        "Big gap. Easy choice.",
    ],
    "dominance_a_loses": [
        "Stop choosing {a}!",
        "Skip {a}",
        "Don’t pick {a}",
        "{a} loses. Hard.",
        "Big gap — {a} is on the wrong side",
        "There’s no contest — and {a} loses",
        "{a} isn’t the choice",
    ],
    "swap": [
        "Stop eating this. Do this instead!",
        "Swap {a} for {b}",
        "This one change matters",
        "Eat {b}, not {a}",
        "Make this swap",
        "Trade up: {b} over {a}",
        "{b}, every time",
        "One swap, big difference",
    ],
}


def _validate_headline_templates() -> None:
    """Reject any template that contains a forbidden hedge word, and any
    template longer than 12 words. Runs once at import time."""
    for mode, templates in HEADLINE_TEMPLATES.items():
        for t in templates:
            lower = t.lower()
            for w in FORBIDDEN_HEADLINE_WORDS:
                if w in lower:
                    raise ValueError(
                        f"headline template '{t}' (mode={mode}) contains forbidden word '{w}'"
                    )
            # Word count cap. Treat punctuation as part of the adjacent word.
            word_count = len(t.split())
            if word_count > 12:
                raise ValueError(
                    f"headline template '{t}' (mode={mode}) exceeds 12 words ({word_count})"
                )
            # Punctuation rule: at most one '!' or one '?', never both.
            if t.count("!") + t.count("?") > 1:
                raise ValueError(
                    f"headline template '{t}' (mode={mode}) violates punctuation rule"
                )
            if "!" in t and "?" in t:
                raise ValueError(
                    f"headline template '{t}' (mode={mode}) mixes ! and ?"
                )


_validate_headline_templates()


def select_headline_mode(
    fmt: str,
    score_a: Optional[int],
    score_b: Optional[int],
    halo: bool,
) -> str:
    """Pick one of: 'shock', 'exposure', 'dominance', 'swap'.
    Mode is the public label; the dominance template is chosen by direction
    inside the headline generator."""
    if fmt == "swap":
        return "swap"
    if fmt == "exposure":
        return "exposure"
    if fmt == "evaluation":
        return "evaluation"
    if score_a is None or score_b is None:
        return "shock"  # safe fallback
    diff = abs(score_a - score_b)
    if diff <= 1:
        return "shock"
    if halo:
        return "exposure"
    if diff >= 3:
        return "dominance"
    return "shock"


def _det_index(pair_id: str, n: int) -> int:
    """Deterministic index from pair_id. Numeric pair_ids use the integer;
    non-numeric ones use a stable character-sum hash."""
    if n <= 0:
        return 0
    digits = re.sub(r"[^0-9]", "", str(pair_id))
    if digits:
        return int(digits) % n
    return sum(ord(c) for c in str(pair_id)) % n


def generate_headline_deterministic(
    row: dict,
    fmt: str,
    score_a: Optional[int],
    score_b: Optional[int],
    halo: bool,
) -> tuple[str, str]:
    """Return (headline, headlineMode). Empty headline + empty mode when
    the row has no scoreable food_a."""
    if score_a is None or fmt == "ranking":
        return "", ""

    mode = select_headline_mode(fmt, score_a, score_b, halo)
    a_display, _, a_label, b_label = resolve_names(row)
    pair_id = clean_cell(row.get("pair_id"))

    # Evaluation posts use the deterministic SEO title as the headline —
    # the prompt is keyed on it, the cards card don't render this format
    # (we run with --no-cards for evaluations), and there's no comparison
    # template that fits a single-food "Is X Healthy?" question.
    if mode == "evaluation":
        return _evaluation_title(a_display or a_label), mode

    if mode == "dominance":
        # food_b is always present in this branch (mode selection requires
        # a_score and b_score to compute the diff).
        templates = (
            HEADLINE_TEMPLATES["dominance_a_wins"]
            if (score_b is not None and score_a > score_b)
            else HEADLINE_TEMPLATES["dominance_a_loses"]
        )
    else:
        templates = HEADLINE_TEMPLATES[mode]

    template = templates[_det_index(pair_id, len(templates))]
    headline = template.format(a=a_label, b=b_label or a_label)
    return headline, mode


def generate_headline_with_openai(  # pragma: no cover - placeholder
    row: dict,
    fmt: str,
    score_a: Optional[int],
    score_b: Optional[int],
    halo: bool,
) -> tuple[str, str]:
    """TODO: wire to the OpenAI API once integration is approved.

    Should accept the same arguments as the deterministic generator and
    return (headline, headlineMode). Until enabled, callers fall back to
    `generate_headline_deterministic`. Do NOT call this from main() yet.
    """
    raise NotImplementedError(
        "OpenAI headline generation is not yet integrated; use "
        "generate_headline_deterministic for now."
    )


def generate_headline(
    row: dict,
    fmt: str,
    score_a: Optional[int],
    score_b: Optional[int],
    halo: bool,
) -> tuple[str, str]:
    """Top-level dispatcher. For now always uses the deterministic generator.
    Swap in OpenAI here once it's ready."""
    return generate_headline_deterministic(row, fmt, score_a, score_b, halo)


# ---------------------------------------------------------------------------
# Postability score (1–10) — gates output and rendering.
# ---------------------------------------------------------------------------

def postability_score(
    fmt: str,
    hook: str,
    halo: bool,
    score_a: Optional[int],
    score_b: Optional[int],
    relation: str,
) -> int:
    if score_a is None:
        return 1

    score = 5  # neutral baseline

    if fmt == "comparison":
        if halo and score_a <= 4:
            score += 3
        elif halo and score_a <= 6:
            score += 2
        elif halo:
            score += 1
        if relation in ("a_loses", "tie"):
            score += 1
        if score_a <= 3:
            score += 1
    elif fmt == "exposure":
        if halo and score_a <= 5:
            score += 3
        elif halo:
            score += 1
        if score_a <= 3:
            score += 1
    elif fmt == "evaluation":
        # Single-food evaluation posts target search queries directly, so
        # they don't need a comparison-driven hook to clear the postability
        # gate. Bonus when the score reveals a clear angle (very low or very
        # high), or when there's a halo to debunk.
        if halo:
            score += 2
        if score_a is not None and (score_a <= 3 or score_a >= 8):
            score += 2
        elif score_a is not None and (score_a <= 5 or score_a >= 7):
            score += 1
        # Floor: every evaluation that has a real score is worth posting.
        score = max(score, 7)
    elif fmt == "swap":
        if score_b is not None:
            diff = score_b - score_a
            if diff >= 3:
                score += 3
            elif diff >= 2:
                score += 2
            elif diff >= 1:
                score += 1
        if halo and score_a <= 5:
            score += 1

    # Clarity: shorter hooks read sharper.
    n = len((hook or "").split())
    if 0 < n <= 8:
        score += 1
    elif n > 14:
        score -= 1

    return max(1, min(10, score))


# ---------------------------------------------------------------------------
# Verdict (one sentence, deterministic, for blog inputs)
# ---------------------------------------------------------------------------

def verdict_sentence(
    food_a_display: str,
    score_a: Optional[int],
    helps: list[str],
    hurts: list[str],
) -> str:
    name = food_a_display or "This product"
    if score_a is None:
        return ""
    if score_a <= 3:
        h = hurts[:2] if hurts else ["it's heavily processed"]
        first = h[0].rstrip(".").lower()
        tail = f" and {h[1].rstrip('.').lower()}" if len(h) > 1 else ""
        return f"Avoid {name} — {first}{tail}."
    if score_a <= 5:
        top = (hurts[:1] or ["several nutritional concerns"])[0].rstrip(".").lower()
        return f"{name} sits at {score_a}/10 — {top}."
    if score_a <= 7:
        top = (helps[:1] or ["a modest nutritional profile"])[0].rstrip(".").lower()
        return f"{name} scores {score_a}/10, helped by {top}."
    top = (helps[:1] or ["genuinely good nutrition"])[0].rstrip(".").lower()
    return f"{name} is a solid {score_a}/10 — {top}."


def content_priority(framing: str, relation: str) -> int:
    """Higher = more interesting content. Deterministic, framing-aware."""
    framing = (framing or "").lower()

    # Decisive A-wins are flat content regardless of framing.
    if relation == "a_wins":
        return 40

    if framing == "health_halo":
        if relation == "a_loses":
            return 100
        if relation == "tie":
            return 95
        if relation == "a_barely_wins":
            return 85

    if framing in ("kids_food", "sugar_shock"):
        if relation in ("a_loses", "tie"):
            return 90
        if relation == "a_barely_wins":
            return 60

    # default framing
    if relation == "tie":
        return 70
    if relation == "a_barely_wins":
        return 60
    if relation == "a_loses":
        return 60
    return 50


# ---------------------------------------------------------------------------
# Per-food pipeline
# ---------------------------------------------------------------------------

def score_food(
    conn: sqlite3.Connection,
    backend_base_url: str,
    csv_name: str,
    barcode: str,
    purpose: str,
) -> tuple[Optional[int], str, list[str], list[str], Optional[str]]:
    """Return (score, interpretation, what_helps, what_hurts, error_message)."""
    barcode = clean_cell(barcode)
    if not barcode:
        return None, "", [], [], f"missing barcode for {csv_name or '(unnamed)'}"

    product = lookup_product(conn, barcode)
    if product is None:
        return None, "", [], [], f"barcode {barcode} not found in DB for {csv_name or '(unnamed)'}"

    extracted = build_extracted_nutrition(product)

    missing = [k for k in REQUIRED_NUTRITION_FIELDS if extracted.get(k) is None]
    if missing:
        return None, "", [], [], (
            f"missing nutrition fields for {csv_name or '(unnamed)'}: "
            + ", ".join(missing)
        )

    try:
        result = call_score(backend_base_url, extracted, purpose, csv_name)
    except ScoreError as e:
        return None, "", [], [], f"backend error for {csv_name or '(unnamed)'}: {e}"

    score = result.get("score")
    interpretation = result.get("interpretation") or ""

    def _list(key: str) -> list[str]:
        v = result.get(key) or []
        if not isinstance(v, list):
            return []
        return [str(s).strip() for s in v if str(s).strip()]

    helps = _list("whatHelps")
    hurts = _list("whatHurts")

    if score is None:
        return None, interpretation, helps, hurts, f"backend returned null score for {csv_name or '(unnamed)'}"
    try:
        score = int(score)
    except (TypeError, ValueError):
        return None, interpretation, helps, hurts, f"backend score not an int for {csv_name or '(unnamed)'}: {score!r}"

    return score, interpretation, helps, hurts, None


# ---------------------------------------------------------------------------
# Blog post generation (OpenAI)
#
# Reads each output/blog_inputs/*.json (already produced earlier in main())
# and writes a markdown post to output/blog_posts/. The model gets ONLY the
# JSON we built — it must not invent nutrition facts or medical claims.
# Failures are isolated per file: one bad call can't break the run.
# ---------------------------------------------------------------------------

DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

_BLOG_SYSTEM_PROMPT = """You write food-comparison blog posts for SmarterEats.

Hard rules:
- Use ONLY the facts present in the JSON the user provides. Do not invent calories, sugar grams, ingredients, or any other nutritional detail.
- Do not make medical claims, diagnose, or prescribe.
- Stay food-focused and consumer-friendly.
- Tone: clear, decisive, practical. Short sentences. No fluff. No long intros. Avoid "it depends" unless the data really demands it.
- 350-500 words total. Markdown only.
- Do NOT wrap your output in fences (no ```markdown). Output raw markdown.
- Do NOT add any commentary before or after the post.
- Every sentence must be specific to the provided food data or the reader’s practical decision. Avoid generic nutrition advice unless it is tied directly to a score, ingredient, macro, processing concern, or comparison from the JSON.

Editorial stance — depends on the JSON's `format` field:

- format == "comparison": food_a is ALWAYS the subject of the post. food_b is ONLY a baseline reference — a yardstick for exposing food_a. Do NOT recommend food_b. Do NOT describe food_b as "the better choice," do NOT suggest readers pick it instead, do NOT frame the post as "which should I choose?". The post answers ONE question: what does this reveal about food_a?
- format == "swap": food_b IS the explicit alternative — recommend it plainly. food_a is what the reader should swap OUT.
- format == "exposure": food_a only (food_b is null). Surface the gap between how food_a is marketed and what's actually in it.

Forbidden closers in any format: "both are fine," "everything in moderation," "it depends on your goals," "context matters," "can be part of a balanced diet," or any similar punt that avoids taking a stance. Take a stance.

Required structure:

YAML frontmatter at the very top — exactly these three fields, no others (no category, no tags, no slug, no image, no draft):
---
title: "{the title is provided in the user message — use it verbatim, including the parenthetical comparison if present}"
description: "{1-sentence SEO blurb, ≤160 chars, no quotes inside}"
date: "{provided date YYYY-MM-DD}"
---

Description rules:
- Exactly one sentence.
- Must include the phrase "is {food_a_display_name} healthy" or a close natural variant (e.g. "Is Gatorade actually healthy?").
- Slightly curiosity-driven — hint at a surprising or counterintuitive answer.
- If food_b is present, hint at the comparison (e.g. "...this comparison with soda tells a different story").

Then, the body H1 — use the EXACT same title string as the frontmatter:
# {title verbatim}

## Quick Answer
2-3 concise sentences. Directly answer whether food_a is healthy and how it compares to food_b. No intro paragraph, no fluff — start with the verdict.

## Quick Verdict
- {food_a_display_name}: X/10
- {food_b_display_name}: Y/10

Winner: {food_a_display_name | food_b_display_name | neither} — one short sentence explaining why.

Use the score values from the JSON's `food_a.score` and `food_b.score` VERBATIM. Do NOT invent scores. If a food has no score in the JSON, write "(no score)" instead of guessing. Omit the food_b bullet entirely when food_b is null (exposure format) and replace the Winner line with a single short verdict sentence on food_a.

## Why {food_a_display_name} Falls Short
3-4 concise bullets, each focused on a nutrition tradeoff: sugar, calories, satiety, protein, fiber, ingredients, processing, sodium, or calorie density. Use food_a.what_hurts and food_a_bullets verbatim where they fit. At least ONE bullet must surface a concrete number pulled from the JSON (e.g. "29.99g of sugar", "180 calories", "85 mg sodium") — never invented.

## How {food_b_display_name} Compares
2-3 concise bullets explaining how food_b is better, worse, or similar on the same dimensions. For comparison/exposure: keep food_b as a yardstick, NOT a recommendation. For swap: name food_b as the recommended alternative and say why.

If food_b is null (exposure format), replace this entire section with `## What's Hidden in {food_a_display_name}` — 2-3 bullets surfacing the gap between food_a's marketing claim and its actual numbers.

## Best Choice Based on Your Goal
Use these three rows verbatim, each followed by one short verdict (a phrase or one short sentence). For comparison/exposure, the verdict can be "neither — pick a real alternative" or similar:
- **Weight loss:** {short verdict}
- **Energy / satiety:** {short verdict}
- **Occasional treat:** {short verdict}

## Better Alternatives
3-5 bullets, each a real, recognizable food (plain Greek yogurt, whole orange, plain oatmeal, hard-boiled egg, unsweetened sparkling water, etc.) with a one-line reason. No invented brands, no gimmicks.

After the Better Alternatives bullets, on its own paragraph (NOT a heading, NOT a bullet, NOT a blockquote), output this sentence VERBATIM:

Want a faster way to find better swaps? SmarterEats lets you compare foods and discover healthier options instantly.

## Bottom Line
2-3 concise sentences summarizing the practical decision. Take a clear stance about food_a. Do NOT recommend food_b unless format == "swap". Do NOT use any forbidden closer. Do not introduce generic advice in the Bottom Line. Restate the practical decision using the score, the strongest concern, and the best alternative direction.

Do NOT generate a "## Related Comparisons" or "## Related" section yourself. That section is appended programmatically after your output from a curated list of already-published posts. Stop after the Bottom Line section.
"""


_EVALUATION_SYSTEM_PROMPT = """You write single-food evaluation blog posts for SmarterEats — answers to consumer queries like "Is popcorn healthy?" or "Are protein bars healthy?".

Hard rules:
- Use ONLY the facts present in the JSON the user provides. Do not invent calories, sugar grams, ingredients, sodium, or any other nutritional detail.
- Do not make medical claims, diagnose, or prescribe.
- Stay food-focused and consumer-friendly.
- Tone: clear, decisive, practical. Short sentences. Concrete reasoning. No long intros.
- 400-600 words total. Markdown only.
- Do NOT wrap your output in fences (no ```markdown). Output raw markdown.
- Do NOT add any commentary before or after the post.

Editorial stance:
- The post answers ONE query: "Is/Are {food} healthy?" — a question the reader is searching for.
- Frame healthiness as goal-dependent, NOT absolute. Whether the food is healthy depends on the eater's goal, portion size, and what they're comparing it to.
- Be nuanced — a flat "yes" or "no" is the wrong answer. Lay out tradeoffs explicitly.
- Tie back to SmarterEats' loop: set a goal → track against it → make better food decisions.
- Keep comparative framing even though there's only one food: "Compared to chips...", "Higher in protein than...", "More filling than..." (use real categories, not invented brands). This boosts retrieval depth and gives the AI surface for related links later.

Forbidden phrases: "everything in moderation," "balanced diet," "context matters," "can be part of a healthy lifestyle," "not necessarily bad," "in a balanced diet." Use "it depends" only when paired with concrete reasoning, never as a punt.

Each major section must stand on its own — assume an AI search engine may surface a single section in isolation. That means:
- Reference the food by name in every section (don't lean on prior context).
- Give concrete reasoning (numbers from the JSON, named tradeoffs).
- No section should read as filler.

Required structure:

YAML frontmatter at the very top — exactly these three fields, no others:
---
title: "{the title is provided in the user message — use it verbatim, including the question mark}"
description: "{1-sentence SEO blurb, ≤160 chars, no quotes inside}"
date: "{provided date YYYY-MM-DD}"
---

Title grammar — Is vs. Are:
The frontmatter `title:` and body H1 are provided in the user message and MUST be used verbatim — do not "correct" the verb. The title was constructed deterministically using these rules so it stays in sync with the URL slug and the rest of the post:

- The verb agrees with the SUBJECT'S HEAD NOUN, not surrounding modifiers.
- "Are" when the head is plural: brands like Doritos, Skittles, Cheerios, Pringles, Pop-Tarts, Triscuits, Pop Corners, Frosted Flakes; common-noun heads like Bars, Crackers, Crisps, Cookies, Gummies, Squares, Berries, Clusters, Nuts, Chips, Snacks, Straws, Seeds, Almonds.
- "Is" when the head is singular or uncountable: brands ending in -s that are still grammatically singular (Snickers, Gogurt, Fruifuls, Reese's, M&M's, Rice Krispies); singular product types where a plural word is just a modifier ("Cheerios Protein Bar" → head "Bar"; "Quaker Granola Bar S'mores" → head "Bar"); uncountable substances ("Oatmeal", "Cereal", "Yogurt").

The description's opening verb (when it begins with "Is/Are") MUST match the title's verb. Frontmatter `title:`, the H1 `# `, and the description's leading verb all agree.

Description rules:
- Exactly one sentence.
- Must include the exact phrase "Is {food_a_display_name} healthy" or "Are {food_a_display_name} healthy" — the verb must match the title's verb verbatim.
- Goal-aware: hint at when the food works (or doesn't) for a typical goal — weight loss, satiety, energy, protein.
- Slightly curiosity-driven, never a punt.
- Example: "Is popcorn healthy? Learn when popcorn is a smart snack, when it can work against your goals, and how it compares to other common snacks."

Body H1 — verbatim title:
# {title verbatim}

## Quick Answer
2-3 sentences. Lead with a direct, natural-language answer using the food's name verbatim. Pattern: "{Food} can be a relatively healthy [snack/option] depending on…" or "{Food} is a mixed bag — fine for X, not for Y." No filler intro.

## Nutrition Snapshot
A short paragraph (1-2 sentences) framing the food's profile, followed by a bullet list of headline numbers pulled VERBATIM from the JSON: calories per serving, protein g, fiber g, sugar g, sodium mg (only include what's present). Include the food's score as "Overall score: X/10". Do NOT invent numbers — if a value is missing from the JSON, omit that bullet.

## What Makes {food_a_display_name} a Good Choice
2-4 bullets grounded in food_a.what_helps and food_a_bullets where they fit. Be concrete: protein, fiber, satiety, calorie density, processing level, ingredient quality. If the score is ≤4/10, keep this section short and honest — don't manufacture positives.

## Potential Downsides
2-4 bullets grounded in food_a.what_hurts and food_a_bullets where they fit. At least ONE bullet must surface a concrete number from the JSON ("29.99g of sugar", "180 calories", "85 mg sodium"). Be specific about what makes the downside material — a sugar/sodium/processing concern, not a vague "could be better".

## How {food_a_display_name} Fits Different Goals
Use these four rows verbatim, each followed by 1-2 sentences of practical guidance grounded in the food's actual numbers. Use language like "Good fit if…", "Less ideal if…", "Watch out for…":
- **Weight loss / calorie control:** {goal-specific verdict}
- **High protein / muscle support:** {goal-specific verdict}
- **Energy / satiety / blood sugar stability:** {goal-specific verdict}
- **Heart health / lower sodium / less processed eating:** {goal-specific verdict}

Each verdict must reference at least one concrete attribute (a number from the JSON, or a category like "ultra-processed", "low fiber"). Avoid generic claims.

## Healthier or Better Alternatives
3-5 bullets, each a real, recognizable food (plain Greek yogurt, whole orange, plain oatmeal, hard-boiled egg, unsweetened sparkling water, etc.) with a one-line reason that compares back to {food_a_display_name}: "Higher in protein", "Less added sugar", "More filling per calorie", "Less processed". At least 2 bullets MUST include an explicit comparative phrase that names {food_a_display_name}. No invented brands, no gimmicks.

After the alternatives bullets, on its own paragraph (NOT a heading, NOT a bullet, NOT a blockquote), output this sentence VERBATIM:

Want a faster way to find better swaps? SmarterEats lets you compare foods and discover healthier options instantly.

## Final Verdict
2-3 sentences. End with the goal-dependent framing — explicitly include a sentence like: "Whether {food_a_display_name} is a good choice depends on your goal, portion size, and what you're comparing it to." Then a clear one-line stance: who should keep it in their rotation, who should be cautious. Take a stance — never punt.

Do NOT generate a "## Related Comparisons" or "## Related" section yourself. That section is appended programmatically after your output from a curated list of already-published posts. Stop after the Final Verdict section.
"""


def _slugify_for_blog(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:max_len].rstrip("-") or "post"


def _smart_title_case_word(w: str) -> str:
    """Capitalize the first char and any char after a hyphen. Preserves
    apostrophes/ampersands and existing inner caps (e.g. 'M&M's' stays
    'M&M's', 'coca-cola' becomes 'Coca-Cola')."""
    if not w:
        return w
    out = [w[0].upper()]
    for i in range(1, len(w)):
        if w[i - 1] == "-" and w[i].isalpha():
            out.append(w[i].upper())
        else:
            out.append(w[i])
    return "".join(out)


def _smart_title_case(s: str) -> str:
    return " ".join(_smart_title_case_word(w) for w in (s or "").split())


def _seo_title(food_a_display: str, food_b_display: str) -> str:
    """SEO title: 'Is X Healthy?' with an optional '(X vs Y)' tail when a
    distinct food_b is provided."""
    a = (food_a_display or "").strip() or "This Product"
    a_tc = _smart_title_case(a)
    b = (food_b_display or "").strip()
    if b and b.lower() != a.lower():
        return f"Is {a_tc} Healthy? ({a_tc} vs {_smart_title_case(b)})"
    return f"Is {a_tc} Healthy?"


# Plural-form common-noun heads. When the LAST word of the food name is one
# of these, the title takes "Are X Healthy?".
_PLURAL_FOOD_ENDINGS: frozenset[str] = frozenset({
    "almonds", "bars", "berries", "cubes", "chips", "clusters", "cookies",
    "crackers", "crisps", "flakes", "fries", "gummies", "lentils",
    "nuggets", "nuts", "olives", "oats", "pickles", "pops", "pretzels",
    "puffs", "raisins", "rolls", "seeds", "snacks", "squares", "sticks",
    "straws", "strips", "tomatoes", "veggies", "wedges", "wraps",
    "grapes", "greens", "beans",
})

# Brands that are grammatically plural even though they're proper nouns —
# always take "Are X Healthy?". Match against the FULL food name in lower
# case (so "Cheerios" matches but "Cheerios Protein Bar" doesn't, since
# its head noun is "bar").
_PLURAL_TREATED_BRANDS: frozenset[str] = frozenset({
    "cheerios", "cheez-its", "cheez its", "doritos", "skittles", "pringles",
    "pop-tarts", "pop tarts", "pop corners", "popcorners", "triscuits",
    "frosted flakes", "rxbar minis", "welch's fruit snacks",
})

# Brands whose name ends in -s but is grammatically singular — always take
# "Is X Healthy?", even though English would normally treat the trailing -s
# as plural. Match against the FULL food name in lower case.
_SINGULAR_BRANDS_ENDING_S: frozenset[str] = frozenset({
    "snickers", "gogurt", "fruifuls", "reese's", "reeses", "m&m's", "m&ms",
    "rice krispies",  # "Krispies" is plural-form but the brand reads singular
})


def _is_plural_food_name(name: str) -> bool:
    """Decide between 'Is' and 'Are' for evaluation titles. The verb agrees
    with the head noun (last meaningful word), NOT with surrounding
    modifiers. Decision order:

    1. Whole name matches a known plural-treated brand → 'Are'.
    2. Whole name matches a known singular brand ending in -s → 'Is'.
    3. Last word is a plural-form common noun → 'Are'.
    4. Default → 'Is' (safe fallback for mass nouns and most singular brands).

    Examples:
    - 'Doritos'                       → True  ('Are Doritos Healthy?')
    - 'Snickers'                      → False ('Is Snickers Healthy?')
    - 'Welch\\'s Fruit Snacks'         → True  (head 'snacks' is plural)
    - 'Cheerios Protein Bar'          → False (head 'bar' is singular;
                                              'Cheerios' is a modifier)
    - 'Kashi Honey Oat Flax Bars'     → True  (head 'bars' is plural)
    - 'Quaker Maple & Brown Sugar Instant Oatmeal' → False (mass noun)"""
    if not name:
        return False
    s = name.strip().lower()
    if not s:
        return False
    if s in _PLURAL_TREATED_BRANDS:
        return True
    if s in _SINGULAR_BRANDS_ENDING_S:
        return False
    parts = s.split()
    if not parts:
        return False

    # 'X with Xs' / 'X & Xs' / 'X and Xs' pattern — when the trailing plural
    # word is just an echo of the singular head ('Almond With Almonds'),
    # the singular form is the actual head and the title takes 'Is'. We
    # walk through every connector occurrence so this works even when the
    # 'with X' clause isn't right at the end.
    for connector in ("with", "and", "&"):
        for i, part in enumerate(parts):
            if part != connector or i == 0:
                continue
            pre = parts[i - 1]
            post_last = parts[-1]
            if pre and post_last == pre + "s":
                return pre in _PLURAL_FOOD_ENDINGS

    return parts[-1] in _PLURAL_FOOD_ENDINGS


def _evaluation_title(food_display: str) -> str:
    """SEO title for single-food evaluation posts: 'Is X Healthy?' for
    singular / mass nouns and brand names, 'Are X Healthy?' for plural
    food categories (chips, bars, cookies, etc.)."""
    a = (food_display or "").strip() or "This Food"
    a_tc = _smart_title_case(a)
    verb = "Are" if _is_plural_food_name(a) else "Is"
    return f"{verb} {a_tc} Healthy?"


def _evaluation_slug(food_display: str) -> str:
    """URL slug for single-food evaluation posts. Mirrors the comparison
    slug pattern so /blog/<slug> routing stays uniform.
    e.g. 'is-popcorn-healthy', 'are-protein-bars-healthy'."""
    a = (food_display or "").strip() or "this food"
    verb = "are" if _is_plural_food_name(a) else "is"
    return _slugify_for_blog(f"{verb} {a} healthy")


def _seo_slug(food_a_display: str, food_b_display: str) -> str:
    """URL slug. Comparison/swap include food_b so two posts comparing the
    same food_a against different food_b's get distinct filenames; exposure
    posts get the short 'is-X-healthy' form."""
    a = (food_a_display or "").strip() or "this product"
    b = (food_b_display or "").strip()
    if b and b.lower() != a.lower():
        return _slugify_for_blog(f"is {a} healthy vs {b}")
    return _slugify_for_blog(f"is {a} healthy")


def load_published_blog_index(website_blog_dir: Optional[str]) -> list[str]:
    """Return sorted slugs of `.mdx` files already published to the
    website's content/blog/ directory. Used as the candidate pool for the
    Related Comparisons section — referenced slugs always resolve at
    build time, never 404. The site renders the link text from each
    destination's current title, so we only need slugs here.

    Returns [] — and prints a warning — when the env var is unset, the
    path is missing, or the path isn't a directory. Never raises."""
    if not website_blog_dir:
        print("WARNING: WEBSITE_BLOG_DIR is not set; staged posts will be "
              "written without a Related Comparisons section.", file=sys.stderr)
        return []
    p = Path(website_blog_dir)
    if not p.exists() or not p.is_dir():
        print(f"WARNING: WEBSITE_BLOG_DIR={website_blog_dir!r} is not a "
              "directory; staged posts will be written without a Related "
              "Comparisons section.", file=sys.stderr)
        return []
    return sorted(mdx.stem for mdx in p.glob("*.mdx"))


def _pick_related_slugs(
    published_slugs: list[str],
    current_slug: str,
    rotation_idx: int,
    n: int = 3,
) -> list[str]:
    """Return up to `n` slugs from `published_slugs`, excluding
    `current_slug`. Rotates by `rotation_idx` so each post cites a
    different slice. Returns [] when fewer than 2 other published posts
    exist (per spec)."""
    candidates = [s for s in published_slugs if s != current_slug]
    if len(candidates) < 2:
        return []
    total = len(candidates)
    start = rotation_idx % total
    return [candidates[(start + i) % total] for i in range(min(n, total))]


def generate_blog_post_with_openai(blog_input: dict, title: str, model: str) -> str:
    """Call the OpenAI chat-completions API to turn one structured blog
    input into a markdown post. Raises on API errors — the caller logs
    and continues. `title` is computed deterministically by the caller
    and must appear verbatim in both the frontmatter and the body H1.

    The system prompt is selected by the JSON's `format` field:
    'evaluation' → single-food 'Is X Healthy?' template; everything else
    (comparison / swap / exposure) → the multi-food template."""
    from openai import OpenAI  # local import so missing dep is handled gracefully

    client = OpenAI()  # picks up OPENAI_API_KEY from env
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    fmt = (blog_input.get("format") or "comparison").strip()
    is_evaluation = fmt == "evaluation"
    system_prompt = _EVALUATION_SYSTEM_PROMPT if is_evaluation else _BLOG_SYSTEM_PROMPT
    intro = (
        "Write the SmarterEats single-food evaluation post."
        if is_evaluation
        else "Write the SmarterEats blog post for this comparison."
    )

    payload = {**blog_input, "date": today, "title": title}
    user_msg = (
        f"{intro}\n\n"
        f"Use this exact title in BOTH the frontmatter `title:` and the body H1 `#`:\n"
        f"  {title}\n\n"
        "Use only the facts in the JSON below. Output markdown only.\n\n"
        f"```json\n{json.dumps(payload, indent=2, ensure_ascii=False)}\n```"
    )

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.7,
    )
    content = resp.choices[0].message.content or ""
    return content.strip()


def write_blog_posts_from_inputs(
    blog_input_dir: str = "output/blog_inputs",
    output_dir: str = "output/blog_posts",
    force: bool = False,
) -> int:
    """For every JSON in `blog_input_dir`, call OpenAI and write a `.mdx`
    post into `output_dir`. Returns the count of successful writes.

    Output filenames are `<slug>.mdx` (slug derived from headline) so the
    staged file can be copied directly into the website's content/blog/
    directory — the slug is also the URL.

    No-ops gracefully when OPENAI_API_KEY is unset, the input directory is
    missing, or the openai package isn't installed. One call failure does
    not stop the rest of the batch.

    When `force=False` (default), an input whose `<slug>.mdx` already exists
    is skipped — re-runs don't re-spend OpenAI tokens on posts already
    generated.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("WARNING: OPENAI_API_KEY not set; skipping blog post generation.",
              file=sys.stderr)
        return 0

    in_dir = Path(blog_input_dir)
    if not in_dir.exists():
        print(f"WARNING: {blog_input_dir} does not exist; nothing to generate.",
              file=sys.stderr)
        return 0

    inputs = sorted(in_dir.glob("*.json"))
    if not inputs:
        print(f"No blog input JSON files in {blog_input_dir}.")
        return 0

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    model = os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    print(f"\nGenerating blog posts via OpenAI (model={model}) "
          f"for {len(inputs)} input(s)…")

    # Phase 1: load all inputs and pre-compute titles + paths so the
    # related-links logic can pull from the full set in this run.
    plan: list[tuple[Path, str, Path, dict]] = []
    for json_path in inputs:
        try:
            with open(json_path) as f:
                blog_input = json.load(f)
        except Exception as e:
            print(f"  WARNING: could not load {json_path.name}: {e}", file=sys.stderr)
            continue
        food_a_disp = ((blog_input.get("food_a") or {}).get("display_name") or "").strip()
        food_b_disp = ((blog_input.get("food_b") or {}).get("display_name") or "").strip() \
            if blog_input.get("food_b") else ""
        # Evaluation posts (single food) use the 'Is/Are X Healthy?' shape;
        # everything else (comparison/swap/exposure) uses the comparison form.
        if (blog_input.get("format") or "") == "evaluation":
            title = _evaluation_title(food_a_disp)
            slug = _evaluation_slug(food_a_disp)
        else:
            title = _seo_title(food_a_disp, food_b_disp)
            slug = _seo_slug(food_a_disp, food_b_disp)
        out_path = out_dir / f"{slug}.mdx"
        plan.append((json_path, title, out_path, blog_input))

    # Related-section candidates come exclusively from the live website's
    # content/blog/ directory so referenced slugs always resolve at build
    # time. When WEBSITE_BLOG_DIR is unset/invalid, the list is empty and
    # the Related Comparisons section is skipped entirely.
    published_slugs = load_published_blog_index(os.environ.get("WEBSITE_BLOG_DIR"))

    # Phase 2: generate, skipping ones that already exist unless forced.
    written = 0
    skipped = 0
    for idx, (json_path, title, out_path, blog_input) in enumerate(plan):
        if not force and out_path.exists():
            skipped += 1
            continue

        try:
            markdown = generate_blog_post_with_openai(blog_input, title, model)
        except Exception as e:
            print(f"  WARNING: OpenAI call failed for {json_path.name}: {e}",
                  file=sys.stderr)
            continue

        if not markdown.strip():
            print(f"  WARNING: empty response for {json_path.name}; skipping.",
                  file=sys.stderr)
            continue

        # Related Comparisons: pick up to 3 published slugs from
        # WEBSITE_BLOG_DIR (excluding the post we're generating) and emit a
        # single <RelatedPosts /> JSX component. The site renders link text
        # from each destination's current title at build time, so titles
        # never go stale here. Empty pool (no env var / missing dir / <2
        # other posts) → section is skipped entirely.
        related_slugs = _pick_related_slugs(
            published_slugs, out_path.stem, idx, n=3,
        )
        if related_slugs:
            slugs_attr = ",".join(related_slugs)
            markdown = (
                markdown.rstrip()
                + "\n\n## Related Comparisons\n\n"
                + f'<RelatedPosts slugs="{slugs_attr}" />\n'
            )

        try:
            out_path.write_text(markdown, encoding="utf-8")
            written += 1
            print(f"  ✓ {out_path.name}")
        except Exception as e:
            print(f"  WARNING: failed to write {out_path.name}: {e}", file=sys.stderr)

    if skipped:
        print(f"Skipped {skipped} blog post(s) already present in {output_dir}/ "
              f"(use --force to regenerate)")
    print(f"Wrote {written} blog post(s) to {output_dir}/")
    return written


# ---------------------------------------------------------------------------
# Result cache (skip already-processed rows on re-runs)
# ---------------------------------------------------------------------------

# Fields whose values, taken together, determine a result. If any of these
# differ between input.csv and the cached output, treat it as a cache miss
# and re-score so edits to a row aren't silently ignored.
_CACHE_KEY_FIELDS = (
    "pair_id", "food_a_barcode", "food_b_barcode",
    "framing_type", "format", "purpose",
)


def _result_cache_key(rec: dict) -> tuple:
    return tuple(clean_cell(rec.get(k)) for k in _CACHE_KEY_FIELDS)


def _coerce_cached_record(rec: dict) -> dict:
    """pd.read_csv with dtype=str returns everything as string. Restore None
    / int for numeric fields so downstream code (blog_inputs, summary)
    works on cached rows exactly like a freshly computed dict."""
    out = dict(rec)
    int_fields = ("food_a_score", "food_b_score", "score_diff",
                  "content_priority", "postability_score")
    for k in int_fields:
        v = out.get(k, "")
        if v in ("", "None", None):
            out[k] = None
            continue
        try:
            out[k] = int(float(v))
        except (ValueError, TypeError):
            pass
    return out


def load_completed_cache(path: str) -> dict[tuple, dict]:
    """Build {key → record} from a prior output/results.csv. Error rows are
    excluded — those live in the sidecar `result_errors.csv` so they're
    purged from results.csv but still suppress retries."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        df = pd.read_csv(p, dtype=str, keep_default_na=False)
    except Exception as e:
        print(f"WARN: could not read {path} for caching: {e}", file=sys.stderr)
        return {}
    cache: dict[tuple, dict] = {}
    for _, r in df.iterrows():
        rec = r.to_dict()
        if (rec.get("status") or "").strip() == "error":
            continue
        cache[_result_cache_key(rec)] = _coerce_cached_record(rec)
    return cache


# ---------------------------------------------------------------------------
# Error sidecar: keeps results.csv free of error rows while still preventing
# retries on the next run. An entry stays in the sidecar as long as its
# key fields (pair_id + barcodes + framing + format + purpose) appear in
# input.csv. Edit any of those → cache miss → row is re-scored.
# ---------------------------------------------------------------------------

RESULT_ERRORS_CSV_DEFAULT = "output/result_errors.csv"

ERROR_CACHE_COLUMNS = [
    "pair_id", "food_a_barcode", "food_b_barcode",
    "framing_type", "format", "purpose",
    "food_a_name", "food_b_name",
    "error_message", "errored_at",
]


def load_error_cache(path: str) -> dict[tuple, dict]:
    """Read prior error rows from the sidecar. Returns {key → record}."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        df = pd.read_csv(p, dtype=str, keep_default_na=False)
    except Exception as e:
        print(f"WARN: could not read {path}: {e}", file=sys.stderr)
        return {}
    cache: dict[tuple, dict] = {}
    for _, r in df.iterrows():
        rec = r.to_dict()
        rec["status"] = "error"
        cache[_result_cache_key(rec)] = rec
    return cache


def write_error_cache(path: str, records) -> None:
    """Overwrite the sidecar with the given records (one row per record).
    Atomically writes via a tmp file."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    import csv as _csv
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=ERROR_CACHE_COLUMNS)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k, "") for k in ERROR_CACHE_COLUMNS})
    os.replace(tmp, path)


def migrate_errors_from_results(results_path: str, sidecar_path: str) -> int:
    """One-shot: if the sidecar doesn't exist yet, copy any status='error'
    rows out of an existing results.csv into it. Returns count migrated.
    No-op when the sidecar is already present."""
    if Path(sidecar_path).exists():
        return 0
    if not Path(results_path).exists():
        return 0
    try:
        df = pd.read_csv(results_path, dtype=str, keep_default_na=False)
    except Exception:
        return 0
    err_rows = df[df.get("status", "") == "error"] if "status" in df.columns else df.iloc[0:0]
    if err_rows.empty:
        return 0
    records = []
    for _, r in err_rows.iterrows():
        records.append({
            "pair_id": r.get("pair_id", ""),
            "food_a_barcode": r.get("food_a_barcode", ""),
            "food_b_barcode": r.get("food_b_barcode", ""),
            "framing_type": r.get("framing_type", ""),
            "format": r.get("format", ""),
            "purpose": r.get("purpose", ""),
            "food_a_name": r.get("food_a_name", ""),
            "food_b_name": r.get("food_b_name", ""),
            "error_message": r.get("error_message", ""),
            "errored_at": "",  # unknown — migrated from a prior run
        })
    write_error_cache(sidecar_path, records)
    return len(records)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Score input.csv pairs and emit results / cards / blog inputs.",
    )
    p.add_argument("--force", action="store_true",
                   help="re-score every input row and regenerate every blog "
                        "post, even ones already produced on a prior run")
    p.add_argument("--blog-only", action="store_true",
                   help="skip scoring and card rendering; only (re)generate "
                        "blog posts from existing output/blog_inputs/. "
                        "Combine with --force to refresh existing posts.")
    p.add_argument("--no-cards", action="store_true",
                   help="skip PNG card rendering. Scoring and blog-post "
                        "generation still run normally — useful when you "
                        "only care about new .mdx files.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    # Blog-only path: skip scoring, sidecar, results.csv writing, and card
    # rendering. Just (re)generate .mdx posts from the existing
    # output/blog_inputs/ JSONs. No backend or DB needed.
    if args.blog_only:
        try:
            write_blog_posts_from_inputs(
                "output/blog_inputs", "output/blog_posts", force=args.force,
            )
        except Exception as e:
            print(f"WARNING: blog post generation failed: {e}", file=sys.stderr)
            return 1
        return 0

    backend_base_url = os.environ.get("BACKEND_BASE_URL")
    if not backend_base_url:
        print("ERROR: BACKEND_BASE_URL env var is required", file=sys.stderr)
        return 2

    db_path = os.environ.get("FOOD_DB_PATH", DEFAULT_DB_PATH)
    try:
        conn = open_db(db_path)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    df = pd.read_csv("input.csv", dtype=str, keep_default_na=False)
    os.makedirs("output", exist_ok=True)

    result_errors_csv = RESULT_ERRORS_CSV_DEFAULT

    # One-shot migration: lift any error rows out of an existing results.csv
    # and into the sidecar so they're purged from the visible output and
    # not retried.
    migrated = migrate_errors_from_results("output/results.csv", result_errors_csv)
    if migrated:
        print(f"Migrated {migrated} prior error row(s) → {result_errors_csv}")

    if args.force:
        ok_cache: dict[tuple, dict] = {}
        err_cache: dict[tuple, dict] = {}
    else:
        ok_cache = load_completed_cache("output/results.csv")
        err_cache = load_error_cache(result_errors_csv)
    completed_cache: dict[tuple, dict] = {**ok_cache, **err_cache}
    if completed_cache:
        print(f"Loaded cache: {len(ok_cache)} ok / {len(err_cache)} error "
              f"(use --force to recompute all)")

    results = []
    cached_count = 0
    purged_error_count = 0
    # Errors retained for the sidecar at end-of-run: prior errors whose key
    # still appears in input.csv (kept) plus any fresh errors from this run.
    # Stale errors whose input row is gone naturally drop out.
    sidecar_errors: dict[tuple, dict] = {}
    POSTABILITY_THRESHOLD = 7
    for _, row in df.iterrows():
        row_dict = row.to_dict()

        pair_id = clean_cell(row_dict.get("pair_id"))
        a_name = clean_cell(row_dict.get("food_a_name"))
        b_name = clean_cell(row_dict.get("food_b_name"))
        a_barcode = clean_cell(row_dict.get("food_a_barcode"))
        b_barcode = clean_cell(row_dict.get("food_b_barcode"))
        framing = clean_cell(row_dict.get("framing_type"))
        purpose = clean_cell(row_dict.get("purpose")) or "snack"
        fmt = resolve_format(row_dict)

        # Cache check before any expensive work. Key includes purpose/framing/
        # barcodes/format so an edit to those fields invalidates the cache.
        cache_lookup = {
            "pair_id": pair_id,
            "food_a_barcode": a_barcode,
            "food_b_barcode": b_barcode,
            "framing_type": framing,
            "format": fmt,
            "purpose": purpose,
        }
        cache_key = _result_cache_key(cache_lookup)
        cached = completed_cache.get(cache_key)
        if cached is not None:
            if (cached.get("status") or "") == "error":
                # Prior error: purge from results.csv (don't append) but keep
                # in the sidecar so it's still suppressed next run.
                sidecar_errors[cache_key] = {
                    **cache_lookup,
                    "food_a_name": a_name, "food_b_name": b_name,
                    "error_message": cached.get("error_message", ""),
                    "errored_at": cached.get("errored_at", ""),
                }
                purged_error_count += 1
                print(f"[{pair_id}] SKIPPED (prior error) — edit a key field to retry")
            else:
                results.append(cached)
                cached_count += 1
                print(f"[{pair_id}] CACHED ({cached.get('status', '?')}) — skipped re-scoring")
            continue

        a_display, b_display, a_label, b_label = resolve_names(row_dict)

        base_record = {
            "pair_id": pair_id,
            "food_a_name": a_name,
            "food_a_display_name": a_display,
            "food_a_label": a_label,
            "food_a_barcode": a_barcode,
            "food_b_name": b_name,
            "food_b_display_name": b_display,
            "food_b_label": b_label,
            "food_b_barcode": b_barcode,
            "purpose": purpose,
            "framing_type": framing,
            "format": fmt,
        }

        # Ranking format is intentionally skipped for now.
        if fmt == "ranking":
            results.append({
                **base_record,
                "food_a_score": None, "food_a_interpretation": "",
                "food_a_what_helps": "", "food_a_what_hurts": "",
                "food_b_score": None, "food_b_interpretation": "",
                "food_b_what_helps": "", "food_b_what_hurts": "",
                "score_diff": None,
                "headline": "", "headlineMode": "",
                "content_priority": 0,
                "postability_score": 0,
                "verdict": "",
                "status": "skipped",
                "error_message": "format=ranking not implemented yet",
            })
            print(f"[{pair_id}] SKIPPED (ranking format)")
            continue

        # Score food_a always; score food_b only when the format needs it.
        try:
            a_score, a_interp, a_helps, a_hurts, a_err = score_food(
                conn, backend_base_url, a_display, a_barcode, purpose)
            # Single-food formats: only score food_a, leave food_b empty.
            if fmt in ("exposure", "evaluation"):
                b_score, b_interp, b_helps, b_hurts, b_err = (None, "", [], [], None)
            else:
                b_score, b_interp, b_helps, b_hurts, b_err = score_food(
                    conn, backend_base_url, b_display, b_barcode, purpose)
        except Exception as e:  # never let one row kill the batch
            sidecar_errors[cache_key] = {
                **cache_lookup,
                "food_a_name": a_name, "food_b_name": b_name,
                "error_message": f"unexpected: {e}",
                "errored_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            print(f"[{pair_id}] ERROR (unexpected): {e} — recorded to {result_errors_csv}")
            continue

        errors = [m for m in (a_err, b_err) if m]
        if errors:
            sidecar_errors[cache_key] = {
                **cache_lookup,
                "food_a_name": a_name, "food_b_name": b_name,
                "error_message": "; ".join(errors),
                "errored_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            print(f"[{pair_id}] ERROR: {'; '.join(errors)} — recorded to {result_errors_csv}")
            continue

        halo = is_health_halo(row_dict)
        headline, headline_mode = generate_headline(
            row_dict, fmt, a_score, b_score, halo,
        )
        relation = score_relation(a_score, b_score) if (a_score is not None and b_score is not None) else ""
        score_diff = (a_score - b_score) if (a_score is not None and b_score is not None) else None
        priority = content_priority(framing, relation) if relation else 0
        postability = postability_score(fmt, headline, halo, a_score, b_score, relation)
        verdict = verdict_sentence(a_display, a_score, a_helps, a_hurts)
        if postability >= POSTABILITY_THRESHOLD:
            status = "ok"
            error_message = ""
        else:
            status = "low_postability"
            error_message = f"postability {postability} < {POSTABILITY_THRESHOLD}"

        results.append({
            **base_record,
            "food_a_score": a_score, "food_a_interpretation": a_interp,
            "food_a_what_helps": " ; ".join(a_helps),
            "food_a_what_hurts": " ; ".join(a_hurts),
            "food_b_score": b_score, "food_b_interpretation": b_interp,
            "food_b_what_helps": " ; ".join(b_helps),
            "food_b_what_hurts": " ; ".join(b_hurts),
            "score_diff": score_diff,
            "headline": headline,
            "headlineMode": headline_mode,
            "content_priority": priority,
            "postability_score": postability,
            "verdict": verdict,
            "status": status,
            "error_message": error_message,
        })

        if status == "ok":
            tag = f"[{fmt}/{headline_mode}]"
            b_part = f"vs {b_display} ({b_score})" if b_score is not None else "(single)"
            print(f"[{pair_id}] {tag} {a_display} ({a_score}) {b_part} "
                  f"post={postability} — {headline}")
        else:
            print(f"[{pair_id}] FILTERED post={postability} — {headline or '(no headline)'}")

    out_df = pd.DataFrame(results, columns=[
        "pair_id",
        "format",
        "food_a_name", "food_a_display_name", "food_a_label", "food_a_barcode",
        "food_a_score", "food_a_interpretation",
        "food_a_what_helps", "food_a_what_hurts",
        "food_b_name", "food_b_display_name", "food_b_label", "food_b_barcode",
        "food_b_score", "food_b_interpretation",
        "food_b_what_helps", "food_b_what_hurts",
        "score_diff", "purpose", "framing_type",
        "headline", "headlineMode", "content_priority",
        "postability_score", "verdict",
        "status", "error_message",
    ])
    out_df.to_csv("output/results.csv", index=False)

    # Sidecar of error rows. Includes prior errors whose input row still
    # exists (kept) plus any fresh errors from this run. Stale errors whose
    # input row was deleted from input.csv naturally drop out.
    if sidecar_errors:
        write_error_cache(result_errors_csv, sidecar_errors.values())
    elif Path(result_errors_csv).exists():
        # All previously-tracked errors are gone from input.csv → empty file.
        write_error_cache(result_errors_csv, [])

    # Per-item blog inputs (only for status=='ok' rows that passed the filter).
    from bullets import punch_up_bullets, split_raw_bullets

    blog_dir = "output/blog_inputs"
    os.makedirs(blog_dir, exist_ok=True)
    blog_written = 0
    for r in results:
        if r["status"] != "ok":
            continue

        a_helps_raw = split_raw_bullets(r["food_a_what_helps"] or "")
        a_hurts_raw = split_raw_bullets(r["food_a_what_hurts"] or "")
        b_helps_raw = split_raw_bullets(r["food_b_what_helps"] or "")
        b_hurts_raw = split_raw_bullets(r["food_b_what_hurts"] or "")

        # Punchy bullets the card uses, exposed for downstream blog synth.
        a_score = r["food_a_score"]
        a_role = "pick" if (isinstance(a_score, int) and a_score >= 7) else "avoid"
        food_a_bullets = punch_up_bullets(
            a_helps_raw if a_role == "pick" else a_hurts_raw,
            a_role,
        )

        b_score = r["food_b_score"]
        food_b_bullets: list[str] = []
        if b_score is not None:
            b_role = "pick" if (isinstance(b_score, int) and b_score >= 7) else "avoid"
            food_b_bullets = punch_up_bullets(
                b_helps_raw if b_role == "pick" else b_hurts_raw,
                b_role,
            )

        payload = {
            "pair_id": r["pair_id"],
            "format": r["format"],
            "purpose": r["purpose"],
            "framing_type": r["framing_type"],
            "headline": r["headline"],
            "headlineMode": r["headlineMode"],
            "verdict": r["verdict"],
            "postability_score": r["postability_score"],
            "food_a_bullets": food_a_bullets,
            "food_b_bullets": food_b_bullets,
            "food_a": {
                "name": r["food_a_name"],
                "display_name": r["food_a_display_name"],
                "score": r["food_a_score"],
                "interpretation": r["food_a_interpretation"],
                "what_helps": a_helps_raw,
                "what_hurts": a_hurts_raw,
            },
            "food_b": None if r["food_b_score"] is None else {
                "name": r["food_b_name"],
                "display_name": r["food_b_display_name"],
                "score": r["food_b_score"],
                "interpretation": r["food_b_interpretation"],
                "what_helps": b_helps_raw,
                "what_hurts": b_hurts_raw,
            },
        }
        pid = re.sub(r"[^A-Za-z0-9]", "", str(r["pair_id"])) or "x"
        path = os.path.join(blog_dir, f"pair_{pid.zfill(3)}.json")
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        blog_written += 1

    # Summary.
    total = len(results)
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    print()
    print("=== Summary ===")
    print(f"Total rows: {total}")
    for s in ("ok", "low_postability", "error", "skipped"):
        if by_status.get(s):
            print(f"  {s:<16} {by_status[s]}")
    if cached_count:
        print(f"Cached (skipped re-scoring): {cached_count} of {total}")
    if sidecar_errors:
        print(f"Errors purged from results.csv: {len(sidecar_errors)} "
              f"({purged_error_count} prior + {len(sidecar_errors) - purged_error_count} new) "
              f"→ {result_errors_csv}")
    print(f"Wrote {total} rows to output/results.csv")
    print(f"Wrote {blog_written} blog input JSON file(s) to {blog_dir}/")

    successes = [r for r in results if r["status"] == "ok"]
    top = sorted(successes, key=lambda r: r["postability_score"], reverse=True)[:5]
    if top:
        print()
        print("Top 5 by postability_score:")
        for r in top:
            print(
                f"  [{r['pair_id']}] post={r['postability_score']:>2}  "
                f"[{r['format']}/{r['headlineMode']}] {r['food_a_display_name']} ({r['food_a_score']}) — {r['headline']}"
            )

    # Render PNG cards for status=='ok' rows.
    if args.no_cards:
        print("\nSkipped card rendering (--no-cards).")
    else:
        try:
            from cards import render_cards
            rendered = render_cards("output/results.csv", "output/cards")
            print(f"\nRendered {rendered} card PNG(s) to output/cards/")
        except Exception as e:
            print(f"\nWARNING: card rendering failed: {e}", file=sys.stderr)

    # Generate markdown blog posts from the blog_inputs JSON via OpenAI.
    # No-ops when OPENAI_API_KEY is unset; never blocks card rendering.
    try:
        write_blog_posts_from_inputs(blog_dir, "output/blog_posts", force=args.force)
    except Exception as e:
        print(f"\nWARNING: blog post generation failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
