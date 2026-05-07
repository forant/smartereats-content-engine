"""Streamlit review UI for the food candidate discovery pipeline.

Loads `output/discovery/normalized_candidates.csv`, lets the reviewer
edit canonical name / brand / hubs / approval status inline, and writes
the result back to:
  - output/discovery/approved_candidates.csv
  - output/discovery/rejected_candidates.csv

The approved CSV is the contract for downstream blog generators; the
rejected CSV is the audit trail for "why did we skip this".
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from discovery.candidate import (
    Candidate,
    read_candidates_csv,
    write_candidates_csv,
    write_candidates_json,
)
from discovery.hubs import V1_HUBS
from discovery.output import (
    APPROVED_CSV, NORMALIZED_CSV, OPPORTUNITIES_CSV, REJECTED_CSV,
    SNAPSHOT_JSON, write_opportunities,
)
from discovery.pipeline import ALL_ADAPTERS, run_discovery


_APPROVAL_STATES = ("pending", "approved", "rejected")


def _editable_columns() -> list[str]:
    """Columns the reviewer can edit inline."""
    return [
        "canonical_name", "brand", "category",
        "assigned_hubs", "approval_status", "rejection_reason", "notes",
    ]


def _load_normalized() -> pd.DataFrame:
    """Read the normalized CSV into a Streamlit-friendly DataFrame.
    Empties → 0-row frame with the canonical column set so st.data_editor
    still renders meaningfully."""
    if not NORMALIZED_CSV.exists():
        return pd.DataFrame(columns=Candidate.field_names())
    df = pd.read_csv(NORMALIZED_CSV, dtype=str, keep_default_na=False)
    return df


def _save_review(df: pd.DataFrame) -> tuple[int, int]:
    """Split the edited DataFrame into approved + rejected and write each
    bucket to its CSV. Pending rows stay in the normalized CSV (updated
    in place too, so edits persist even before approval)."""
    approved: list[Candidate] = []
    rejected: list[Candidate] = []
    for _, row in df.iterrows():
        cand = Candidate.from_csv_row(row.to_dict())
        if cand.approval_status == "approved":
            approved.append(cand)
        elif cand.approval_status == "rejected":
            rejected.append(cand)
    write_candidates_csv(APPROVED_CSV, approved)
    write_candidates_csv(REJECTED_CSV, rejected)
    # Persist edits back to normalized too — keeps the working set in sync
    # if the reviewer edits canonical_name / hubs / notes pre-approval.
    all_candidates = [Candidate.from_csv_row(row.to_dict())
                      for _, row in df.iterrows()]
    write_candidates_csv(NORMALIZED_CSV, all_candidates)
    write_candidates_json(SNAPSHOT_JSON, all_candidates)
    write_opportunities(all_candidates)
    return len(approved), len(rejected)


def render_food_discovery_mode() -> None:
    st.header("Food candidate discovery")
    st.caption(
        "Curated food/product entities scored for CONTENT relevance "
        "(not nutrition). Approved rows feed downstream blog generators."
    )

    # Run-controls block — kicks off `run_discovery` from the UI.
    with st.expander("Run discovery", expanded=not NORMALIZED_CSV.exists()):
        col1, col2, col3 = st.columns([2, 2, 1])
        with col1:
            sources = st.multiselect(
                "Sources",
                sorted(ALL_ADAPTERS.keys()),
                default=["seed"],
                help="Adapters to invoke. 'seed' is the curated brand list "
                     "and always works; other adapters are best-effort.",
            )
        with col2:
            cats_input = st.text_input(
                "Categories",
                value="all",
                help="Comma-separated category slugs. Use 'all' to grab "
                     "every category the seed adapter has.",
            )
        with col3:
            limit = st.number_input(
                "Limit / call", min_value=1, max_value=500, value=50, step=10,
            )
        if st.button("▶ Run discovery", type="primary",
                     disabled=not sources):
            cats = [c.strip() for c in cats_input.split(",") if c.strip()]
            with st.spinner("Discovering candidates..."):
                raw, normalized = run_discovery(
                    adapter_names=sources,
                    categories=cats or ["all"],
                    limit_per_call=int(limit),
                )
            st.success(
                f"Wrote {len(normalized)} normalized candidate(s) from "
                f"{len(raw)} raw row(s). Reload below to review."
            )
            st.rerun()

    df = _load_normalized()
    if df.empty:
        st.info(
            f"No candidates yet. Run discovery above (or `./venv/bin/python "
            f"discover_foods.py` from the CLI) to populate "
            f"`{NORMALIZED_CSV}`."
        )
        return

    # Top-of-page metrics.
    counts = df["approval_status"].value_counts().to_dict()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total", len(df))
    m2.metric("Pending", counts.get("pending", 0))
    m3.metric("Approved", counts.get("approved", 0))
    m4.metric("Rejected", counts.get("rejected", 0))

    # Filters.
    with st.expander("Filters", expanded=False):
        f1, f2, f3 = st.columns(3)
        with f1:
            min_score = st.slider("Min relevance score", 0, 100, 0, step=5)
        with f2:
            hubs = sorted(set(V1_HUBS) | _hubs_in_df(df))
            chosen_hubs = st.multiselect("Hubs (any match)", hubs, default=[])
        with f3:
            statuses = st.multiselect(
                "Status", list(_APPROVAL_STATES),
                default=["pending"],
            )

    view = df.copy()
    view["relevance_score_n"] = pd.to_numeric(
        view["relevance_score"], errors="coerce"
    ).fillna(0).astype(int)
    if min_score:
        view = view[view["relevance_score_n"] >= min_score]
    if statuses:
        view = view[view["approval_status"].isin(statuses)]
    if chosen_hubs:
        chosen_set = set(chosen_hubs)
        view = view[view["assigned_hubs"].fillna("").apply(
            lambda s: bool(_split_hubs(s) & chosen_set)
        )]
    view = view.sort_values(
        ["relevance_score_n", "canonical_name"], ascending=[False, True],
    )

    st.caption(f"Showing {len(view)} of {len(df)} candidates")

    # Inline editor — the read-only columns provide context, the editable
    # ones are listed in `_editable_columns()`.
    editable = _editable_columns()
    column_config = {
        "approval_status": st.column_config.SelectboxColumn(
            "approval_status", options=list(_APPROVAL_STATES),
        ),
        "relevance_score": st.column_config.NumberColumn(disabled=True),
        "score_reasons": st.column_config.TextColumn(disabled=True),
        "raw_name": st.column_config.TextColumn(disabled=True),
        "aliases": st.column_config.TextColumn(disabled=True),
        "source": st.column_config.TextColumn(disabled=True),
        "retailer": st.column_config.TextColumn(disabled=True),
        "discovered_at": st.column_config.TextColumn(disabled=True),
    }
    show_cols = [
        "approval_status", "relevance_score", "canonical_name", "brand",
        "category", "assigned_hubs", "score_reasons", "raw_name", "aliases",
        "rejection_reason", "notes", "source", "retailer", "confidence",
    ]
    show_cols = [c for c in show_cols if c in view.columns]
    edited = st.data_editor(
        view[show_cols],
        column_config=column_config,
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        key="food_discovery_editor",
    )

    # Bulk-action helpers — operate on rows currently visible.
    a1, a2, a3 = st.columns([1, 1, 2])
    with a1:
        if st.button("Approve all visible (≥ min score)",
                     disabled=len(edited) == 0):
            edited["approval_status"] = "approved"
            df.loc[edited.index, "approval_status"] = "approved"
            n_a, n_r = _save_review(df)
            st.success(f"Saved — approved={n_a}, rejected={n_r}.")
            st.rerun()
    with a2:
        if st.button("Save edits", type="primary",
                     disabled=len(edited) == 0):
            # Merge edited fields back into the full df.
            for col in show_cols:
                df.loc[edited.index, col] = edited[col].values
            n_a, n_r = _save_review(df)
            st.success(
                f"Saved — approved={n_a}, rejected={n_r}, "
                f"opportunities={OPPORTUNITIES_CSV.name}."
            )
            st.rerun()
    with a3:
        st.caption(
            f"Outputs: `{APPROVED_CSV}`, `{REJECTED_CSV}`, "
            f"`{OPPORTUNITIES_CSV}`. Edits to other fields persist in "
            f"`{NORMALIZED_CSV}` on save."
        )


def _split_hubs(s: str) -> set[str]:
    """Hubs in CSV form are joined with ' | '. Tolerate commas too."""
    if not s:
        return set()
    parts: list[str] = []
    for piece in str(s).split("|"):
        for sub in piece.split(","):
            sub = sub.strip()
            if sub:
                parts.append(sub)
    return set(parts)


def _hubs_in_df(df: pd.DataFrame) -> set[str]:
    """Union of all hubs that appear in the assigned_hubs column."""
    out: set[str] = set()
    for v in df.get("assigned_hubs", pd.Series(dtype=str)).fillna(""):
        out |= _split_hubs(v)
    return out
