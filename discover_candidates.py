"""Discovery batch for the SmarterEats content engine.

Walks the local OFF database for promising food comparisons, scores only
the promising candidates against a small set of cached reference foods,
and writes each kept comparison as a `pending` row in
`output/discovery_results.csv`. The Streamlit "Cleaner Mode" then turns
those rows into approved input.csv entries.

This script does NOT:
  - render PNG cards
  - generate input.csv directly
  - require Streamlit
  - score the entire DB blindly

WEEKLY CADENCE
--------------
Designed to run weekly and produce ~50-200 new candidates per run. Two
mechanisms make this work:

1. `output/considered_pairs.csv` records every (candidate_barcode,
   reference_barcode) pair the script has scored — kept OR dropped. On
   the next run those pairs are skipped, so each week naturally advances
   to fresh products further down each bucket's completeness ranking.

2. The `--top` / `--max-raw` flags tune the depth per bucket. Defaults
   (top=12) target ~150 candidates scored per run with mixed buckets +
   brands.

Usage (with the backend running):

    export BACKEND_BASE_URL="http://localhost:8000"
    export FOOD_DB_PATH="/Users/.../foodscore/backend/off_products.db"
    ./venv/bin/python discover_candidates.py [options]

Common flags:
    --top N            candidates per bucket (default 12)
    --max-raw N        raw matches pulled per bucket query (default 100)
    --bucket KEY       run only this one bucket (e.g. yogurt)
    --brands           also run brand-driven discovery
    --brands-only      only run brand-driven discovery
    --dry-run          print what would be scored, don't call the backend
    --reset            clear considered_pairs.csv before running
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Reuse the lookup + scoring pipeline from main.py rather than duplicate.
from main import (
    open_db,
    lookup_product,
    score_food,
    clean_cell,
)


OUTPUT_CSV = "output/discovery_results.csv"
CONSIDERED_LOG = "output/considered_pairs.csv"


# --- Reference foods ------------------------------------------------------
#
# Each ref has:
#   - canonical name + label suggestion (used in the discovery row)
#   - barcodes: explicit hints to try first (DB lookup with leading-zero
#     fallback handles 12↔13-digit variants)
#   - name_search: query terms used as a fallback when no barcode resolves

REFERENCE_FOODS: dict[str, dict] = {
    "snickers": {
        "name": "Snickers",
        "label": "candy bar",
        "display_name": "Snickers",
        "barcodes": ["0040000002635", "0040000001058", "0040000424314"],
        "name_search": "snickers mars",
    },
    "coca_cola": {
        "name": "Coca-Cola",
        "label": "soda",
        "display_name": "Coca-Cola",
        "barcodes": ["0088009900767", "0049000028928", "5449000000996"],
        "name_search": "coca cola classic",
    },
    "frosted_flakes": {
        "name": "Frosted Flakes",
        "label": "sugary cereal",
        "display_name": "Frosted Flakes",
        "barcodes": ["0038000181771"],
        "name_search": "frosted flakes kellogg",
    },
    "doritos": {
        "name": "Doritos",
        "label": "chips",
        "display_name": "Doritos",
        "barcodes": [],
        "name_search": "doritos nacho cheese",
    },
    "pringles": {
        "name": "Pringles",
        "label": "chips",
        "display_name": "Pringles",
        "barcodes": ["0038000007019"],
        "name_search": "pringles original",
    },
    "skittles": {
        "name": "Skittles",
        "label": "candy",
        "display_name": "Skittles",
        "barcodes": [],
        "name_search": "skittles original",
    },
    "oreo": {
        "name": "Oreo",
        "label": "cookies",
        "display_name": "Oreo",
        "barcodes": [],
        "name_search": "oreo original",
    },
    "mountain_dew": {
        "name": "Mountain Dew",
        "label": "soda",
        "display_name": "Mountain Dew",
        "barcodes": [],
        "name_search": "mountain dew",
    },
    "ben_jerrys": {
        "name": "Ben & Jerry's",
        "label": "ice cream",
        "display_name": "Ben & Jerry's",
        "barcodes": [],
        "name_search": "ben jerry's",
    },
}


# --- Buckets --------------------------------------------------------------
#
# Each bucket has multiple search queries to widen the OFF match space.
# Each bucket is tied to a default reference for the "vs X" comparison.

CANDIDATE_BUCKETS: list[dict] = [
    {"key": "smoothie", "queries": ["smoothie", "fruit smoothie", "green smoothie"],
     "framing": "sugar_shock", "purpose": "snack",
     "label_hint": "healthy smoothie", "angle": "smoothie sugar exposure",
     "ref": "coca_cola"},
    {"key": "juice", "queries": ["juice", "fruit juice", "100% juice"],
     "framing": "sugar_shock", "purpose": "snack",
     "label_hint": "juice", "angle": "juice sugar exposure",
     "ref": "coca_cola"},
    {"key": "green_tea", "queries": ["green tea", "matcha"],
     "framing": "default", "purpose": "snack",
     "label_hint": "green tea", "angle": "sweetened tea exposure",
     "ref": "coca_cola"},
    {"key": "vitamin_water", "queries": ["vitamin water", "vitaminwater", "enhanced water"],
     "framing": "sugar_shock", "purpose": "snack",
     "label_hint": "vitamin water", "angle": "functional drink halo",
     "ref": "coca_cola"},
    {"key": "sports_drink", "queries": ["sports drink", "gatorade", "powerade", "electrolyte"],
     "framing": "sugar_shock", "purpose": "snack",
     "label_hint": "sports drink", "angle": "sports drink sugar",
     "ref": "coca_cola"},
    {"key": "kombucha", "queries": ["kombucha"],
     "framing": "health_halo", "purpose": "snack",
     "label_hint": "kombucha", "angle": "kombucha halo",
     "ref": "coca_cola"},
    {"key": "plant_milk", "queries": ["almond milk", "oat milk", "soy milk", "coconut milk drink"],
     "framing": "health_halo", "purpose": "snack",
     "label_hint": "plant milk", "angle": "plant milk halo",
     "ref": "coca_cola"},
    {"key": "diet_soda", "queries": ["diet soda", "zero sugar"],
     "framing": "default", "purpose": "snack",
     "label_hint": "diet soda", "angle": "diet soda exposure",
     "ref": "coca_cola"},

    {"key": "granola", "queries": ["granola", "granola cluster"],
     "framing": "health_halo", "purpose": "snack",
     "label_hint": "granola", "angle": "granola halo",
     "ref": "snickers"},
    {"key": "granola_bar", "queries": ["granola bar", "chewy bar", "crunchy granola bar"],
     "framing": "health_halo", "purpose": "snack",
     "label_hint": "granola bar", "angle": "granola bar halo",
     "ref": "snickers"},
    {"key": "protein_bar", "queries": ["protein bar", "high protein bar"],
     "framing": "health_halo", "purpose": "snack",
     "label_hint": "protein bar", "angle": "protein bar halo",
     "ref": "snickers"},
    {"key": "energy_bar", "queries": ["energy bar", "rxbar", "kind bar"],
     "framing": "health_halo", "purpose": "snack",
     "label_hint": "energy bar", "angle": "energy bar halo",
     "ref": "snickers"},
    {"key": "meal_replacement", "queries": ["meal replacement", "meal bar", "ensure", "soylent"],
     "framing": "health_halo", "purpose": "meal",
     "label_hint": "meal replacement", "angle": "meal replacement halo",
     "ref": "snickers"},

    {"key": "yogurt", "queries": ["yogurt", "greek yogurt", "icelandic yogurt"],
     "framing": "health_halo", "purpose": "snack",
     "label_hint": "yogurt", "angle": "yogurt halo",
     "ref": "snickers"},
    {"key": "kids_yogurt", "queries": ["gogurt", "go-gurt", "kids yogurt", "yogurt tube"],
     "framing": "kids_food", "purpose": "snack",
     "label_hint": "kids yogurt", "angle": "kids yogurt halo",
     "ref": "snickers"},
    {"key": "fruit_snacks", "queries": ["fruit snack", "fruit gummies", "fruit roll"],
     "framing": "kids_food", "purpose": "snack",
     "label_hint": "fruit snacks", "angle": "kids fruit snacks",
     "ref": "skittles"},
    {"key": "kids_drink", "queries": ["juice box", "capri sun", "kids juice"],
     "framing": "kids_food", "purpose": "snack",
     "label_hint": "kids juice", "angle": "kids drink halo",
     "ref": "coca_cola"},

    {"key": "cereal", "queries": ["cereal", "breakfast cereal", "whole grain cereal"],
     "framing": "health_halo", "purpose": "breakfast",
     "label_hint": "cereal", "angle": "cereal comparison",
     "ref": "frosted_flakes"},
    {"key": "oatmeal", "queries": ["oatmeal", "instant oats", "flavored oatmeal"],
     "framing": "health_halo", "purpose": "breakfast",
     "label_hint": "oatmeal", "angle": "oatmeal halo",
     "ref": "frosted_flakes"},
    {"key": "breakfast_bar", "queries": ["breakfast bar", "breakfast biscuit"],
     "framing": "health_halo", "purpose": "breakfast",
     "label_hint": "breakfast bar", "angle": "breakfast bar halo",
     "ref": "frosted_flakes"},

    {"key": "veggie_chips", "queries": ["veggie chip", "vegetable chip", "veggie straws"],
     "framing": "health_halo", "purpose": "snack",
     "label_hint": "veggie chips", "angle": "veggie chips halo",
     "ref": "doritos"},
    {"key": "popcorn", "queries": ["popcorn", "popped corn", "kettle corn"],
     "framing": "health_halo", "purpose": "snack",
     "label_hint": "popcorn", "angle": "popcorn comparison",
     "ref": "doritos"},
    {"key": "crackers", "queries": ["crackers", "wheat thin", "triscuit", "rice cake"],
     "framing": "health_halo", "purpose": "snack",
     "label_hint": "crackers", "angle": "crackers halo",
     "ref": "doritos"},
    {"key": "potato_chips", "queries": ["potato chip", "kettle chip", "baked chip"],
     "framing": "default", "purpose": "snack",
     "label_hint": "chips", "angle": "chips comparison",
     "ref": "doritos"},

    {"key": "ice_cream", "queries": ["ice cream", "halo top", "frozen dessert"],
     "framing": "sugar_shock", "purpose": "treat",
     "label_hint": "ice cream", "angle": "ice cream halo",
     "ref": "ben_jerrys"},
    {"key": "frozen_yogurt", "queries": ["frozen yogurt", "froyo"],
     "framing": "health_halo", "purpose": "treat",
     "label_hint": "frozen yogurt", "angle": "froyo halo",
     "ref": "ben_jerrys"},
    {"key": "cookie", "queries": ["cookie", "biscuit"],
     "framing": "default", "purpose": "treat",
     "label_hint": "cookie", "angle": "cookie comparison",
     "ref": "oreo"},

    {"key": "gluten_free", "queries": ["gluten free", "gluten-free"],
     "framing": "health_halo", "purpose": "snack",
     "label_hint": "gluten-free snack", "angle": "gluten-free halo",
     "ref": "snickers"},
    {"key": "organic_snack", "queries": ["organic snack", "organic bar", "organic chip"],
     "framing": "health_halo", "purpose": "snack",
     "label_hint": "organic snack", "angle": "organic halo",
     "ref": "snickers"},
    {"key": "keto_snack", "queries": ["keto bar", "keto snack", "low carb bar"],
     "framing": "health_halo", "purpose": "snack",
     "label_hint": "keto snack", "angle": "keto halo",
     "ref": "snickers"},
    {"key": "vegan_snack", "queries": ["vegan bar", "vegan snack", "plant-based snack"],
     "framing": "health_halo", "purpose": "snack",
     "label_hint": "vegan snack", "angle": "vegan halo",
     "ref": "snickers"},
]


# --- Brand-driven buckets -------------------------------------------------
#
# When --brands is set, each entry pulls all products from a single brand
# and treats them like a bucket. brand_norm is matched against
# brands_normalized as a comma-delimited token (case-insensitive), so
# "kind" matches "KIND" and "KIND, Mars" but not "Mankind".

BRAND_BUCKETS: list[dict] = [
    {"key": "brand_kind",          "brand_norm": "kind",
     "ref": "snickers",       "framing": "health_halo", "purpose": "snack",
     "angle": "kind brand halo",     "label_hint": "kind bar"},
    {"key": "brand_naked",         "brand_norm": "naked juice",
     "ref": "coca_cola",      "framing": "sugar_shock", "purpose": "snack",
     "angle": "naked juice halo",    "label_hint": "smoothie"},
    {"key": "brand_clif",          "brand_norm": "clif",
     "ref": "snickers",       "framing": "health_halo", "purpose": "snack",
     "angle": "clif bar halo",       "label_hint": "energy bar"},
    {"key": "brand_rxbar",         "brand_norm": "rxbar",
     "ref": "snickers",       "framing": "health_halo", "purpose": "snack",
     "angle": "rxbar halo",          "label_hint": "protein bar"},
    {"key": "brand_naturevalley",  "brand_norm": "nature valley",
     "ref": "snickers",       "framing": "health_halo", "purpose": "snack",
     "angle": "nature valley halo",  "label_hint": "granola bar"},
    {"key": "brand_quaker",        "brand_norm": "quaker",
     "ref": "frosted_flakes", "framing": "health_halo", "purpose": "breakfast",
     "angle": "quaker halo",         "label_hint": "oatmeal"},
    {"key": "brand_kashi",         "brand_norm": "kashi",
     "ref": "frosted_flakes", "framing": "health_halo", "purpose": "breakfast",
     "angle": "kashi halo",          "label_hint": "cereal"},
    {"key": "brand_chobani",       "brand_norm": "chobani",
     "ref": "snickers",       "framing": "health_halo", "purpose": "snack",
     "angle": "chobani halo",        "label_hint": "greek yogurt"},
    {"key": "brand_yoplait",       "brand_norm": "yoplait",
     "ref": "snickers",       "framing": "kids_food",   "purpose": "snack",
     "angle": "yoplait halo",        "label_hint": "yogurt"},
    {"key": "brand_vitaminwater",  "brand_norm": "vitaminwater",
     "ref": "coca_cola",      "framing": "sugar_shock", "purpose": "snack",
     "angle": "vitaminwater halo",   "label_hint": "vitamin water"},
    {"key": "brand_gatorade",      "brand_norm": "gatorade",
     "ref": "coca_cola",      "framing": "sugar_shock", "purpose": "snack",
     "angle": "gatorade halo",       "label_hint": "sports drink"},
    {"key": "brand_special_k",     "brand_norm": "special k",
     "ref": "frosted_flakes", "framing": "health_halo", "purpose": "breakfast",
     "angle": "special k halo",      "label_hint": "diet cereal"},
    {"key": "brand_halo_top",      "brand_norm": "halo top",
     "ref": "ben_jerrys",     "framing": "sugar_shock", "purpose": "treat",
     "angle": "halo top halo",       "label_hint": "low-cal ice cream"},
    {"key": "brand_kind_dark",     "brand_norm": "kind dark chocolate",
     "ref": "snickers",       "framing": "health_halo", "purpose": "snack",
     "angle": "kind dark halo",      "label_hint": "dark chocolate bar"},
]


# --- Alternative foods (used for swap rows) -------------------------------
#
# Curated list of "good alternative" foods. Each entry has the same shape as
# REFERENCE_FOODS so it can be resolved + scored at startup. Only buckets
# listed in BUCKET_ALTERNATIVE produce swap rows.

ALTERNATIVE_FOODS: dict[str, dict] = {
    "whole_orange": {
        "name": "Whole Orange",
        "label": "whole orange",
        "display_name": "Whole Orange",
        # Marks & Spencer "Orange" — full per-100g nutriments, no UPC for raw
        # produce so we pin the only good in-DB match.
        "barcodes": ["00270489"],
        "name_search": "orange fruit raw",
    },
    "plain_greek_yogurt": {
        "name": "Plain Greek Yogurt",
        "label": "plain greek yogurt",
        "display_name": "Plain Greek Yogurt",
        # Chobani Whole Milk Plain Greek Yogurt — high-completeness entry.
        "barcodes": ["0894700010434", "0818290011633"],
        "name_search": "chobani plain greek yogurt",
    },
    "coconut_water": {
        "name": "Coconut Water",
        "label": "coconut water",
        "display_name": "Coconut Water",
        # Harmless Harvest organic coconut water.
        "barcodes": ["0810068900651"],
        "name_search": "coconut water",
    },
    "plain_oatmeal": {
        "name": "Plain Oatmeal",
        "label": "plain oatmeal",
        "display_name": "Plain Oatmeal",
        # To Your Health gluten-free rolled oats — plain and well-populated.
        "barcodes": ["0811205021000"],
        "name_search": "rolled oats old fashioned",
    },
}

# Map bucket key → alternative key. Only buckets in this map produce swap
# rows. Per spec: only juice / smoothie / kids_yogurt / sports_drink /
# sugary cereal qualify.
BUCKET_ALTERNATIVE: dict[str, str] = {
    "juice":         "whole_orange",
    "smoothie":      "plain_greek_yogurt",
    "kids_yogurt":   "plain_greek_yogurt",
    "sports_drink":  "coconut_water",
    "cereal":        "plain_oatmeal",   # closest match to user's "sugary_cereal"
}

# Purpose to use when scoring each alternative.
ALT_PURPOSE: dict[str, str] = {
    "whole_orange":       "snack",
    "plain_greek_yogurt": "snack",
    "coconut_water":      "snack",
    "plain_oatmeal":      "breakfast",
}


# Frames that qualify a candidate for an automatic exposure row.
EXPOSURE_FRAMINGS: frozenset[str] = frozenset(
    ("health_halo", "kids_food", "sugar_shock")
)

# Sentinel reference_barcode for exposure considered-entries (so we can
# dedup exposure independently of any comparison reference).
EXPOSURE_MARKER = "EXPOSURE"


DEFAULT_TOP = 12        # candidates scored per bucket per run
DEFAULT_MAX_RAW = 100   # raw DB matches considered before pre-filter


# --- DB search + filtering ------------------------------------------------

def _parse_nutri(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _completeness(nutri: dict) -> int:
    keys = (
        "energy-kcal_100g", "proteins_100g", "fat_100g", "carbohydrates_100g",
        "sugars_100g", "fiber_100g", "saturated-fat_100g", "sodium_100g",
        "added-sugars_100g",
    )
    if not nutri:
        return 0
    have = sum(1 for k in keys if nutri.get(k) is not None)
    return min(10, round(have * 10 / len(keys)))


def _enrich_row(row: sqlite3.Row) -> dict:
    nutri = _parse_nutri(row["nutriments"])
    sg = row["serving_quantity"]
    try:
        sg_f = float(sg) if sg is not None else None
    except (TypeError, ValueError):
        sg_f = None
    return {
        "code": row["code"],
        "name": row["product_name"] or "",
        "brand": row["brands"] or "",
        "serving_size": row["serving_size"] or "",
        "serving_quantity": sg_f,
        "ingredients_text": (row["ingredients_text"] or "").strip(),
        "nutriments": nutri,
        "completeness": _completeness(nutri),
    }


def search_db(conn: sqlite3.Connection, query: str, limit: int) -> list[dict]:
    """FTS-then-LIKE name search with hyphen/space-collapsed fallback."""
    q = query.strip()
    if not q:
        return []
    tokens = [t for t in q.lower().split() if t]
    if not tokens:
        return []

    columns = (
        "code, product_name, brands, nutriments, "
        "serving_size, serving_quantity, ingredients_text"
    )
    fts_query = " ".join(tokens)
    try:
        cur = conn.execute(
            f"""
            SELECT {columns}
            FROM products_fts fts
            JOIN products p ON p.rowid = fts.rowid
            WHERE products_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts_query, limit),
        )
        rows = list(cur.fetchall())
    except Exception:
        rows = []
    if rows:
        return [_enrich_row(r) for r in rows]

    norm_expr = "REPLACE(REPLACE(name_normalized, '-', ''), ' ', '')"
    likes = [f"%{t.replace('-', '').replace(' ', '')}%" for t in tokens]
    where = " AND ".join(f"{norm_expr} LIKE ?" for _ in likes)
    cur = conn.execute(
        f"SELECT {columns} FROM products WHERE {where} LIMIT ?",
        (*likes, limit),
    )
    return [_enrich_row(r) for r in cur.fetchall()]


