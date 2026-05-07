"""Candidate data model for food discovery.

A `Candidate` is a single food/product entity surfaced by a retailer or seed
adapter. Stays a dataclass (not a Pydantic model) to keep the dep surface
small — CSV and JSON serialization use dataclasses.asdict + a couple of
helpers below. Lists serialize as `|`-joined strings in CSVs (single column,
human-reviewable in Streamlit), JSON keeps them native.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime, timezone
from typing import Optional


_LIST_FIELDS = ("score_reasons", "assigned_hubs", "aliases")


@dataclass
class Candidate:
    # Source provenance — preserved for traceability even when we collapse
    # variants into a canonical entity.
    raw_name: str = ""
    canonical_name: str = ""
    brand: str = ""
    category: str = ""
    source: str = ""              # adapter name: 'seed' / 'target' / 'walmart'
    source_url: str = ""
    source_rank: Optional[int] = None
    retailer: str = ""            # 'target' / 'walmart' / 'costco' / etc.
    upc: str = ""                 # optional; OFF lookups can fill this later
    package_size: str = ""
    image_url: str = ""

    # Discovery metadata.
    discovered_at: str = ""
    confidence: float = 0.0       # 0.0-1.0; how sure are we about the brand+name?

    # Content relevance — distinct from nutrition. Score is 0-100, reasons
    # explain how it got there.
    relevance_score: int = 0
    score_reasons: list[str] = field(default_factory=list)

    # Editorial classification.
    assigned_hubs: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    notes: str = ""

    # Review workflow.
    rejection_reason: str = ""
    approval_status: str = "pending"   # pending | approved | rejected

    # Internal: stable key for dedup. Always derived; not authored.
    dedup_key: str = ""

    @staticmethod
    def field_names() -> list[str]:
        return [f.name for f in fields(Candidate)]

    def now_stamp(self) -> None:
        """Set discovered_at to the current UTC time if unset."""
        if not self.discovered_at:
            self.discovered_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

    def to_csv_row(self) -> dict:
        """Flatten to a dict suitable for csv.DictWriter — list fields are
        joined with ' | ' so they read naturally in Streamlit / Excel and
        round-trip cleanly back to lists via `from_csv_row`."""
        d = asdict(self)
        for k in _LIST_FIELDS:
            d[k] = " | ".join(d.get(k) or [])
        if d.get("source_rank") is None:
            d["source_rank"] = ""
        if d.get("confidence") is None:
            d["confidence"] = ""
        return d

    @classmethod
    def from_csv_row(cls, row: dict) -> "Candidate":
        """Inverse of `to_csv_row`. Tolerates missing fields and free-text
        edits in the CSV (someone may have typed a hub name with comma-
        separation or trailing whitespace)."""
        kwargs: dict = {}
        for fld in fields(cls):
            v = row.get(fld.name, "")
            if fld.name in _LIST_FIELDS:
                kwargs[fld.name] = _split_list_field(v)
                continue
            if fld.name == "source_rank":
                kwargs[fld.name] = _maybe_int(v)
                continue
            if fld.name == "confidence":
                kwargs[fld.name] = _maybe_float(v)
                continue
            if fld.name == "relevance_score":
                kwargs[fld.name] = _maybe_int(v) or 0
                continue
            kwargs[fld.name] = (v or "").strip() if isinstance(v, str) else v
        return cls(**kwargs)


def _split_list_field(v) -> list[str]:
    """Accept either ' | '-joined strings (canonical CSV form) or comma-
    separated strings (someone hand-edited). Returns clean tokens."""
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v)
    parts = re.split(r"[|,;]", s)
    return [p.strip() for p in parts if p.strip()]


def _maybe_int(v) -> Optional[int]:
    if v in ("", None):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _maybe_float(v) -> Optional[float]:
    if v in ("", None):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def write_candidates_csv(path, candidates: list[Candidate]) -> int:
    """Write candidates to a CSV with the canonical column order. Returns
    the count written."""
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    cols = Candidate.field_names()
    with open(p, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        for c in candidates:
            writer.writerow(c.to_csv_row())
    return len(candidates)


def read_candidates_csv(path) -> list[Candidate]:
    """Round-trip the canonical CSV back to Candidate objects. Returns []
    when the file is missing."""
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return []
    with open(p, newline="") as f:
        return [Candidate.from_csv_row(row) for row in csv.DictReader(f)]


def write_candidates_json(path, candidates: list[Candidate]) -> int:
    """JSON snapshot of candidates — keeps native list types. Returns the
    count written."""
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump([asdict(c) for c in candidates], f, indent=2, ensure_ascii=False)
    return len(candidates)
