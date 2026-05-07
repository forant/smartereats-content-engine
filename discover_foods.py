"""CLI for the food candidate discovery pipeline.

Examples:
    # Default — seed adapter only, all categories.
    ./venv/bin/python discover_foods.py

    # Just one category, with a specific adapter.
    ./venv/bin/python discover_foods.py --source seed --category protein-bars

    # Try a real retailer (best-effort; falls through gracefully on block).
    ./venv/bin/python discover_foods.py --source target --category protein-bars --limit 30

    # Multi-source run.
    ./venv/bin/python discover_foods.py --source seed --source target --category all

    # Dry run — print the slate, don't write CSVs.
    ./venv/bin/python discover_foods.py --dry-run

    # Constrain hub assignment to the live website topics dir.
    WEBSITE_TOPICS_DIR=/path/to/site/content/topics \\
        ./venv/bin/python discover_foods.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from discovery.candidate import Candidate
from discovery.output import (
    APPROVED_CSV, NORMALIZED_CSV, OPPORTUNITIES_CSV, RAW_CSV, REJECTED_CSV,
    SNAPSHOT_JSON, write_opportunities,
)
from discovery.pipeline import ALL_ADAPTERS, run_discovery


def _load_topics_allowlist() -> set[str] | None:
    """Mirrors main.load_published_topics_index — local copy keeps this
    CLI free of imports from the heavy main.py module."""
    p = os.environ.get("WEBSITE_TOPICS_DIR")
    if not p:
        return None
    path = Path(p)
    if not path.exists() or not path.is_dir():
        print(
            f"WARNING: WEBSITE_TOPICS_DIR={p!r} is not a directory; hub "
            "assignments won't be intersected with the live allowlist.",
            file=sys.stderr,
        )
        return None
    out: set[str] = set()
    for ext in ("*.mdx", "*.md"):
        for f in path.glob(ext):
            out.add(f.stem)
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Discover food candidate entities for SmarterEats blog ideation. "
            "Scores candidates for CONTENT relevance — not nutrition."
        ),
    )
    p.add_argument(
        "--source", action="append", default=None,
        choices=sorted(ALL_ADAPTERS.keys()),
        help=("Which adapter(s) to run. Pass once per adapter for multi-"
              "source runs. Defaults to ['seed'] when unset."),
    )
    p.add_argument(
        "--category", action="append", default=None,
        help=("Category slug(s) to fetch (e.g. protein-bars). Pass once "
              "per category. Use 'all' to grab every category the seed "
              "adapter has. Defaults to ['all']."),
    )
    p.add_argument(
        "--limit", type=int, default=50,
        help="Cap per (adapter, category) call. Default 50.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Run the pipeline but don't write CSVs / JSON.",
    )
    p.add_argument(
        "--top", type=int, default=20,
        help="How many top-scoring rows to print at the end. Default 20.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    sources = args.source or ["seed"]
    categories = args.category or ["all"]
    allowed_hubs = _load_topics_allowlist()

    raw, normalized = run_discovery(
        adapter_names=sources,
        categories=categories,
        limit_per_call=args.limit,
        allowed_hubs=allowed_hubs,
        write_outputs=not args.dry_run,
    )

    print()
    print("=== Discovery summary ===")
    print(f"sources:        {', '.join(sources)}")
    print(f"categories:     {', '.join(categories)}")
    print(f"raw candidates: {len(raw)}")
    print(f"normalized:     {len(normalized)} (after canonicalize + dedupe)")

    if not args.dry_run:
        print(f"wrote:          {RAW_CSV}")
        print(f"                {NORMALIZED_CSV}")
        print(f"                {SNAPSHOT_JSON}")
        # Emit opportunities upfront — pre-review it can also reflect the
        # post types we'd target if everything were approved.
        opps = write_opportunities(normalized)
        print(f"                {OPPORTUNITIES_CSV} ({opps} rows)")
    else:
        print("(dry-run; no files written)")

    if normalized and args.top:
        print()
        print(f"Top {min(args.top, len(normalized))} by relevance:")
        for c in normalized[: args.top]:
            hubs = ", ".join(c.assigned_hubs) or "—"
            print(f"  {c.relevance_score:>3}  {c.canonical_name:<35}  "
                  f"[{c.brand:<22}]  {hubs}")

    print()
    print("Next: open Streamlit (./venv/bin/streamlit run input_builder.py) → "
          "Food Discovery to review/approve candidates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