def search_by_brand(conn: sqlite3.Connection, brand_norm: str, limit: int) -> list[dict]:
    """Pull up to `limit` products whose `brands` contains brand_norm as
    a comma-delimited token (case-insensitive). False-positive resilient."""
    bn = brand_norm.lower().strip()
    if not bn:
        return []
    columns = (
        "code, product_name, brands, nutriments, "
        "serving_size, serving_quantity, ingredients_text"
    )
    # Use a permissive LIKE for the SQL prefilter, then word-token-match
    # in Python to avoid substring collisions like "kind" inside "Mankind".
    cur = conn.execute(
        f"SELECT {columns} FROM products WHERE brands_normalized LIKE ? LIMIT ?",
        (f"%{bn}%", limit * 4),  # over-fetch since we'll filter
    )
    out: list[dict] = []
    for row in cur.fetchall():
        tokens = [t.strip().lower() for t in re.split(r"[,;]", row["brands"] or "")]
        if bn in tokens:
            out.append(_enrich_row(row))
        if len(out) >= limit:
            break
    return out


# --- Quality gates: brand / name / nutrition ------------------------------
#
# US-recognizable brand names. Used as a *boost* in product_quality_score,
# never as a hard filter — long-tail valid brands stay in.

US_BRAND_NAMES: tuple[str, ...] = (
    # Confectionery
    "mars", "snickers", "twix", "m&m's", "m&ms", "milky way",
    "hershey's", "hershey", "reese's", "reeses", "kit kat",
    "skittles", "starburst", "nestle", "ghirardelli",
    # Soft drinks / waters
    "coca-cola", "coke", "pepsi", "mountain dew", "dr pepper",
    "sprite", "fanta", "gatorade", "powerade", "vitaminwater",
    "vitamin water", "red bull", "monster", "rockstar", "celsius",
    "sunkist", "schweppes", "canada dry", "snapple", "arizona",
    # Cereal & breakfast
    "kellogg's", "kelloggs", "kellogg", "general mills", "post",
    "quaker", "kashi", "cheerios", "frosted flakes", "froot loops",
    "lucky charms", "wheaties", "raisin bran", "special k",
    # Chips / snacks
    "frito-lay", "frito lay", "lay's", "lays", "doritos", "tostitos",
    "ruffles", "pringles", "cheetos", "fritos", "sun chips",
    "sunchips", "stacy's", "popchips",
    # Dairy / yogurt
    "chobani", "yoplait", "dannon", "fage", "oikos",
    "stonyfield", "siggi's", "siggis",
    # Ice cream
    "ben & jerry's", "ben and jerry's", "haagen-dazs", "haagen dazs",
    "halo top", "talenti", "breyers", "edy's", "edys", "blue bunny",
    "klondike",
    # Bars
    "kind", "clif", "rxbar", "luna", "nature valley", "quest",
    "powerbar", "larabar", "perfect bar", "thinkthin",
    # Cookies / baked
    "oreo", "nabisco", "keebler", "pepperidge farm", "chips ahoy",
    "famous amos", "milano",
    # Crackers
    "cheez-it", "cheez it", "ritz", "triscuit", "wheat thins", "goldfish",
    # Drinks / juice
    "naked juice", "naked", "tropicana", "minute maid", "sunny d",
    "ocean spray", "welch's", "v8", "honest tea",
    # Other staples
    "kraft", "campbell's", "campbells", "heinz", "starbucks",
    "sara lee", "wonder bread",
    # Meal replacement / energy
    "soylent", "ensure", "muscle milk", "premier protein",
    "annie's", "annies", "amy's", "amys",
)

