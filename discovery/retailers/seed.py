"""Seed-list adapter: reads curated `{brand, category, retailer}` rows
from a JSON file and emits them as candidates. No network, no anti-bot
risk, always works. V1's primary ideation source.

The seed file lives at `config/discovery_seeds.json` by default. Format:

    [
      {"brand": "Quest",     "category": "protein-bars",   "retailer": "target"},
      {"brand": "Barebells", "category": "protein-bars",   "retailer": "target"},
      {"brand": "Fairlife Core Power", "category": "protein-drinks",
       "retailer": "walmart", "aliases": ["Core Power", "Fairlife"]},
      ...
    ]

Each entry can also include optional fields: `source_url`, `package_size`,
`image_url`, `notes`. These flow through unchanged.

To grow the catalog: edit the JSON file and re-run discovery. The pipeline
will dedupe, so duplicate entries (same brand+category) are safe.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from ..candidate import Candidate
from .base import Retailer, make_candidate


DEFAULT_SEED_PATH = Path("config") / "discovery_seeds.json"


class SeedAdapter(Retailer):
    name = "seed"

    def __init__(self, seed_path: Optional[Path] = None) -> None:
        self.seed_path = Path(seed_path) if seed_path else DEFAULT_SEED_PATH

    def _load(self) -> list[dict]:
        if not self.seed_path.exists():
            print(
                f"WARNING: seed file {self.seed_path} not found; "
                "SeedAdapter returning no candidates.",
                file=sys.stderr,
            )
            return []
        try:
            with open(self.seed_path) as f:
                data = json.load(f)
        except Exception as e:
            print(f"WARNING: could not parse {self.seed_path}: {e}", file=sys.stderr)
            return []
        if not isinstance(data, list):
            print(
                f"WARNING: {self.seed_path} top-level must be a list; got "
                f"{type(data).__name__}",
                file=sys.stderr,
            )
            return []
        return data

    def supports(self, category: str) -> bool:
        # The seed file determines categories; we accept anything and let
        # `fetch` filter.
        return True

    def fetch(self, category: str, limit: int = 50) -> list[Candidate]:
        rows = self._load()
        if not rows:
            return []
        target_cat = (category or "").strip().lower()
        out: list[Candidate] = []
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            row_cat = (row.get("category") or "").strip().lower()
            # If category="all" or empty, take everything; otherwise filter.
            if target_cat and target_cat != "all" and row_cat != target_cat:
                continue
            brand = (row.get("brand") or "").strip()
            if not brand:
                continue
            # raw_name defaults to "{Brand} {Category}" which mirrors how
            # readers naturally search: "Quest protein bars".
            raw_name = (row.get("raw_name") or "").strip() or _default_raw_name(
                brand, row_cat
            )
            cand = make_candidate(
                raw_name=raw_name,
                brand=brand,
                category=row_cat,
                source=self.name,
                retailer=(row.get("retailer") or "").strip(),
                source_url=(row.get("source_url") or "").strip(),
                source_rank=i,
                upc=(row.get("upc") or "").strip(),
                package_size=(row.get("package_size") or "").strip(),
                image_url=(row.get("image_url") or "").strip(),
            )
            cand.aliases = [a.strip() for a in (row.get("aliases") or []) if str(a).strip()]
            cand.notes = (row.get("notes") or "").strip()
            cand.confidence = float(row.get("confidence") or 0.95)
            out.append(cand)
            if len(out) >= limit:
                break
        return out


def _default_raw_name(brand: str, category: str) -> str:
    """Compose a natural-language raw_name from a curated brand+category.
    Matches how shoppers search: 'Quest protein bars', 'Lean Cuisine
    frozen meals'. Hyphenated category slugs become spaced words."""
    if not brand:
        return category
    if not category:
        return brand
    return f"{brand} {category.replace('-', ' ')}"
