"""Discovery pipeline orchestrator.

Walks adapter → category combinations, normalizes + dedupes the resulting
candidates, scores them, assigns hubs, and writes the raw + normalized
CSVs. Approval/rejection happens out-of-band in Streamlit (or in a
spreadsheet) and feeds the approved/rejected outputs.

The pipeline is the only thing that knows about adapters; the writers and
review UI never import from `retailers/`.
"""

from __future__ import annotations

import sys
from typing import Iterable, Optional

from .candidate import Candidate
from .hubs import assign_hubs
from .normalize import canonicalize_name, dedupe_candidates, dedup_key_for
from .output import write_normalized, write_raw, write_snapshot
from .retailers import Retailer, SeedAdapter, TargetAdapter, WalmartAdapter
from .score import score_relevance


# Default adapter registry. CLI can opt into / out of any of these.
ALL_ADAPTERS: dict[str, type[Retailer]] = {
    "seed":    SeedAdapter,
    "target":  TargetAdapter,
    "walmart": WalmartAdapter,
}


def _instantiate(adapter_name: str) -> Retailer:
    cls = ALL_ADAPTERS.get(adapter_name)
    if cls is None:
        raise ValueError(
            f"Unknown adapter {adapter_name!r}. "
            f"Valid: {sorted(ALL_ADAPTERS)}"
        )
    return cls()


def run_discovery(
    adapter_names: Iterable[str],
    categories: Iterable[str],
    *,
    limit_per_call: int = 50,
    allowed_hubs: Optional[set[str]] = None,
    write_outputs: bool = True,
) -> tuple[list[Candidate], list[Candidate]]:
    """Run discovery end-to-end. Returns (raw_candidates,
    normalized_candidates).

    `adapter_names`  — which adapters to invoke (e.g. ['seed', 'target']).
    `categories`     — which category slugs to fetch from each adapter.
                       Use ['all'] to pull every category an adapter
                       supports (only meaningful for SeedAdapter).
    `limit_per_call` — cap per (adapter, category) pair. Politeness +
                       sanity guard against runaway scrapes.
    `allowed_hubs`   — when provided, intersect assigned hubs with this
                       live allowlist (typically WEBSITE_TOPICS_DIR).
    `write_outputs`  — flip to False for unit tests / dry-runs."""
    adapters = []
    for name in adapter_names:
        try:
            adapters.append(_instantiate(name))
        except ValueError as e:
            print(f"WARNING: {e}", file=sys.stderr)

    raw: list[Candidate] = []
    for adapter in adapters:
        for cat in categories:
            if not adapter.supports(cat) and cat != "all":
                continue
            try:
                got = adapter.fetch(cat, limit=limit_per_call)
            except Exception as e:  # adapters MUST not raise, but be safe
                print(
                    f"WARNING: adapter {adapter.name}/{cat} raised {e!r}; "
                    "skipping (fix the adapter — it should fail gracefully).",
                    file=sys.stderr,
                )
                got = []
            raw.extend(got)

    # Normalize each candidate before dedupe so we collapse on canonical
    # form, not the noisy raw title.
    for c in raw:
        canonical, brand = canonicalize_name(c.raw_name, brand_hint=c.brand)
        c.canonical_name = canonical
        if not c.brand and brand:
            c.brand = brand
        c.dedup_key = dedup_key_for(c.canonical_name, c.brand)

    normalized = dedupe_candidates(raw)

    # Score + hub-assign on the deduped set so the boost for multi-source
    # entities reflects the real merge count.
    for c in normalized:
        # source_count proxy: 1 + number of aliases collected during dedupe.
        source_count = 1 + len(c.aliases)
        score, reasons = score_relevance(c, source_count=source_count)
        c.relevance_score = score
        c.score_reasons = reasons
        c.assigned_hubs = assign_hubs(c, allowed=allowed_hubs)

    # Stable sort: highest relevance first, then by canonical_name.
    normalized.sort(key=lambda c: (-c.relevance_score, c.canonical_name.lower()))

    if write_outputs:
        write_raw(raw)
        write_normalized(normalized)
        write_snapshot(normalized)

    return raw, normalized