US_BRAND_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(r"\b" + re.escape(s) + r"\b", re.IGNORECASE)
    for s in US_BRAND_NAMES
)

# Brand strings that are just category descriptors. A candidate fails the
# gate only when *every* token in the brand field appears here, so mixed
# entries like "Coca-Cola, soda" still pass.
GENERIC_BRAND_TOKENS: frozenset[str] = frozenset({
    "smoothie", "yogurt", "yoghurt", "vitamin water", "vitaminwater",
    "green tea", "tea", "juice", "water", "milk", "soda", "cola",
    "granola", "cereal", "popcorn", "chips", "ice cream", "energy drink",
    "sports drink", "kombucha", "snack", "bar", "cookies", "crackers",
    "organic", "natural", "gluten free", "gluten-free", "low fat",
    "fat free", "diet", "lite", "light", "kids", "bio", "eco",
    "n/a", "none", "unknown", "various", "no brand", "brandless",
})

# Patterns stripped from the suggested display name so the cleaner starts
# from "Brand Product" rather than "Brand Product Original Family Size 12oz".
DISPLAY_SUFFIX_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bnet\s*wt\.?\s*[\d./\s]+\s*(?:oz|g|lb|ml|fl)?\b", re.IGNORECASE),
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:oz|g|lb|kg|ml|fl\s*oz|count|ct|pack)\b", re.IGNORECASE),
    re.compile(r"\b(?:family|sharing|value|party|king|fun)\s+size\b", re.IGNORECASE),
    re.compile(r"\boriginal\b", re.IGNORECASE),
    re.compile(r"\bnew!?\b", re.IGNORECASE),
)


