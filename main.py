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


def lookup_product(conn: sqlite3.Connection, barcode: str) -> Optional[dict]:
    """Return a dict with keys: code, name, brand, nutriments (dict),
    serving_size, serving_quantity, ingredients_text. None if not found."""
    barcode = barcode.strip()
    if not barcode:
        return None

    # Prefer canonical_products if present (curated/merged OFF data).
    if _table_exists(conn, "canonical_products"):
        row = conn.execute(
            "SELECT code, canonical_name AS name, canonical_brand AS brand, "
            "nutriments, serving_size, serving_quantity, ingredients_text "
            "FROM canonical_products WHERE code = ?",
            (barcode,),
        ).fetchone()
        if row:
            return _row_to_product(row)

    if _table_exists(conn, "products"):
        row = conn.execute(
            "SELECT code, product_name AS name, brands AS brand, "
            "nutriments, serving_size, serving_quantity, ingredients_text "
            "FROM products WHERE code = ?",
            (barcode,),
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


def generate_hook(row: dict, score_a: Optional[int], score_b: Optional[int]) -> tuple[str, str]:
    """Return (hook, relation). Empty hook when scores are missing."""
    if score_a is None or score_b is None:
        return "", ""

    _, _, a_label, b_label = resolve_names(row)
    framing = clean_cell(row.get("framing_type")).lower()

    relation = score_relation(score_a, score_b)

    if framing == "health_halo":
        if relation == "a_loses":
            hook = f"{a_label} is worse than {b_label}"
        elif relation == "tie":
            hook = f"{a_label} is basically the same as {b_label}"
        elif relation == "a_barely_wins":
            hook = f"{a_label} is barely better than {b_label}"
        else:
            hook = f"{a_label} wins, but it’s not as clean as it looks"
    elif framing == "kids_food":
        if relation in ("tie", "a_loses"):
            hook = "This kids snack is basically candy"
        elif relation == "a_barely_wins":
            hook = "This kids snack is barely better than candy"
        else:
            hook = "This kids snack actually holds up"
    elif framing == "sugar_shock":
        if relation in ("tie", "a_loses"):
            hook = f"{a_label} is basically dessert"
        elif relation == "a_barely_wins":
            hook = f"{a_label} is barely better than dessert"
        else:
            hook = f"{a_label} beats dessert, but check the sugar"
    else:
        if relation == "tie":
            hook = f"{a_label} is basically the same as {b_label}"
        elif relation == "a_barely_wins":
            hook = f"{a_label} is barely better than {b_label}"
        elif relation == "a_loses":
            hook = f"{a_label} is worse than {b_label}"
        else:
            hook = f"{a_label} actually beats {b_label}"

    return hook, relation


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
) -> tuple[Optional[int], str, list[str], Optional[str]]:
    """Return (score, interpretation, what_helps, error_message). On error, score is None."""
    barcode = clean_cell(barcode)
    if not barcode:
        return None, "", [], f"missing barcode for {csv_name or '(unnamed)'}"

    product = lookup_product(conn, barcode)
    if product is None:
        return None, "", [], f"barcode {barcode} not found in DB for {csv_name or '(unnamed)'}"

    extracted = build_extracted_nutrition(product)

    missing = [k for k in REQUIRED_NUTRITION_FIELDS if extracted.get(k) is None]
    if missing:
        return None, "", [], (
            f"missing nutrition fields for {csv_name or '(unnamed)'}: "
            + ", ".join(missing)
        )

    try:
        result = call_score(backend_base_url, extracted, purpose, csv_name)
    except ScoreError as e:
        return None, "", [], f"backend error for {csv_name or '(unnamed)'}: {e}"

    score = result.get("score")
    interpretation = result.get("interpretation") or ""
    helps = result.get("whatHelps") or []
    if not isinstance(helps, list):
        helps = []
    helps = [str(h).strip() for h in helps if str(h).strip()]

    if score is None:
        return None, interpretation, helps, f"backend returned null score for {csv_name or '(unnamed)'}"
    try:
        score = int(score)
    except (TypeError, ValueError):
        return None, interpretation, helps, f"backend score not an int for {csv_name or '(unnamed)'}: {score!r}"

    return score, interpretation, helps, None


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
    for _, row in df.iterrows():
        row_dict = row.to_dict()

        pair_id = clean_cell(row_dict.get("pair_id"))
        a_name = clean_cell(row_dict.get("food_a_name"))
        b_name = clean_cell(row_dict.get("food_b_name"))
        a_barcode = clean_cell(row_dict.get("food_a_barcode"))
        b_barcode = clean_cell(row_dict.get("food_b_barcode"))
        framing = clean_cell(row_dict.get("framing_type"))
        purpose = clean_cell(row_dict.get("purpose")) or "snack"

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
        }

        try:
            a_score, a_interp, a_helps, a_err = score_food(conn, backend_base_url, a_display, a_barcode, purpose)
            b_score, b_interp, b_helps, b_err = score_food(conn, backend_base_url, b_display, b_barcode, purpose)
        except Exception as e:  # never let one row kill the batch
            results.append({
                **base_record,
                "food_a_score": None, "food_a_interpretation": "", "food_a_what_helps": "",
                "food_b_score": None, "food_b_interpretation": "", "food_b_what_helps": "",
                "score_diff": None,
                "hook": "",
                "content_priority": 0,
                "status": "error",
                "error_message": f"unexpected: {e}",
            })
            print(f"[{pair_id}] ERROR (unexpected): {e}")
            continue

        errors = [m for m in (a_err, b_err) if m]
        if errors:
            hook, relation = "", ""
            score_diff = None
            priority = 0
            status = "error"
            error_message = "; ".join(errors)
        else:
            hook, relation = generate_hook(row_dict, a_score, b_score)
            score_diff = a_score - b_score
            priority = content_priority(framing, relation)
            status = "ok"
            error_message = ""

        results.append({
            **base_record,
            "food_a_score": a_score, "food_a_interpretation": a_interp,
            "food_a_what_helps": " ; ".join(a_helps),
            "food_b_score": b_score, "food_b_interpretation": b_interp,
            "food_b_what_helps": " ; ".join(b_helps),
            "score_diff": score_diff,
            "hook": hook,
            "content_priority": priority,
            "status": status,
            "error_message": error_message,
        })

        if status == "ok":
            tag = f"[{framing}]" if framing else ""
            print(f"[{pair_id}] {tag} {a_display} ({a_score}) vs {b_display} ({b_score}) "
                  f"diff={score_diff} priority={priority} — {hook}")
        else:
            print(f"[{pair_id}] ERROR: {error_message}")

    out_df = pd.DataFrame(results, columns=[
        "pair_id",
        "food_a_name", "food_a_display_name", "food_a_label", "food_a_barcode",
        "food_a_score", "food_a_interpretation", "food_a_what_helps",
        "food_b_name", "food_b_display_name", "food_b_label", "food_b_barcode",
        "food_b_score", "food_b_interpretation", "food_b_what_helps",
        "score_diff", "purpose", "framing_type",
        "hook", "content_priority",
        "status", "error_message",
    ])
    out_df.to_csv("output/results.csv", index=False)

    # Summary
    total = len(results)
    ok_count = sum(1 for r in results if r["status"] == "ok")
    fail_count = total - ok_count
    print()
    print("=== Summary ===")
    print(f"Total rows: {total}")
    print(f"Successful: {ok_count}")
    print(f"Failed:     {fail_count}")
    print(f"Wrote {total} rows to output/results.csv")

    successes = [r for r in results if r["status"] == "ok"]
    top = sorted(successes, key=lambda r: r["content_priority"], reverse=True)[:5]
    if top:
        print()
        print("Top 5 by content_priority:")
        for r in top:
            print(
                f"  [{r['pair_id']}] priority={r['content_priority']:>3}  "
                f"{r['food_a_display_name']} ({r['food_a_score']}) vs "
                f"{r['food_b_display_name']} ({r['food_b_score']}) — {r['hook']}"
            )

    # Render PNG cards for successful rows.
    try:
        from cards import render_cards
        rendered = render_cards("output/results.csv", "output/cards")
        print(f"\nRendered {rendered} card PNG(s) to output/cards/")
    except Exception as e:
        print(f"\nWARNING: card rendering failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
