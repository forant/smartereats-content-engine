"""SmarterEats input.csv builder.

A local Streamlit helper to build clean input.csv rows from the OFF
database without any backend calls, scoring, or rendering.

Setup (one-time):
    pip install -r requirements-builder.txt

Run from the project root (so it can find ./off_products.db and
./input.csv at relative paths):
    streamlit run input_builder.py

The DB path defaults to ./off_products.db; set FOOD_DB_PATH to point
elsewhere (e.g. /Users/.../foodscore/backend/off_products.db).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pandas as pd
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


def search_products(conn, query: str, limit: int = 12) -> list[dict]:
    """Search OFF products by name. FTS first, fall back to ANDed LIKE."""
    q = query.strip()
    if not q:
        return []
    tokens = [t for t in q.lower().split() if t]
    if not tokens:
        return []
    fts_query = " ".join(tokens)
    fts_hits: list[dict] = []
    try:
        cursor = conn.execute(
            """
            SELECT p.code, p.product_name, p.brands
            FROM products_fts fts
            JOIN products p ON p.rowid = fts.rowid
            WHERE products_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts_query, limit),
        )
        fts_hits = [dict(r) for r in cursor.fetchall()]
    except Exception:
        fts_hits = []

    if fts_hits:
        return fts_hits

    # Fall back to ANDed LIKE on name_normalized with hyphens/spaces
    # collapsed so "gogurt" matches stored "go-gurt", "specialk" matches
    # "special k", etc. Each token is also collapsed the same way.
    norm_expr = "REPLACE(REPLACE(name_normalized, '-', ''), ' ', '')"
    likes = [f"%{t.replace('-', '').replace(' ', '')}%" for t in tokens]
    where = " AND ".join(f"{norm_expr} LIKE ?" for _ in likes)
    cursor = conn.execute(
        f"SELECT code, product_name, brands FROM products WHERE {where} LIMIT ?",
        (*likes, limit),
    )
    return [dict(r) for r in cursor.fetchall()]


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
    """Append a row to input.csv. Preserves any existing extra columns
    (e.g. legacy `angle`) from a prior CSV."""
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
    """Light cleanup of an OFF product_name for the card display field."""
    if not raw:
        return ""
    s = raw.strip()
    for sep in (",", " - ", " | ", " / ", " (", " ["):
        if sep in s:
            s = s.split(sep)[0].strip()
    words = s.split()
    return " ".join(words[:5]) if len(words) > 5 else s


def _label_match(r: dict) -> str:
    name = r.get("product_name") or "(unnamed)"
    brand = r.get("brands") or "—"
    code = r.get("code") or ""
    return f"{name}  ·  {brand}  ·  {code}"


# --- UI --------------------------------------------------------------------

st.set_page_config(page_title="SmarterEats Input Builder", layout="wide")
st.title("SmarterEats Input Builder")
st.caption(f"DB: `{_db_path()}` · CSV: `{INPUT_CSV}`")

conn = _connect()
if conn is None:
    st.error(
        f"OFF database not found at `{_db_path()}`. "
        "Set FOOD_DB_PATH or place the file at `./off_products.db`."
    )
    st.stop()

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

# Food A.
st.subheader("Food A")
search_a = st.text_input("Search OFF (e.g. 'gogurt strawberry')", key="search_a")
food_a: dict | None = None
if search_a:
    res_a = search_products(conn, search_a)
    if not res_a:
        st.warning("No matches in OFF for that query.")
    else:
        opts = ["— select a product —"] + [_label_match(r) for r in res_a]
        sel = st.selectbox(f"Top {len(res_a)} matches", opts, key="sel_a")
        if sel != opts[0]:
            food_a = res_a[opts.index(sel) - 1]

# Food B (conditional).
food_b: dict | None = None
if needs_food_b:
    st.divider()
    st.subheader("Food B")
    search_b = st.text_input("Search OFF (e.g. 'snickers')", key="search_b")
    if search_b:
        res_b = search_products(conn, search_b)
        if not res_b:
            st.warning("No matches in OFF for that query.")
        else:
            opts_b = ["— select a product —"] + [_label_match(r) for r in res_b]
            sel_b = st.selectbox(f"Top {len(res_b)} matches", opts_b, key="sel_b")
            if sel_b != opts_b[0]:
                food_b = res_b[opts_b.index(sel_b) - 1]
else:
    st.caption("Food B is not needed for the `exposure` format.")

st.divider()

# Preview / edit.
st.subheader("Card Preview Data")
ready = (food_a is not None) and ((not needs_food_b) or (food_b is not None))

if not ready:
    if food_a is None:
        st.info("Pick a Food A above to start.")
    elif needs_food_b:
        st.info("Pick a Food B above.")
else:
    cols = st.columns(2 if needs_food_b else 1)
    with cols[0]:
        st.markdown("**Food A — DB record**")
        st.text_input("Raw product name (read-only)",
                      value=food_a["product_name"], disabled=True, key="ro_a_name")
        st.text_input("Brand (read-only)",
                      value=food_a["brands"] or "—", disabled=True, key="ro_a_brand")
        st.text_input("Barcode (read-only)",
                      value=food_a["code"], disabled=True, key="ro_a_code")
        st.markdown("**Food A — what gets saved**")
        a_name = st.text_input(
            "food_a_name", value=food_a["product_name"], key="a_name",
        )
        a_display = st.text_input(
            "food_a_display_name (shown on card)",
            value=default_display_name(food_a["product_name"]),
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
                          value=food_b["product_name"], disabled=True, key="ro_b_name")
            st.text_input("Brand (read-only)",
                          value=food_b["brands"] or "—", disabled=True, key="ro_b_brand")
            st.text_input("Barcode (read-only)",
                          value=food_b["code"], disabled=True, key="ro_b_code")
            st.markdown("**Food B — what gets saved**")
            b_name = st.text_input(
                "food_b_name", value=food_b["product_name"], key="b_name",
            )
            b_display = st.text_input(
                "food_b_display_name (shown on card)",
                value=default_display_name(food_b["product_name"]),
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
        # Clear the form so the next row starts fresh.
        for k in ("search_a", "search_b", "sel_a", "sel_b",
                  "a_name", "a_display", "a_label",
                  "b_name", "b_display", "b_label"):
            st.session_state.pop(k, None)
        st.rerun()

st.divider()

# Current input.csv view.
st.subheader("Current input.csv")
df_show = load_csv()
if df_show.empty:
    st.caption("No rows yet — the file will be created on first save.")
else:
    st.dataframe(df_show, use_container_width=True)
    st.download_button(
        "Download input.csv",
        data=df_show.to_csv(index=False).encode("utf-8"),
        file_name="input.csv",
        mime="text/csv",
    )