def _brand_tokens(brand: str) -> list[str]:
    """Split brand on ,/; → lowercase tokens, empties stripped."""
    return [t.strip().lower() for t in re.split(r"[,;]", brand or "") if t.strip()]


def is_known_us_brand(brand: str) -> bool:
    if not brand:
        return False
    return any(p.search(brand) for p in US_BRAND_PATTERNS)


def is_only_generic_brand(brand: str) -> bool:
    """True iff every token in the brand field is a category descriptor.
    Returns False on empty (handled by a separate empty-brand check)."""
    tokens = _brand_tokens(brand)
    if not tokens:
        return False
    return all(t in GENERIC_BRAND_TOKENS for t in tokens)


def has_clean_name(name: str) -> bool:
    """Reject ingredient-dump style names without losing real products."""
    if not name:
        return False
    if len(name) > 120:
        return False
    sep_count = sum(name.count(s) for s in (",", "|", "/"))
    if sep_count > 3:
        return False
    return True


def product_quality_score(c: dict) -> int:
    """0-10 composite combining nutrition completeness, brand recognition,
    serving size, and name cleanliness. Used to sort candidates within a
    bucket and emitted to discovery_results.csv as a hint for the cleaner."""
    score = 0
    score += min(5, (c.get("completeness") or 0) // 2)
    if is_known_us_brand(c.get("brand") or ""):
        score += 2
    if c.get("serving_quantity") is not None:
        score += 1
    name = (c.get("name") or "").strip()
    if name and len(name) <= 60:
        score += 1
    if name and sum(name.count(s) for s in (",", "|", "/")) == 0:
        score += 1
    return max(0, min(10, score))


def has_usable_nutrition(c: dict) -> bool:
    """Pre-filter requiring the four backend-required fields plus
    sugars_100g. Discovery prefers products where sugar bullets are
    computable since most framings (sugar_shock, health_halo) lean on it."""
    nutri = c.get("nutriments") or {}
    required = (
        "energy-kcal_100g", "proteins_100g", "fat_100g",
        "carbohydrates_100g", "sugars_100g",
    )
    return all(nutri.get(k) is not None for k in required)


def has_strong_hurts(hurts: list[str]) -> bool:
    text = " ".join(hurts).lower()
    return any(k in text for k in (
        "sugar", "added", "ultra-processed", "ultra processed", "processed",
        "refined", "concentrated", "high",
    ))


# --- Reference resolution -------------------------------------------------

def resolve_reference(conn: sqlite3.Connection, ref_def: dict) -> Optional[dict]:
    """Try each barcode hint, then fall back to a name search. Returns the
    best (highest-completeness) match, or None."""
    for code in ref_def.get("barcodes") or []:
        product = lookup_product(conn, code)
        if product is not None:
            nutri = product.get("nutriments") or {}
            return {
                "code": product["code"],
                "name": product.get("name") or ref_def["name"],
                "brand": product.get("brand") or "",
                "completeness": _completeness(nutri),
                "nutriments": nutri,
                "serving_quantity": product.get("serving_quantity"),
                "ingredients_text": product.get("ingredients_text") or "",
            }
    name_search = ref_def.get("name_search") or ref_def["name"]
    candidates = [c for c in search_db(conn, name_search, limit=20) if has_usable_nutrition(c)]
    candidates.sort(key=lambda c: -c["completeness"])
    return candidates[0] if candidates else None


# --- Postability + interest gate ------------------------------------------

def is_interesting(cand_score: int, ref_score: int, framing: str) -> bool:
    """Spec rule:
      - candidate_score <= reference_score  → keep
      - abs(diff) <= 1                      → keep (close call)
      - candidate is health-halo + score≤5  → keep (exposure)
    """
    if cand_score <= ref_score:
        return True
    if abs(cand_score - ref_score) <= 1:
        return True
    if framing == "health_halo" and cand_score <= 5:
        return True
    return False


def discovery_postability(
    cand_score: int,
    ref_score: int,
    framing: str,
    completeness: int,
    strong_hurts: bool,
) -> int:
    score = 5
    if cand_score <= ref_score:
        score += 3
    elif abs(cand_score - ref_score) <= 1:
        score += 2
    if framing in ("kids_food", "sugar_shock"):
        score += 1
    if framing == "health_halo" and cand_score <= 5:
        score += 2
    if completeness >= 8:
        score += 1
    if strong_hurts:
        score += 1
    return max(1, min(10, score))


# --- Display name / label suggestions -------------------------------------

def suggest_display_name(raw: str, brand: str = "") -> str:
    """Trim a noisy OFF product_name into something a human will recognize.
    When `brand` is a known US brand and not already in the name, prepend
    it so the suggestion reads like 'Brand Product' (e.g. 'Kellogg's
    Frosted Flakes')."""
    if not raw:
        return (brand or "").strip()
    s = raw.strip()
    for sep in (",", " - ", " | ", " / ", " (", " ["):
        if sep in s:
            s = s.split(sep)[0].strip()
    for pat in DISPLAY_SUFFIX_PATTERNS:
        s = pat.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" -,")
    brand_first = (brand or "").split(",")[0].strip()
    if (brand_first
            and brand_first.lower() not in s.lower()
            and is_known_us_brand(brand_first)):
        s = f"{brand_first} {s}".strip()
    words = s.split()
    return " ".join(words[:6]) if len(words) > 6 else s


