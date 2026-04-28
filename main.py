"""SmarterEats content engine.

Reads pairs of foods from input.csv, looks each barcode up in a local SQLite
copy of the Open Food Facts data, calls the SmarterEats backend /score
endpoint for each, and writes a results CSV with deterministic hooks.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
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

    OFF stores 13-digit EAN. US UPCs (12 digits) are typically stored with a
    leading '0'. Some entries also keep the bare 12-digit form. Try the
    user's value first, then a leading-zero-padded variant, then a stripped
    variant — whichever path matches first wins.
    """
    out = [barcode]
    if barcode.isdigit():
        if len(barcode) == 12:
            out.append("0" + barcode)
        elif len(barcode) == 13 and barcode.startswith("0"):
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

VALID_FORMATS = ("comparison", "exposure", "swap", "ranking")


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
    _, _, a_label, b_label = resolve_names(row)
    pair_id = clean_cell(row.get("pair_id"))

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
# Main
# ---------------------------------------------------------------------------

def main() -> int:
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

    results = []
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
            if fmt == "exposure":
                b_score, b_interp, b_helps, b_hurts, b_err = (None, "", [], [], None)
            else:
                b_score, b_interp, b_helps, b_hurts, b_err = score_food(
                    conn, backend_base_url, b_display, b_barcode, purpose)
        except Exception as e:  # never let one row kill the batch
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
                "status": "error",
                "error_message": f"unexpected: {e}",
            })
            print(f"[{pair_id}] ERROR (unexpected): {e}")
            continue

        errors = [m for m in (a_err, b_err) if m]
        if errors:
            headline, headline_mode = "", ""
            relation = ""
            score_diff = None
            priority = 0
            postability = 0
            verdict = ""
            status = "error"
            error_message = "; ".join(errors)
        else:
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
        elif status == "low_postability":
            print(f"[{pair_id}] FILTERED post={postability} — {headline or '(no headline)'}")
        else:
            print(f"[{pair_id}] ERROR: {error_message}")

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
    try:
        from cards import render_cards
        rendered = render_cards("output/results.csv", "output/cards")
        print(f"\nRendered {rendered} card PNG(s) to output/cards/")
    except Exception as e:
        print(f"\nWARNING: card rendering failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
