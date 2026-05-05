"""SmarterEats input.csv builder.

A local Streamlit helper to build clean input.csv rows from the OFF
database without any backend calls, scoring, or rendering.

Setup (one-time):
    pip install -r requirements-builder.txt

Run from the project root:
    streamlit run input_builder.py

The DB path defaults to ./off_products.db; set FOOD_DB_PATH to override.
The Streamlit app fetches product images and "last updated" dates from
the OFF API on demand (cached). Toggle the sidebar switch off to run
offline.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
import streamlit as st


DEFAULT_DB_PATH = "./off_products.db"
INPUT_CSV = "input.csv"

# Canonical column order written to input.csv.
CSV_HEADER = [
    "pair_id", "format",
    "food_a_name", "food_a_display_name", "food_a_label", "food_a_barcode",
    "food_b_name", "food_b_display_name", "food_b_label", "food_b_barcode",
    "purpose", "framing_type",
]

FORMATS = ["comparison", "exposure", "swap"]
FRAMINGS = ["health_halo", "kids_food", "sugar_shock", "swap", "default"]
PURPOSES = ["snack", "meal", "post_workout", "treat", "convenience", "ingredient"]

# Nutrient keys we consider for the completeness score.
_COMPLETENESS_KEYS = (
    "energy-kcal_100g", "proteins_100g", "fat_100g", "carbohydrates_100g",
    "sugars_100g", "fiber_100g", "saturated-fat_100g",
    "sodium_100g", "added-sugars_100g",
)

# OFF image/meta endpoint. The OFF API requires a descriptive User-Agent
# or it returns 403; this header identifies the local utility.
_OFF_API = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
_OFF_HEADERS = {
    "User-Agent": (
        "smartereats-input-builder/0.1 "
        "(https://github.com/forant/smartereats-content-engine)"
    ),
}


# --- DB --------------------------------------------------------------------

def _db_path() -> str:
    return os.environ.get("FOOD_DB_PATH", DEFAULT_DB_PATH)


@st.cache_resource
def _connect():
    path = _db_path()
    if not Path(path).exists():
        return None
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_nutri(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _per_serving(nutri: dict, key_100g: str, serving_g: Optional[float]) -> Optional[float]:
    """Return per-serving value, preferring backend's _serving variant."""
    if not nutri:
        return None
    serving_key = key_100g.replace("_100g", "_serving")
    raw = nutri.get(serving_key)
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    per_100 = nutri.get(key_100g)
    if per_100 is None or serving_g is None:
        return None
    try:
        return float(per_100) * float(serving_g) / 100.0
    except (TypeError, ValueError):
        return None


def _completeness(nutri: dict) -> int:
    if not nutri:
        return 0
    have = sum(1 for k in _COMPLETENESS_KEYS if nutri.get(k) is not None)
    return min(10, round(have * 10 / len(_COMPLETENESS_KEYS)))


def _enrich_row(row: sqlite3.Row) -> dict:
    """Convert a DB row into a dict with computed per-serving fields."""
    nutri = _parse_nutri(row["nutriments"])
    serving_g = row["serving_quantity"]
    try:
        serving_g_f = float(serving_g) if serving_g is not None else None
    except (TypeError, ValueError):
        serving_g_f = None
    ingredients = (row["ingredients_text"] or "").strip()
    return {
        "code": row["code"],
        "name": row["product_name"] or "",
        "brand": row["brands"] or "",
        "serving_size": row["serving_size"] or "",
        "serving_quantity": serving_g_f,
        "ingredients_text": ingredients,
        "has_ingredients": bool(ingredients),
        "nutriments": nutri,
        "calories_serving": _per_serving(nutri, "energy-kcal_100g", serving_g_f),
        "sugar_serving": _per_serving(nutri, "sugars_100g", serving_g_f),
        "protein_serving": _per_serving(nutri, "proteins_100g", serving_g_f),
        "fiber_serving": _per_serving(nutri, "fiber_100g", serving_g_f),
        "completeness": _completeness(nutri),
        # Filled in later by OFF API enrichment if enabled.
        "image_url": None,
        "last_modified": "",
    }