# --- CSV I/O --------------------------------------------------------------

CSV_COLUMNS = [
    "discovery_id",
    "candidate_name_raw", "candidate_brand", "candidate_barcode",
    "candidate_display_name_suggested", "candidate_label_suggested",
    "candidate_score", "candidate_interpretation",
    "candidate_what_helps", "candidate_what_hurts",
    "reference_name_raw", "reference_brand", "reference_barcode",
    "reference_display_name_suggested", "reference_label_suggested",
    "reference_score", "reference_interpretation",
    "score_diff", "purpose", "framing_type", "format",
    "content_angle", "postability_score", "product_quality_score",
    "status", "error_message",
]

CONSIDERED_COLUMNS = [
    "candidate_barcode", "reference_barcode",
    "outcome", "scored_at",
]


def load_existing_state(path: str) -> tuple[set[tuple[str, str]], int]:
    """Return (kept pairs, next discovery_id) from discovery_results.csv."""
    seen: set[tuple[str, str]] = set()
    next_id = 1
    p = Path(path)
    if not p.exists():
        return seen, next_id
    try:
        with open(p, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cand = (row.get("candidate_barcode") or "").strip()
                ref = (row.get("reference_barcode") or "").strip()
                if cand and ref:
                    seen.add((cand, ref))
                try:
                    rid = int(row.get("discovery_id") or 0)
                    if rid >= next_id:
                        next_id = rid + 1
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        print(f"WARN: failed to load existing {path}: {e}", file=sys.stderr)
    return seen, next_id


def load_considered_pairs(path: str) -> set[tuple[str, str]]:
    """Pairs we've already scored (kept, dropped, OR errored). Skipped on
    re-runs so weekly cadence advances naturally to fresh candidates."""
    seen: set[tuple[str, str]] = set()
    p = Path(path)
    if not p.exists():
        return seen
    try:
        with open(p, newline="") as f:
            for row in csv.DictReader(f):
                cand = (row.get("candidate_barcode") or "").strip()
                ref = (row.get("reference_barcode") or "").strip()
                if cand and ref:
                    seen.add((cand, ref))
    except Exception as e:
        print(f"WARN: failed to load {path}: {e}", file=sys.stderr)
    return seen


def append_considered(path: str, pairs: list[dict]) -> None:
    if not pairs:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    file_exists = Path(path).exists()
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CONSIDERED_COLUMNS)
        if not file_exists:
            writer.writeheader()
        for p in pairs:
            writer.writerow({k: p.get(k, "") for k in CONSIDERED_COLUMNS})


def append_rows(path: str, rows: list[dict]) -> None:
    if not rows:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    file_exists = Path(path).exists()
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if not file_exists:
            writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in CSV_COLUMNS})


