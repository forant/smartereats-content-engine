"""Food candidate discovery pipeline for SmarterEats blog ideation.

This module produces a reviewable catalog of canonical food/product entities
that downstream blog generators (`main.py`, `discover_evaluations.py`) can
consume. It does NOT score nutrition — that lives in the foodscore backend.
What this module scores is **content relevance**: is this entity worth
writing about, and which topic hubs does it belong in.

Pipeline:

    sources (seed YAML / retailer adapters)
        → raw candidates (Candidate objects with rawName, brand, ...)
        → normalize (canonical name, dedup key)
        → relevance scoring (0-100 + reasons)
        → hub assignment (1+ topic hubs from the live list)
        → output CSVs (raw / normalized / approved / rejected)
        → Streamlit review (approve / reject / edit)
        → exported approved candidates feed downstream generation

The "approved" CSV is what feeds blog generation. Rows in there are the
canonical food entities to evaluate / compare / put in goal guides.
"""

from .candidate import Candidate
from .pipeline import run_discovery
from .output import (
    OUTPUT_DIR,
    RAW_CSV,
    NORMALIZED_CSV,
    APPROVED_CSV,
    REJECTED_CSV,
    OPPORTUNITIES_CSV,
)

__all__ = [
    "Candidate",
    "run_discovery",
    "OUTPUT_DIR",
    "RAW_CSV",
    "NORMALIZED_CSV",
    "APPROVED_CSV",
    "REJECTED_CSV",
    "OPPORTUNITIES_CSV",
]
