"""Output paths + writers for the discovery pipeline.

Five CSV files + one JSON snapshot live under output/discovery/:

  raw_candidates.csv         — direct from adapters, before dedup
  normalized_candidates.csv  — after canonicalize + dedupe + score + hub
  approved_candidates.csv    — Streamlit-approved subset (feeds generators)
  rejected_candidates.csv    — Streamlit-rejected subset (audit trail)
  content_opportunities.csv  — derived post ideas per approved entity

  discovery_snapshot.json    — single point-in-time JSON of everything for
                               versioning / diffing across runs

The approved CSV is the contract for downstream generators. Its schema
mirrors `Candidate.field_names()` so it round-trips through dataclasses
cleanly.
"""

from __future__ import annotations

import csv
from pathlib import Path

from .candidate import Candidate, write_candidates_csv, write_candidates_json


OUTPUT_DIR = Path("output") / "discovery"
RAW_CSV = OUTPUT_DIR / "raw_candidates.csv"
NORMALIZED_CSV = OUTPUT_DIR / "normalized_candidates.csv"
APPROVED_CSV = OUTPUT_DIR / "approved_candidates.csv"
REJECTED_CSV = OUTPUT_DIR / "rejected_candidates.csv"
OPPORTUNITIES_CSV = OUTPUT_DIR / "content_opportunities.csv"
SNAPSHOT_JSON = OUTPUT_DIR / "discovery_snapshot.json"


# Suggested post types per assigned hub. Keep small and stable — the
# Streamlit reviewer / downstream generators are free to add more.
_HUB_POST_TYPES: dict[str, tuple[str, ...]] = {
    "high-protein-snacks":   ("evaluation", "comparison", "goal_guide"),
    "weight-loss-snacks":    ("evaluation", "goal_guide"),
    "healthy-costco-foods":  ("retailer_guide", "evaluation"),
    "protein-bars":          ("evaluation", "comparison", "goal_guide"),
    "healthy-frozen-meals":  ("evaluation", "comparison", "goal_guide"),
    "healthy-drinks":        ("evaluation", "comparison"),
    "healthy-convenience-foods": ("evaluation", "goal_guide"),
}


def write_raw(candidates: list[Candidate]) -> int:
    return write_candidates_csv(RAW_CSV, candidates)


def write_normalized(candidates: list[Candidate]) -> int:
    return write_candidates_csv(NORMALIZED_CSV, candidates)


def write_approved(candidates: list[Candidate]) -> int:
    return write_candidates_csv(APPROVED_CSV, candidates)


def write_rejected(candidates: list[Candidate]) -> int:
    return write_candidates_csv(REJECTED_CSV, candidates)


def write_snapshot(candidates: list[Candidate]) -> int:
    return write_candidates_json(SNAPSHOT_JSON, candidates)


def write_opportunities(candidates: list[Candidate]) -> int:
    """Derive a content_opportunities.csv from the candidates list. Each
    row is one (canonical entity, post type) pair the team could ship.
    Useful as a TODO surface — sortable by relevance + topic in Streamlit
    or Sheets."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cols = [
        "canonical_name", "brand", "category", "post_type",
        "primary_hub", "all_hubs", "relevance_score", "confidence",
        "source", "retailer", "source_count_proxy", "rejection_reason",
        "approval_status", "example_raw_names", "score_reasons",
    ]
    with open(OPPORTUNITIES_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        rows_written = 0
        for c in candidates:
            if not c.canonical_name:
                continue
            primary = c.assigned_hubs[0] if c.assigned_hubs else ""
            post_types = _post_types_for(c.assigned_hubs)
            for pt in post_types or ("evaluation",):
                writer.writerow({
                    "canonical_name": c.canonical_name,
                    "brand": c.brand,
                    "category": c.category,
                    "post_type": pt,
                    "primary_hub": primary,
                    "all_hubs": " | ".join(c.assigned_hubs),
                    "relevance_score": c.relevance_score,
                    "confidence": c.confidence,
                    "source": c.source,
                    "retailer": c.retailer,
                    "source_count_proxy": 1 + len(c.aliases),
                    "rejection_reason": c.rejection_reason,
                    "approval_status": c.approval_status,
                    "example_raw_names": " | ".join(
                        [c.raw_name] + c.aliases[:2]
                    ),
                    "score_reasons": " | ".join(c.score_reasons),
                })
                rows_written += 1
    return rows_written


def _post_types_for(hubs: list[str]) -> list[str]:
    """Aggregate the suggested post types across a candidate's hubs.
    Stable order; first-seen wins."""
    out: list[str] = []
    seen: set[str] = set()
    for h in hubs:
        for pt in _HUB_POST_TYPES.get(h, ()):
            if pt not in seen:
                seen.add(pt)
                out.append(pt)
    return out