def _exposure_postability(cand_score: int, framing: str, completeness: int, strong: bool) -> int:
    score = 5
    if cand_score <= 4:
        score += 3
    elif cand_score <= 5:
        score += 2
    if framing in ("kids_food", "sugar_shock"):
        score += 1
    if completeness >= 8:
        score += 1
    if strong:
        score += 1
    return max(1, min(10, score))


def _swap_postability(cand_score: int, alt_score: int, framing: str,
                      completeness: int, strong: bool) -> int:
    score = 5
    diff = alt_score - cand_score  # positive = alternative is better
    if diff >= 4:
        score += 3
    elif diff >= 2:
        score += 2
    elif diff >= 1:
        score += 1
    if framing in ("kids_food", "sugar_shock"):
        score += 1
    if completeness >= 8:
        score += 1
    if strong:
        score += 1
    return max(1, min(10, score))


def build_comparison_row(
    discovery_id: int,
    cand: dict,
    ref: dict,
    cand_score: int,
    cand_interp: str,
    cand_helps: list[str],
    cand_hurts: list[str],
    ref_score: int,
    ref_interp: str,
    bucket: dict,
) -> dict:
    framing = bucket["framing"]
    completeness = cand.get("completeness", 0)
    strong = has_strong_hurts(cand_hurts)
    postability = discovery_postability(
        cand_score, ref_score, framing, completeness, strong
    )
    return {
        "discovery_id": discovery_id,
        "candidate_name_raw": cand["name"],
        "candidate_brand": cand["brand"] or "",
        "candidate_barcode": cand["code"],
        "candidate_display_name_suggested": suggest_display_name(cand["name"], cand.get("brand", "")),
        "candidate_label_suggested": bucket["label_hint"],
        "candidate_score": cand_score,
        "candidate_interpretation": cand_interp,
        "candidate_what_helps": " ; ".join(cand_helps),
        "candidate_what_hurts": " ; ".join(cand_hurts),
        "reference_name_raw": ref["name"],
        "reference_brand": ref.get("brand") or "",
        "reference_barcode": ref["code"],
        "reference_display_name_suggested": REFERENCE_FOODS.get(
            bucket.get("ref", "snickers"), {}
        ).get("display_name") or ref["name"],
        "reference_label_suggested": REFERENCE_FOODS.get(
            bucket.get("ref", "snickers"), {}
        ).get("label") or "",
        "reference_score": ref_score,
        "reference_interpretation": ref_interp,
        "score_diff": cand_score - ref_score,
        "purpose": bucket["purpose"],
        "framing_type": framing,
        "format": "comparison",
        "content_angle": bucket["angle"],
        "postability_score": postability,
        "product_quality_score": product_quality_score(cand),
        "status": "pending",
        "error_message": "",
    }


# Backwards-compat alias — older code may still import build_row.
build_row = build_comparison_row


def build_exposure_row(
    discovery_id: int,
    cand: dict,
    cand_score: int,
    cand_interp: str,
    cand_helps: list[str],
    cand_hurts: list[str],
    bucket: dict,
) -> dict:
    """Single-item exposure row. Reference fields are blank by design —
    the renderer's exposure layout is food_a-only."""
    framing = bucket["framing"]
    completeness = cand.get("completeness", 0)
    strong = has_strong_hurts(cand_hurts)
    postability = _exposure_postability(cand_score, framing, completeness, strong)
    return {
        "discovery_id": discovery_id,
        "candidate_name_raw": cand["name"],
        "candidate_brand": cand["brand"] or "",
        "candidate_barcode": cand["code"],
        "candidate_display_name_suggested": suggest_display_name(cand["name"], cand.get("brand", "")),
        "candidate_label_suggested": bucket["label_hint"],
        "candidate_score": cand_score,
        "candidate_interpretation": cand_interp,
        "candidate_what_helps": " ; ".join(cand_helps),
        "candidate_what_hurts": " ; ".join(cand_hurts),
        "reference_name_raw": "",
        "reference_brand": "",
        "reference_barcode": "",
        "reference_display_name_suggested": "",
        "reference_label_suggested": "",
        "reference_score": "",
        "reference_interpretation": "",
        "score_diff": "",
        "purpose": bucket["purpose"],
        "framing_type": framing,
        "format": "exposure",
        "content_angle": bucket["angle"],
        "postability_score": postability,
        "product_quality_score": product_quality_score(cand),
        "status": "pending",
        "error_message": "",
    }


def build_swap_row(
    discovery_id: int,
    cand: dict,
    alt: dict,
    alt_def: dict,
    cand_score: int,
    cand_interp: str,
    cand_helps: list[str],
    cand_hurts: list[str],
    bucket: dict,
) -> dict:
    """Swap row: food_a = the candidate (swap OUT), food_b = the
    alternative from ALTERNATIVE_FOODS (PICK THIS)."""
    framing = bucket["framing"]
    completeness = cand.get("completeness", 0)
    strong = has_strong_hurts(cand_hurts)
    alt_score = alt["score"]
    postability = _swap_postability(cand_score, alt_score, framing, completeness, strong)
    return {
        "discovery_id": discovery_id,
        "candidate_name_raw": cand["name"],
        "candidate_brand": cand["brand"] or "",
        "candidate_barcode": cand["code"],
        "candidate_display_name_suggested": suggest_display_name(cand["name"], cand.get("brand", "")),
        "candidate_label_suggested": bucket["label_hint"],
        "candidate_score": cand_score,
        "candidate_interpretation": cand_interp,
        "candidate_what_helps": " ; ".join(cand_helps),
        "candidate_what_hurts": " ; ".join(cand_hurts),
        "reference_name_raw": alt["name"],
        "reference_brand": alt.get("brand") or "",
        "reference_barcode": alt["code"],
        "reference_display_name_suggested": alt_def.get("display_name") or alt["name"],
        "reference_label_suggested": alt_def.get("label") or "",
        "reference_score": alt_score,
        "reference_interpretation": alt.get("interpretation", ""),
        "score_diff": cand_score - alt_score,  # negative — candidate is worse
        "purpose": bucket["purpose"],
        "framing_type": framing,
        "format": "swap",
        "content_angle": bucket["angle"] + " (swap)",
        "postability_score": postability,
        "product_quality_score": product_quality_score(cand),
        "status": "pending",
        "error_message": "",
    }


# --- Bucket processing ----------------------------------------------------