def search_products(conn, query: str, limit: int = 12) -> list[dict]:
    """Search OFF products by name. FTS first, fall back to space/hyphen-
    tolerant LIKE. Returns enriched dicts with nutrition + completeness."""
    q = query.strip()
    if not q:
        return []
    tokens = [t for t in q.lower().split() if t]
    if not tokens:
        return []

    columns = """
        code, product_name, brands, nutriments, serving_size,
        serving_quantity, ingredients_text
    """
    fts_query = " ".join(tokens)
    fts_rows = []
    try:
        cursor = conn.execute(
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
        fts_rows = list(cursor.fetchall())
    except Exception:
        fts_rows = []

    if fts_rows:
        return [_enrich_row(r) for r in fts_rows]

    norm_expr = "REPLACE(REPLACE(name_normalized, '-', ''), ' ', '')"
    likes = [f"%{t.replace('-', '').replace(' ', '')}%" for t in tokens]
    where = " AND ".join(f"{norm_expr} LIKE ?" for _ in likes)
    cursor = conn.execute(
        f"SELECT {columns} FROM products WHERE {where} LIMIT ?",
        (*likes, limit),
    )
    return [_enrich_row(r) for r in cursor.fetchall()]


# --- OFF API enrichment (image + last_modified) ----------------------------

@st.cache_data(ttl=86400, show_spinner=False)
def _fetch_off_meta(code: str) -> dict:
    """Fetch image_front_small_url + last_modified_t for one barcode.
    Cached for 24h. Returns {} on any failure."""
    if not code:
        return {}
    try:
        resp = requests.get(
            _OFF_API.format(code=code),
            params={"fields": "image_front_small_url,image_small_url,last_modified_t"},
            headers=_OFF_HEADERS,
            timeout=4,
        )
        if resp.status_code != 200:
            return {}
        data = resp.json()
        prod = (data or {}).get("product") or {}
    except Exception:
        return {}

    image_url = prod.get("image_front_small_url") or prod.get("image_small_url") or None

    last_str = ""
    last_t = prod.get("last_modified_t")
    if last_t:
        try:
            last_str = datetime.fromtimestamp(int(last_t), tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            pass

    return {"image_url": image_url, "last_modified": last_str}


def _enrich_with_off(results: list[dict], parallel: int = 6) -> None:
    """Mutate results in place with OFF image_url + last_modified."""
    if not results:
        return
    codes = [r["code"] for r in results]

    def _go(code):
        try:
            return code, _fetch_off_meta(code)
        except Exception:
            return code, {}

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        for code, meta in ex.map(_go, codes):
            for r in results:
                if r["code"] == code:
                    r["image_url"] = meta.get("image_url")
                    r["last_modified"] = meta.get("last_modified", "")
                    break


# --- Sorting ---------------------------------------------------------------

def _normalize(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())


def _exact_rank(query: str, name: str, brand: str) -> int:
    q, n, b = _normalize(query), _normalize(name), _normalize(brand)
    if q == n or q == b:
        return 0
    if n.startswith(q) or b.startswith(q):
        return 1
    if q in n or q in b:
        return 2
    return 3


def _sort_results(results: list[dict], query: str) -> list[dict]:
    return sorted(
        results,
        key=lambda r: (
            _exact_rank(query, r["name"], r["brand"]),
            -r["completeness"],
            0 if r.get("serving_quantity") else 1,
            0 if r.get("image_url") else 1,
        ),
    )


# --- CSV I/O ---------------------------------------------------------------

def load_csv() -> pd.DataFrame:
    p = Path(INPUT_CSV)
    if not p.exists():
        return pd.DataFrame(columns=CSV_HEADER)
    try:
        return pd.read_csv(p, dtype=str, keep_default_na=False)
    except Exception:
        return pd.DataFrame(columns=CSV_HEADER)


def next_pair_id(df: pd.DataFrame) -> int:
    if df.empty or "pair_id" not in df.columns:
        return 1
    max_id = 0
    for v in df["pair_id"]:
        try:
            max_id = max(max_id, int(v))
        except (ValueError, TypeError):
            pass
    return max_id + 1


def append_row(row: dict) -> None:
    df = load_csv()
    for col in CSV_HEADER:
        if col not in df.columns:
            df[col] = ""
    new_df = pd.DataFrame([{c: row.get(c, "") for c in df.columns}])
    out = pd.concat([df, new_df], ignore_index=True)
    keep = [c for c in CSV_HEADER if c in out.columns] + [
        c for c in out.columns if c not in CSV_HEADER
    ]
    out[keep].to_csv(INPUT_CSV, index=False)


# --- Helpers ---------------------------------------------------------------

def default_display_name(raw: str) -> str:
    if not raw:
        return ""
    s = raw.strip()
    for sep in (",", " - ", " | ", " / ", " (", " ["):
        if sep in s:
            s = s.split(sep)[0].strip()
    words = s.split()
    return " ".join(words[:5]) if len(words) > 5 else s


def _fmt_g(value: Optional[float], precision: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{precision}f}g"


def _fmt_kcal(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.0f} kcal"


def _serving_summary(r: dict) -> str:
    sg = r.get("serving_quantity")
    raw = (r.get("serving_size") or "").strip()
    if sg:
        return f"{sg:.0f}g serving" + (f" ({raw})" if raw and str(int(sg)) not in raw else "")
    if raw:
        return f"Serving: {raw}"
    return "Serving: —"


def _missing_flags(r: dict) -> list[str]:
    flags: list[str] = []
    if not r.get("serving_quantity"):
        flags.append("serving size")
    if r.get("calories_serving") is None:
        flags.append("calories")
    if r.get("sugar_serving") is None:
        flags.append("sugar")
    if r.get("protein_serving") is None:
        flags.append("protein")
    if not r.get("has_ingredients"):
        flags.append("ingredients")
    return flags


# --- Per-result card -------------------------------------------------------

def _render_result(r: dict, side: str, slot_key: str) -> None:
    """Render one search-result card and a 'Select' button.
    side is 'a' or 'b'. slot_key disambiguates button keys per session."""
    with st.container(border=True):
        cols = st.columns([1, 5, 2])

        with cols[0]:
            if r.get("image_url"):
                try:
                    st.image(r["image_url"], width=80)
                except Exception:
                    st.caption("📦")
            else:
                st.caption("📦")

        with cols[1]:
            st.markdown(f"**{r['name'] or '(unnamed)'}**")
            st.caption(f"{r['brand'] or '—'}  ·  `{r['code']}`")

            parts = [_serving_summary(r)]
            if r.get("calories_serving") is not None:
                parts.append(_fmt_kcal(r["calories_serving"]))
            if r.get("sugar_serving") is not None:
                parts.append(f"{r['sugar_serving']:.1f}g sugar")
            if r.get("protein_serving") is not None:
                parts.append(f"{r['protein_serving']:.1f}g protein")
            if r.get("fiber_serving") is not None:
                parts.append(f"{r['fiber_serving']:.1f}g fiber")
            st.markdown("  ·  ".join(parts))

            ingredients_mark = "✓ ingredients" if r["has_ingredients"] else "✗ ingredients"
            updated = r.get("last_modified") or ""
            tail = ingredients_mark
            if updated:
                tail += f"  ·  updated {updated}"
            st.caption(tail)

            flags = _missing_flags(r)
            if flags:
                st.caption(f"⚠️ Missing: {', '.join(flags)}")

        with cols[2]:
            st.metric("Completeness", f"{r['completeness']}/10")
            if st.button("Select", key=f"sel_{side}_{slot_key}_{r['code']}", use_container_width=True):
                st.session_state[f"selected_{side}"] = r
                st.rerun()


# --- Manual Search Mode (existing) -----------------------------------------

def _render_picker(conn, side: str, label: str, *, fetch_meta: bool, result_limit: int) -> Optional[dict]:
    """Render search input + result cards for one side. Returns the selected
    dict (also kept in session_state)."""
    st.subheader(label)
    selected_key = f"selected_{side}"
    selected = st.session_state.get(selected_key)

    # If a product is already selected, show a compact "currently selected"
    # bar with a clear button — keeps the form quiet between searches.
    if selected:
        with st.container(border=True):
            sc1, sc2, sc3 = st.columns([1, 6, 2])
            with sc1:
                if selected.get("image_url"):
                    try:
                        st.image(selected["image_url"], width=64)
                    except Exception:
                        st.caption("📦")
                else:
                    st.caption("📦")
            with sc2:
                st.markdown(f"**Selected:** {selected['name']}")
                st.caption(f"{selected['brand'] or '—'}  ·  `{selected['code']}`")
            with sc3:
                if st.button("Clear", key=f"clear_{side}", use_container_width=True):
                    st.session_state.pop(selected_key, None)
                    st.rerun()

    query = st.text_input(
        f"Search OFF for {label}",
        key=f"search_{side}",
        placeholder="e.g. 'gogurt strawberry', 'snickers', 'naked green machine'",
    )
    if not query:
        return selected

    with st.spinner("Searching…"):
        results = search_products(conn, query, limit=result_limit)
    if not results:
        st.warning("No matches in OFF.")
        return selected

    if fetch_meta:
        with st.spinner("Fetching images + dates…"):
            _enrich_with_off(results)

    sorted_results = _sort_results(results, query)

    st.caption(f"{len(sorted_results)} result(s) · sorted by relevance, then completeness")
    for r in sorted_results:
        _render_result(r, side=side, slot_key=str(hash(query)))

    return st.session_state.get(selected_key)


def render_manual_mode(conn, *, fetch_meta: bool, result_limit: int) -> None:
    # Top settings.
    c1, c2, c3 = st.columns(3)
    with c1:
        fmt = st.selectbox("Format", FORMATS, index=0)
    with c2:
        framing = st.selectbox("Framing type", FRAMINGS, index=0)
    with c3:
        purpose = st.selectbox("Purpose", PURPOSES, index=0)

    needs_food_b = fmt in ("comparison", "swap")

    st.divider()

    food_a = _render_picker(conn, "a", "Food A",
                            fetch_meta=fetch_meta, result_limit=result_limit)

    food_b = None
    if needs_food_b:
        st.divider()
        food_b = _render_picker(conn, "b", "Food B",
                                fetch_meta=fetch_meta, result_limit=result_limit)
    else:
        st.caption("Food B is not needed for the `exposure` format.")
        st.session_state.pop("selected_b", None)

    st.divider()

    # Preview / edit.
    st.subheader("Card Preview Data")
    ready = (food_a is not None) and ((not needs_food_b) or (food_b is not None))

    if not ready:
        if food_a is None:
            st.info("Search and pick a Food A above.")
        elif needs_food_b:
            st.info("Search and pick a Food B above.")
        return

    cols = st.columns(2 if needs_food_b else 1)
    with cols[0]:
        st.markdown("**Food A — DB record**")
        st.text_input("Raw product name (read-only)",
                      value=food_a["name"], disabled=True, key="ro_a_name")
        st.text_input("Brand (read-only)",
                      value=food_a["brand"] or "—", disabled=True, key="ro_a_brand")
        st.text_input("Barcode (read-only)",
                      value=food_a["code"], disabled=True, key="ro_a_code")
        st.markdown("**Food A — what gets saved**")
        a_name = st.text_input("food_a_name", value=food_a["name"], key="a_name")
        a_display = st.text_input(
            "food_a_display_name (shown on card)",
            value=default_display_name(food_a["name"]),
            key="a_display",
        )
        a_label = st.text_input(
            "food_a_label (used in headlines)",
            value="",
            placeholder="e.g. 'kids yogurt', 'healthy smoothie'",
            key="a_label",
        )

    if needs_food_b and food_b is not None:
        with cols[1]:
            st.markdown("**Food B — DB record**")
            st.text_input("Raw product name (read-only)",
                          value=food_b["name"], disabled=True, key="ro_b_name")
            st.text_input("Brand (read-only)",
                          value=food_b["brand"] or "—", disabled=True, key="ro_b_brand")
            st.text_input("Barcode (read-only)",
                          value=food_b["code"], disabled=True, key="ro_b_code")
            st.markdown("**Food B — what gets saved**")
            b_name = st.text_input("food_b_name", value=food_b["name"], key="b_name")
            b_display = st.text_input(
                "food_b_display_name (shown on card)",
                value=default_display_name(food_b["name"]),
                key="b_display",
            )
            b_label = st.text_input(
                "food_b_label (used in headlines)",
                value="",
                placeholder="e.g. 'candy bar', 'sugary drink'",
                key="b_label",
            )
    else:
        b_name = b_display = b_label = ""

    st.divider()
    st.subheader("Row Preview")
    df_now = load_csv()
    pair_id = next_pair_id(df_now)
    row = {
        "pair_id": pair_id,
        "format": fmt,
        "food_a_name": (a_name or "").strip(),
        "food_a_display_name": (a_display or "").strip(),
        "food_a_label": (a_label or "").strip(),
        "food_a_barcode": food_a["code"],
        "food_b_name": (b_name or "").strip(),
        "food_b_display_name": (b_display or "").strip(),
        "food_b_label": (b_label or "").strip(),
        "food_b_barcode": (food_b["code"] if (needs_food_b and food_b) else ""),
        "purpose": purpose,
        "framing_type": framing,
    }
    st.code(",".join(CSV_HEADER), language="csv")
    st.code(",".join(str(row[c]) for c in CSV_HEADER), language="csv")

    if st.button("Append to input.csv", type="primary"):
        append_row(row)
        st.success(f"Appended pair_id={pair_id} to {INPUT_CSV}")
        for k in (
            "search_a", "search_b", "selected_a", "selected_b",
            "a_name", "a_display", "a_label",
            "b_name", "b_display", "b_label",
        ):
            st.session_state.pop(k, None)
        st.rerun()


# --- Cleaner Mode ----------------------------------------------------------

DISCOVERY_CSV = "output/discovery_results.csv"


def _load_discoveries() -> pd.DataFrame:
    p = Path(DISCOVERY_CSV)
    if not p.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(p, dtype=str, keep_default_na=False)
    except Exception:
        return pd.DataFrame()


def _update_discovery_status(discovery_id: str, status: str) -> bool:
    """Mark one row's status (e.g. 'approved' / 'skipped'). Returns True if
    the row was found and updated."""
    p = Path(DISCOVERY_CSV)
    if not p.exists():
        return False
    df = pd.read_csv(p, dtype=str, keep_default_na=False)
    if "discovery_id" not in df.columns or "status" not in df.columns:
        return False
    mask = df["discovery_id"] == str(discovery_id)
    if not mask.any():
        return False
    df.loc[mask, "status"] = status
    df.to_csv(p, index=False)
    return True


# Callbacks for the cleaner-mode clear buttons. Mutating widget-bound
# session_state keys must happen in an on_click callback (which runs before
# widgets re-instantiate on the next rerun) — doing it inline raises a
# StreamlitAPIException.
def _clear_discovery_reviewed() -> None:
    df = _load_discoveries()
    kept = df[df.get("status", "") == "pending"] if "status" in df.columns else df
    kept.to_csv(DISCOVERY_CSV, index=False)
    st.session_state.pop("clear_queue_confirm", None)
    st.session_state["cleaner_idx"] = 0


def _clear_discovery_all() -> None:
    df = _load_discoveries()
    df.iloc[0:0].to_csv(DISCOVERY_CSV, index=False)
    st.session_state.pop("clear_queue_confirm", None)
    st.session_state["cleaner_idx"] = 0


def _delete_input_rows() -> None:
    """Drop the multiselect-chosen rows from input.csv. Keyed on pair_id
    so duplicates are unaffected. Stale entries in output/result_errors.csv
    will self-evict on the next main.py run since they only persist while
    a matching input row exists."""
    selected = st.session_state.get("delete_input_select") or []
    if not selected:
        return
    pair_ids = {s.split(":", 1)[0].strip() for s in selected}
    df = load_csv()
    if "pair_id" in df.columns:
        df = df[~df["pair_id"].astype(str).isin(pair_ids)]
        df.to_csv(INPUT_CSV, index=False)
    st.session_state.pop("delete_input_select", None)
    st.session_state.pop("delete_input_confirm", None)


def render_cleaner_mode() -> None:
    df = _load_discoveries()
    if df.empty:
        st.warning(
            f"No discoveries found at `{DISCOVERY_CSV}`. "
            "Run `python discover_candidates.py` first."
        )
        return

    total = len(df)
    counts = df["status"].value_counts().to_dict() if "status" in df.columns else {}
    pending_n = counts.get("pending", 0)
    approved_n = counts.get("approved", 0)
    skipped_n = counts.get("skipped", 0)

    sc1, sc2, sc3, sc4 = st.columns(4)
    sc1.metric("Total", total)
    sc2.metric("Pending", pending_n)
    sc3.metric("Approved", approved_n)
    sc4.metric("Skipped", skipped_n)

    reviewed_n = approved_n + skipped_n
    if total > 0:
        with st.expander(f"Clear discovery queue ({total} rows)"):
            st.caption(
                "Pending = rows you haven't approved or explicitly skipped — "
                "clicking '◀ Previous / Next ▶' alone leaves a row pending. "
                "Cleared rows are NOT re-discovered next run — that's tracked "
                "separately in `output/considered_pairs.csv`, which these "
                "buttons do not touch."
            )
            confirm = st.checkbox(
                "I confirm clearing rows from the queue",
                key="clear_queue_confirm",
            )
            c1, c2 = st.columns(2)
            with c1:
                st.button(
                    f"Clear reviewed ({reviewed_n})",
                    disabled=(not confirm) or reviewed_n == 0,
                    key="clear_reviewed_btn",
                    on_click=_clear_discovery_reviewed,
                    help="Drops approved + skipped rows. Pending rows stay.",
                )
            with c2:
                st.button(
                    f"Clear ALL ({total})",
                    disabled=not confirm,
                    key="clear_all_btn",
                    on_click=_clear_discovery_all,
                    help="Drops every row in the queue, including pending.",
                )

    pending = df[df.get("status", "") == "pending"].copy()
    if pending.empty:
        st.success("Nothing left to review — all rows are approved or skipped.")
        st.dataframe(df, use_container_width=True)
        return

    # Sort by postability_score desc so the best ideas surface first.
    if "postability_score" in pending.columns:
        pending["_post"] = pd.to_numeric(pending["postability_score"], errors="coerce").fillna(0)
        pending = pending.sort_values("_post", ascending=False).reset_index(drop=True)
    else:
        pending = pending.reset_index(drop=True)

    idx = int(st.session_state.get("cleaner_idx", 0))
    idx = max(0, min(idx, len(pending) - 1))
    st.session_state["cleaner_idx"] = idx

    nav1, nav2, nav3 = st.columns([1, 2, 1])
    with nav1:
        if st.button("◀ Previous", disabled=(idx == 0), use_container_width=True):
            st.session_state["cleaner_idx"] = max(0, idx - 1)
            st.rerun()
    with nav2:
        st.markdown(
            f"<div style='text-align:center;'>Pending {idx + 1} of {len(pending)}</div>",
            unsafe_allow_html=True,
        )
    with nav3:
        if st.button("Next ▶", disabled=(idx >= len(pending) - 1), use_container_width=True):
            st.session_state["cleaner_idx"] = min(len(pending) - 1, idx + 1)
            st.rerun()

    row = pending.iloc[idx]
    discovery_id = str(row.get("discovery_id", "")).strip()
    row_format = (row.get("format") or "comparison").strip().lower() or "comparison"

    # Format-specific labels for the read-only context block.
    if row_format == "exposure":
        cand_label = "Candidate (food_a) — single-item exposure"
        ref_label = ""
    elif row_format == "swap":
        cand_label = "Candidate (food_a) — swap OUT"
        ref_label = "Alternative (food_b) — PICK THIS"
    else:
        cand_label = "Candidate (food_a)"
        ref_label = "Reference (food_b)"

    st.divider()

    # Discovery context (read-only).
    with st.container(border=True):
        st.markdown(
            f"**discovery_id:** `{discovery_id}` · "
            f"**format:** `{row_format}` · "
            f"**postability:** `{row.get('postability_score', '?')}/10` · "
            f"**diff:** `{row.get('score_diff', '?') or '—'}` · "
            f"**angle:** _{row.get('content_angle', '')}_"
        )
        # Two-column layout when there's a reference; full-width for exposure.
        if row_format == "exposure":
            ccol = st.container()
            rcol = None
        else:
            ccol, rcol = st.columns(2)
        with ccol:
            st.markdown(f"**{cand_label}**")
            st.write(f"{row.get('candidate_name_raw', '') or '—'}")
            st.caption(
                f"{row.get('candidate_brand', '') or '—'} · "
                f"`{row.get('candidate_barcode', '')}` · "
                f"score `{row.get('candidate_score', '?')}/10`"
            )
            interp = (row.get("candidate_interpretation") or "").strip()
            if interp:
                st.caption(interp)
            hurts = (row.get("candidate_what_hurts") or "").strip()
            if hurts:
                st.caption(f"⚠️ Hurts: {hurts}")
            helps = (row.get("candidate_what_helps") or "").strip()
            if helps:
                st.caption(f"✓ Helps: {helps}")
        if rcol is not None:
            with rcol:
                st.markdown(f"**{ref_label}**")
                st.write(f"{row.get('reference_name_raw', '') or '—'}")
                st.caption(
                    f"{row.get('reference_brand', '') or '—'} · "
                    f"`{row.get('reference_barcode', '')}` · "
                    f"score `{row.get('reference_score', '?')}/10`"
                )
                ref_interp = (row.get("reference_interpretation") or "").strip()
                if ref_interp:
                    st.caption(ref_interp)

    # Editable form.
    st.divider()
    st.subheader("Edit (becomes the input.csv row)")

    e1, e2, e3 = st.columns(3)
    with e1:
        fmt_options = FORMATS + (
            [] if (row.get("format") or "comparison") in FORMATS else [row.get("format")]
        )
        fmt = st.selectbox(
            "Format", fmt_options,
            index=fmt_options.index(row.get("format") or "comparison"),
            key=f"clean_fmt_{discovery_id}",
        )
    with e2:
        framing_options = FRAMINGS + (
            [] if (row.get("framing_type") or "default") in FRAMINGS else [row.get("framing_type")]
        )
        framing = st.selectbox(
            "Framing type", framing_options,
            index=framing_options.index(row.get("framing_type") or "default"),
            key=f"clean_framing_{discovery_id}",
        )
    with e3:
        purpose_options = PURPOSES + (
            [] if (row.get("purpose") or "snack") in PURPOSES else [row.get("purpose")]
        )
        purpose = st.selectbox(
            "Purpose", purpose_options,
            index=purpose_options.index(row.get("purpose") or "snack"),
            key=f"clean_purpose_{discovery_id}",
        )

    fcols = st.columns(2)
    with fcols[0]:
        st.markdown("**Food A — candidate**")
        a_name = st.text_input(
            "food_a_name",
            value=row.get("candidate_name_raw") or "",
            key=f"clean_a_name_{discovery_id}",
        )
        a_display = st.text_input(
            "food_a_display_name",
            value=row.get("candidate_display_name_suggested") or row.get("candidate_name_raw") or "",
            key=f"clean_a_display_{discovery_id}",
        )
        a_label = st.text_input(
            "food_a_label",
            value=row.get("candidate_label_suggested") or "",
            placeholder="e.g. 'kids yogurt'",
            key=f"clean_a_label_{discovery_id}",
        )
        a_barcode = st.text_input(
            "food_a_barcode",
            value=row.get("candidate_barcode") or "",
            key=f"clean_a_code_{discovery_id}",
        )
    with fcols[1]:
        st.markdown("**Food B — reference**")
        b_name = st.text_input(
            "food_b_name",
            value=row.get("reference_name_raw") or "",
            key=f"clean_b_name_{discovery_id}",
        )
        b_display = st.text_input(
            "food_b_display_name",
            value=row.get("reference_display_name_suggested") or row.get("reference_name_raw") or "",
            key=f"clean_b_display_{discovery_id}",
        )
        b_label = st.text_input(
            "food_b_label",
            value=row.get("reference_label_suggested") or "",
            placeholder="e.g. 'candy bar'",
            key=f"clean_b_label_{discovery_id}",
        )
        b_barcode = st.text_input(
            "food_b_barcode",
            value=row.get("reference_barcode") or "",
            key=f"clean_b_code_{discovery_id}",
        )

    st.divider()

    # Action buttons.
    bcol1, bcol2, bcol3 = st.columns([2, 1, 1])
    with bcol1:
        if st.button("✓ Approve & append to input.csv",
                     type="primary", key=f"approve_{discovery_id}",
                     use_container_width=True):
            df_in = load_csv()
            new_pair_id = next_pair_id(df_in)
            new_row = {
                "pair_id": new_pair_id,
                "format": fmt,
                "food_a_name": (a_name or "").strip(),
                "food_a_display_name": (a_display or "").strip(),
                "food_a_label": (a_label or "").strip(),
                "food_a_barcode": (a_barcode or "").strip(),
                "food_b_name": (b_name or "").strip(),
                "food_b_display_name": (b_display or "").strip(),
                "food_b_label": (b_label or "").strip(),
                "food_b_barcode": (b_barcode or "").strip(),
                "purpose": purpose,
                "framing_type": framing,
            }
            append_row(new_row)
            _update_discovery_status(discovery_id, "approved")
            st.success(
                f"Approved discovery `{discovery_id}` → input.csv pair_id={new_pair_id}"
            )
            # Don't bump idx — pending list shrinks, so the next pending row
            # naturally takes this slot. Clamp on the next render.
            st.rerun()
    with bcol2:
        if st.button("Skip", key=f"skip_{discovery_id}", use_container_width=True):
            _update_discovery_status(discovery_id, "skipped")
            st.info(f"Skipped discovery `{discovery_id}`.")
            st.rerun()
    with bcol3:
        if st.button("Next pending ▶", key=f"next_{discovery_id}",
                     use_container_width=True, disabled=(idx >= len(pending) - 1)):
            st.session_state["cleaner_idx"] = min(len(pending) - 1, idx + 1)
            st.rerun()


# --- Page ------------------------------------------------------------------

st.set_page_config(page_title="SmarterEats Input Builder", layout="wide")
st.title("SmarterEats Input Builder")
st.caption(f"DB: `{_db_path()}` · CSV: `{INPUT_CSV}`")

with st.sidebar:
    mode = st.radio(
        "Mode",
        ["Manual Search", "Cleaner Mode"],
        index=0,
        help=(
            "Manual: search OFF and build rows by hand. "
            "Cleaner: review pending rows from output/discovery_results.csv."
        ),
    )
    st.divider()
    st.markdown("### Search settings")
    fetch_meta = st.checkbox(
        "Fetch images + last-updated from OFF",
        value=True,
        help="Adds 1-3s on first search per query (parallel + cached). Disable for offline / fast.",
        disabled=(mode == "Cleaner Mode"),
    )
    result_limit = st.slider(
        "Max results", 5, 20, 10,
        disabled=(mode == "Cleaner Mode"),
    )

conn = _connect()
if conn is None and mode == "Manual Search":
    st.error(
        f"OFF database not found at `{_db_path()}`. "
        "Set FOOD_DB_PATH or place the file at `./off_products.db`."
    )
    st.stop()

if mode == "Manual Search":
    render_manual_mode(conn, fetch_meta=fetch_meta, result_limit=result_limit)
else:
    render_cleaner_mode()

st.divider()

# Current input.csv view (always visible).
st.subheader("Current input.csv")
df_show = load_csv()
if df_show.empty:
    st.caption("No rows yet — the file will be created on first save.")
else:
    st.dataframe(df_show, use_container_width=True)

    # Row deletion. Lets the user prune input.csv rows from the UI rather
    # than downloading + editing in a spreadsheet (which strips leading
    # zeros from barcode columns and breaks lookups).
    with st.expander(f"Delete rows from input.csv"):
        st.caption(
            "Tip: edit input.csv here instead of downloading it — Excel / "
            "Google Sheets silently strip leading zeros from barcode columns, "
            "which breaks DB lookups."
        )
        delete_options: list[str] = []
        for _, r in df_show.iterrows():
            a = (r.get("food_a_display_name") or r.get("food_a_name") or "?").strip()
            b = (r.get("food_b_display_name") or r.get("food_b_name") or "").strip()
            label = f"{r.get('pair_id', '')}: {a}"
            if b:
                label += f" vs {b}"
            delete_options.append(label)
        to_delete = st.multiselect(
            "Rows to delete",
            delete_options,
            key="delete_input_select",
        )
        if to_delete:
            confirm_del = st.checkbox(
                f"Confirm delete ({len(to_delete)} row(s))",
                key="delete_input_confirm",
            )
            st.button(
                f"Delete {len(to_delete)} row(s)",
                disabled=not confirm_del,
                key="delete_input_btn",
                on_click=_delete_input_rows,
                help="Removes selected rows from input.csv. Cannot be undone.",
            )

    dl_col, clr_col = st.columns([1, 2])
    with dl_col:
        st.download_button(
            "Download input.csv",
            data=df_show.to_csv(index=False).encode("utf-8"),
            file_name="input.csv",
            mime="text/csv",
        )
    with clr_col:
        confirm = st.checkbox(
            f"Confirm clear ({len(df_show)} rows)", key="clear_confirm",
        )
        if st.button("Clear input.csv", disabled=not confirm,
                     help="Wipes input.csv to header-only. Cannot be undone — "
                          "main.py's results.csv will keep prior runs."):
            pd.DataFrame(columns=CSV_HEADER).to_csv(INPUT_CSV, index=False)
            st.session_state["clear_confirm"] = False
            st.success("input.csv cleared.")
            st.rerun()