def process_bucket(
    *,
    conn,
    backend: str,
    bucket: dict,
    references: dict,
    alternatives: dict,
    seen_pairs: set,
    next_id: int,
    top: int,
    max_raw: int,
    is_brand: bool,
    dry_run: bool,
) -> tuple[list[dict], list[dict], int]:
    """Score `top` fresh candidates from this bucket and emit comparison /
    exposure / swap rows according to per-format eligibility rules.

    Returns (kept_rows, considered_pairs_to_log, new_next_id).
    """
    ref_key = bucket.get("ref") or "snickers"
    ref = references.get(ref_key) or references.get("snickers")
    if ref is None:
        print(f"\n[{bucket['key']}] SKIP (no reference)")
        return [], [], next_id

    # Bucket-level eligibility for the optional formats.
    exposure_eligible = bucket["framing"] in EXPOSURE_FRAMINGS
    alt_key = BUCKET_ALTERNATIVE.get(bucket["key"])
    alt = alternatives.get(alt_key) if alt_key else None
    swap_eligible = alt is not None  # bucket maps to an alternative AND it resolved+scored

    label = "brand" if is_brand else "bucket"
    extras = []
    if exposure_eligible:
        extras.append("exposure")
    if swap_eligible:
        extras.append(f"swap→{alt_key}")
    extras_str = f" (+{', '.join(extras)})" if extras else ""
    print(f"\n[{label}: {bucket['key']}] vs {ref_key}{extras_str}")

    if is_brand:
        unique = search_by_brand(conn, bucket["brand_norm"], limit=max_raw)
    else:
        all_candidates: list[dict] = []
        for q in bucket["queries"]:
            all_candidates.extend(search_db(conn, q, limit=max_raw))
        seen_codes: set[str] = set()
        unique = []
        for c in all_candidates:
            if c["code"] in seen_codes:
                continue
            seen_codes.add(c["code"])
            unique.append(c)

    n_no_nutri = n_no_brand = n_generic_brand = n_bad_name = 0
    usable: list[dict] = []
    for c in unique:
        if not has_usable_nutrition(c):
            n_no_nutri += 1
            continue
        brand = (c.get("brand") or "").strip()
        if not brand:
            n_no_brand += 1
            continue
        if is_only_generic_brand(brand):
            n_generic_brand += 1
            continue
        if not has_clean_name(c.get("name") or ""):
            n_bad_name += 1
            continue
        usable.append(c)
    usable.sort(key=lambda c: (
        -product_quality_score(c),
        -(c.get("completeness") or 0),
    ))

    # A candidate is "fresh" if it has at least one applicable-format pair
    # we haven't considered yet. This way, a candidate that's been
    # comparison-evaluated but never exposure-evaluated (e.g. because the
    # spec changed) can still get a new exposure row.
    def _applicable_pairs(code: str) -> list[tuple[str, str]]:
        pairs = [(code, ref["code"])]
        if exposure_eligible:
            pairs.append((code, EXPOSURE_MARKER))
        if swap_eligible:
            pairs.append((code, alt["code"]))
        return pairs

    fresh = [
        c for c in usable
        if any(p not in seen_pairs for p in _applicable_pairs(c["code"]))
    ]
    skipped_old = len(usable) - len(fresh)
    fresh = fresh[:top]

    print(f"  {len(unique)} matches · {len(usable)} usable · "
          f"{skipped_old} already considered · scoring {len(fresh)}")
    print(f"    dropped → no-nutri={n_no_nutri} no-brand={n_no_brand} "
          f"generic-brand={n_generic_brand} bad-name={n_bad_name}")

    if dry_run:
        for c in fresh:
            print(f"  [dry-run]   q={product_quality_score(c)} comp={c['completeness']} "
                  f"{c['name'][:40]:<40} [{(c.get('brand') or '—')[:25]}]")
        return [], [], next_id

    kept_rows: list[dict] = []
    considered: list[dict] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for cand in fresh:
        score, interp, helps, hurts, err = score_food(
            conn, backend, cand["name"], cand["code"], bucket["purpose"],
        )
        if err or score is None:
            # Don't mark errors as considered — let weekly retries try again.
            print(f"  [score-err] {cand['name'][:50]}: {err}")
            continue

        cmp_pair = (cand["code"], ref["code"])
        exp_pair = (cand["code"], EXPOSURE_MARKER) if exposure_eligible else None
        swap_pair = (cand["code"], alt["code"]) if swap_eligible else None

        emitted: list[str] = []

        # Comparison row (the primary output).
        if cmp_pair not in seen_pairs:
            if is_interesting(score, ref["score"], bucket["framing"]):
                row = build_comparison_row(
                    next_id, cand, ref, score, interp, helps, hurts,
                    ref["score"], ref["interpretation"], bucket,
                )
                kept_rows.append(row)
                next_id += 1
                considered.append({
                    "candidate_barcode": cand["code"],
                    "reference_barcode": ref["code"],
                    "outcome": "kept",
                    "scored_at": now,
                })
                emitted.append(f"cmp(diff={row['score_diff']:+d} post={row['postability_score']})")
            else:
                considered.append({
                    "candidate_barcode": cand["code"],
                    "reference_barcode": ref["code"],
                    "outcome": "dropped",
                    "scored_at": now,
                })
            seen_pairs.add(cmp_pair)

        # Exposure row (if bucket framing qualifies and score is low enough).
        if exp_pair and exp_pair not in seen_pairs:
            if score <= 5:
                row = build_exposure_row(
                    next_id, cand, score, interp, helps, hurts, bucket,
                )
                kept_rows.append(row)
                next_id += 1
                considered.append({
                    "candidate_barcode": cand["code"],
                    "reference_barcode": EXPOSURE_MARKER,
                    "outcome": "kept",
                    "scored_at": now,
                })
                emitted.append(f"exp(post={row['postability_score']})")
            else:
                considered.append({
                    "candidate_barcode": cand["code"],
                    "reference_barcode": EXPOSURE_MARKER,
                    "outcome": "dropped",
                    "scored_at": now,
                })
            seen_pairs.add(exp_pair)

        # Swap row (curated buckets only; cand poor + alt clearly better).
        if swap_pair and swap_pair not in seen_pairs:
            alt_score = alt["score"]
            if score <= 6 and alt_score >= score + 2:
                row = build_swap_row(
                    next_id, cand, alt, ALTERNATIVE_FOODS[alt_key],
                    score, interp, helps, hurts, bucket,
                )
                kept_rows.append(row)
                next_id += 1
                considered.append({
                    "candidate_barcode": cand["code"],
                    "reference_barcode": alt["code"],
                    "outcome": "kept",
                    "scored_at": now,
                })
                emitted.append(f"swap→{alt_key}(post={row['postability_score']})")
            else:
                considered.append({
                    "candidate_barcode": cand["code"],
                    "reference_barcode": alt["code"],
                    "outcome": "dropped",
                    "scored_at": now,
                })
            seen_pairs.add(swap_pair)

        if emitted:
            print(f"  [keep]      {cand['name'][:50]} cand={score} ref={ref['score']} "
                  f"→ {' + '.join(emitted)}")
        else:
            print(f"  [drop]      {cand['name'][:50]} cand={score} ref={ref['score']}")

    return kept_rows, considered, next_id


# --- Main -----------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Discover new SmarterEats comparison candidates from OFF.",
    )
    p.add_argument("--top", type=int, default=DEFAULT_TOP,
                   help=f"candidates scored per bucket per run (default {DEFAULT_TOP})")
    p.add_argument("--max-raw", type=int, default=DEFAULT_MAX_RAW,
                   help=f"raw matches pulled per query (default {DEFAULT_MAX_RAW})")
    p.add_argument("--bucket", type=str, default=None,
                   help="run only this bucket key (e.g. 'yogurt', 'brand_kind')")
    p.add_argument("--brands", action="store_true",
                   help="also run brand-driven discovery (BRAND_BUCKETS)")
    p.add_argument("--brands-only", action="store_true",
                   help="run only brand-driven discovery, skip CANDIDATE_BUCKETS")
    p.add_argument("--dry-run", action="store_true",
                   help="print the candidate slate without calling the backend")
    p.add_argument("--reset", action="store_true",
                   help=f"clear {CONSIDERED_LOG} before running")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    backend = os.environ.get("BACKEND_BASE_URL")
    if not backend and not args.dry_run:
        print("ERROR: BACKEND_BASE_URL env var is required (use --dry-run to skip)",
              file=sys.stderr)
        return 2

    db_path = os.environ.get("FOOD_DB_PATH", "./off_products.db")
    try:
        conn = open_db(db_path)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.reset and Path(CONSIDERED_LOG).exists():
        Path(CONSIDERED_LOG).unlink()
        print(f"Reset: removed {CONSIDERED_LOG}")

    kept_pairs, next_id = load_existing_state(OUTPUT_CSV)
    considered_pairs = load_considered_pairs(CONSIDERED_LOG)
    # Considered = anything we've ever scored (kept OR dropped). Kept pairs
    # are also in here implicitly once we record them, but old runs may not
    # have populated considered.csv — union the two for safety.
    seen_pairs = considered_pairs | kept_pairs

    print("=== Discovery batch ===")
    print(f"DB:        {db_path}")
    print(f"Backend:   {backend or '(dry-run)'}")
    print(f"Output:    {OUTPUT_CSV}")
    print(f"Considered log: {CONSIDERED_LOG}")
    print(f"Top per bucket: {args.top} · max raw per query: {args.max_raw}")
    print(f"Existing kept: {len(kept_pairs)} · already-considered: {len(considered_pairs)} · "
          f"next discovery_id: {next_id}")

    # 1) Resolve + score reference foods (cached for the whole run).
    print("\n--- Reference foods ---")
    references: dict[str, dict] = {}
    for ref_key, ref_def in REFERENCE_FOODS.items():
        ref = resolve_reference(conn, ref_def)
        if ref is None:
            print(f"  [{ref_key:<15}] NOT FOUND")
            continue
        if args.dry_run:
            references[ref_key] = {**ref, "score": 0, "interpretation": "",
                                   "helps": [], "hurts": []}
            print(f"  [{ref_key:<15}] {ref['name'][:40]:<40} {ref['code']} (dry-run)")
            continue
        purpose = "breakfast" if ref_key == "frosted_flakes" else "snack"
        score, interp, helps, hurts, err = score_food(
            conn, backend, ref["name"], ref["code"], purpose,
        )
        if err or score is None:
            print(f"  [{ref_key:<15}] score failed: {err}")
            continue
        references[ref_key] = {
            **ref, "score": score, "interpretation": interp,
            "helps": helps, "hurts": hurts,
        }
        print(f"  [{ref_key:<15}] {ref['name'][:40]:<40} {ref['code']} → {score}/10")

    if not references:
        print("\nERROR: no references resolved/scored. Aborting.", file=sys.stderr)
        return 2

    # 1b) Resolve + score alternatives used by swap rows. Curated, small set.
    print("\n--- Alternatives (for swap rows) ---")
    alternatives: dict[str, dict] = {}
    for alt_key, alt_def in ALTERNATIVE_FOODS.items():
        alt = resolve_reference(conn, alt_def)
        if alt is None:
            print(f"  [{alt_key:<22}] NOT FOUND — swap rows for buckets using "
                  f"this alternative will be skipped")
            continue
        if args.dry_run:
            alternatives[alt_key] = {**alt, "score": 0, "interpretation": "",
                                     "helps": [], "hurts": []}
            print(f"  [{alt_key:<22}] {alt['name'][:35]:<35} {alt['code']} (dry-run)")
            continue
        purpose = ALT_PURPOSE.get(alt_key, "snack")
        score, interp, helps, hurts, err = score_food(
            conn, backend, alt["name"], alt["code"], purpose,
        )
        if err or score is None:
            print(f"  [{alt_key:<22}] score failed: {err}")
            continue
        alternatives[alt_key] = {
            **alt, "score": score, "interpretation": interp,
            "helps": helps, "hurts": hurts,
        }
        print(f"  [{alt_key:<22}] {alt['name'][:35]:<35} {alt['code']} → {score}/10")

    # 2) Build the bucket list per the flags.
    buckets_to_run: list[tuple[dict, bool]] = []  # (bucket, is_brand)
    if not args.brands_only:
        for b in CANDIDATE_BUCKETS:
            buckets_to_run.append((b, False))
    if args.brands or args.brands_only:
        for b in BRAND_BUCKETS:
            buckets_to_run.append((b, True))

    if args.bucket:
        buckets_to_run = [(b, ib) for (b, ib) in buckets_to_run if b["key"] == args.bucket]
        if not buckets_to_run:
            print(f"\nERROR: --bucket '{args.bucket}' did not match any bucket key",
                  file=sys.stderr)
            return 2

    print(f"\n--- Running {len(buckets_to_run)} bucket(s) ---")

    # 3) Walk buckets.
    total_kept = 0
    total_considered = 0

    for bucket, is_brand in buckets_to_run:
        kept, considered, next_id = process_bucket(
            conn=conn,
            backend=backend or "",
            bucket=bucket,
            references=references,
            alternatives=alternatives,
            seen_pairs=seen_pairs,
            next_id=next_id,
            top=args.top,
            max_raw=args.max_raw,
            is_brand=is_brand,
            dry_run=args.dry_run,
        )

        # Persist per-bucket so a Ctrl-C mid-run doesn't lose prior buckets.
        if not args.dry_run:
            if kept:
                append_rows(OUTPUT_CSV, kept)
            if considered:
                append_considered(CONSIDERED_LOG, considered)

        total_kept += len(kept)
        total_considered += len(considered)
        # Update local seen with the considered ones too.
        for c in considered:
            seen_pairs.add((c["candidate_barcode"], c["reference_barcode"]))

    print("\n=== Done ===")
    print(f"Considered: {total_considered} · Kept: {total_kept}")
    if args.dry_run:
        print("Dry-run: no rows were written.")
    elif total_kept:
        print(f"Wrote to {OUTPUT_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
