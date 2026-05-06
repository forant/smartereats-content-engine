"""Discover candidate foods for single-food 'Is X Healthy?' evaluation posts.

Walks the existing comparison corpus (input.csv) for unique foods, normalizes
them, dedupes, and proposes evaluation posts the cleaner can review and add
to input.csv.

Reuses everything possible from the existing pipeline:
  - main._evaluation_title / _evaluation_slug for grammar + URL.
  - main._smart_title_case for display formatting.
  - The same input.csv schema, so output rows append cleanly.

Default mode is dry-run: prints the proposed slate without writing anything.
Pass --csv FILE to stage candidates in a separate file, or --append-to-input
to add them straight to input.csv.

Usage:
    ./venv/bin/python discover_evaluations.py
    ./venv/bin/python discover_evaluations.py --csv output/evaluation_candidates.csv
    ./venv/bin/python discover_evaluations.py --append-to-input --limit 20

Backward-compatible: doesn't touch existing comparison rows, doesn't run the
backend, doesn't call OpenAI.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

from main import (
    _evaluation_slug,
    _evaluation_title,
    _smart_title_case,
)
from discover_candidates import (
    CSV_COLUMNS as DISCOVERY_CSV_COLUMNS,
    OUTPUT_CSV as DISCOVERY_CSV_PATH,
    load_existing_state as load_discovery_state,
    append_rows as append_discovery_rows,
)


INPUT_CSV_DEFAULT = "input.csv"
DEFAULT_PURPOSE = "snack"
DEFAULT_FRAMING = "evaluation"

# Trailing size / count noise stripped before normalization. Matches things
# like "12 oz", "Net Wt 1lb 2oz", "12-pack", "(16 fl oz)", "8 ct".
_SIZE_RE = re.compile(
    r"\b(?:net\s*wt\.?\s*[\d./\s]+\s*(?:oz|kg|lb|ml|g|l|fl)?"
    # Note: longer units come before shorter ones so 'lb' matches before 'l'.
    r"|\d+(?:\.\d+)?\s*(?:fl\s*oz|count|ct|pack|pk|pieces|piece|kg|lb|ml|oz|g|l)"
    r"|\d+\s*[-x]\s*\d+"
    r"|\(?\d+(?:\.\d+)?\s*(?:fl\s*)?oz\)?"
    r")\b",
    re.IGNORECASE,
)

# Tokens that vary between SKUs without changing what the food is. Stripped
# during normalization so "Frosted Flakes Original Family Size" and "Frosted
# Flakes" collapse to the same dedup key.
_NOISE_TOKENS: frozenset[str] = frozenset({
    "original", "classic", "regular", "natural", "organic", "premium",
    "low", "fat", "free", "lite", "light", "diet", "zero", "no",
    "added", "reduced", "less", "with", "and", "or", "the",
    "a", "an", "in", "for", "of", "by",
    "family", "value", "sharing", "party", "king", "fun", "size",
    "new", "improved", "select", "choice",
})

# Brand-style names that read awkwardly as evaluation subjects ("Is Smoothie
# Healthy?" — yes, what kind?). Skip these unless the user passes --include-vague.
_VAGUE_NAMES: frozenset[str] = frozenset({
    "smoothie", "yogurt", "snack", "drink", "juice", "water", "milk",
    "bar", "cookie", "chip", "cracker", "cereal", "soda", "tea",
    "coffee", "candy", "dessert",
})


def normalize_food_name(name: str) -> str:
    """Lowercase, strip size/count noise and qualifier tokens, collapse
    whitespace. Used as the dedup key — surface variants of the same
    product family collapse to the same string."""
    s = (name or "").lower()
    s = _SIZE_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9\s'-]", " ", s)
    tokens = [t for t in s.split() if t and t not in _NOISE_TOKENS]
    # Stable order — sort to also collapse word-order variants.
    return " ".join(tokens).strip()


def display_form(name: str) -> str:
    """Clean the surface form for display in titles / slugs. Strips size
    noise the same way the dedup key does, then applies the smart
    title-case helper."""
    s = _SIZE_RE.sub(" ", (name or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" -,")
    return _smart_title_case(s) or "This Food"


def is_vague_name(norm: str) -> bool:
    """True when the normalized name is a single generic category word
    that wouldn't make for a useful 'Is X Healthy?' post on its own."""
    if not norm:
        return True
    parts = norm.split()
    return len(parts) == 1 and parts[0] in _VAGUE_NAMES


def collect_unique_foods(input_csv_path: str) -> list[dict]:
    """Walk input.csv, return one entry per unique normalized food. Each
    entry: {norm, display_name, count, label, purpose, framing, sample_barcode}."""
    p = Path(input_csv_path)
    if not p.exists():
        return []
    seen: dict[str, dict] = {}
    with open(p, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            for side in ("a", "b"):
                disp = (row.get(f"food_{side}_display_name") or "").strip()
                raw = (row.get(f"food_{side}_name") or "").strip()
                base = disp or raw
                if not base:
                    continue
                norm = normalize_food_name(base)
                if not norm:
                    continue
                entry = seen.get(norm)
                if entry is not None:
                    entry["count"] += 1
                    continue
                seen[norm] = {
                    "norm": norm,
                    "display_name": display_form(base),
                    "label": (row.get(f"food_{side}_label") or "").strip(),
                    "purpose": (row.get("purpose") or DEFAULT_PURPOSE).strip(),
                    "framing": (row.get("framing_type") or DEFAULT_FRAMING).strip(),
                    "sample_barcode": (row.get(f"food_{side}_barcode") or "").strip(),
                    "count": 1,
                }
    # Highest-frequency foods first (most-talked-about in the corpus →
    # likely strongest SEO targets), tie-break alphabetically.
    return sorted(seen.values(), key=lambda e: (-e["count"], e["norm"]))


def existing_evaluation_norms(input_csv_path: str) -> set[str]:
    """Normalized food keys already present as evaluation rows in input.csv —
    used to skip dupes."""
    p = Path(input_csv_path)
    if not p.exists():
        return set()
    out: set[str] = set()
    with open(p, newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("format") or "").strip() != "evaluation":
                continue
            base = (
                (row.get("food_a_display_name") or "").strip()
                or (row.get("food_a_name") or "").strip()
            )
            if base:
                out.add(normalize_food_name(base))
    return out


def next_pair_id(input_csv_path: str) -> int:
    p = Path(input_csv_path)
    if not p.exists():
        return 1
    next_id = 1
    with open(p, newline="") as f:
        for row in csv.DictReader(f):
            try:
                v = int((row.get("pair_id") or "").strip())
                if v >= next_id:
                    next_id = v + 1
            except (TypeError, ValueError):
                continue
    return next_id


def existing_columns(input_csv_path: str) -> list[str]:
    """Read the CSV header so appended rows match the file's column order
    exactly (and don't introduce header drift)."""
    p = Path(input_csv_path)
    if not p.exists():
        return []
    with open(p, newline="") as f:
        reader = csv.reader(f)
        try:
            return next(reader)
        except StopIteration:
            return []


def propose_row(entry: dict, pair_id: int) -> dict:
    """Build an input.csv row for a single-food evaluation post."""
    display = entry["display_name"]
    return {
        "pair_id": str(pair_id),
        "food_a_name": display,
        "food_a_display_name": display,
        "food_a_label": entry.get("label") or display.lower(),
        "food_a_barcode": entry.get("sample_barcode") or "",
        "food_b_name": "",
        "food_b_display_name": "",
        "food_b_label": "",
        "food_b_barcode": "",
        "purpose": entry.get("purpose") or DEFAULT_PURPOSE,
        "framing_type": entry.get("framing") or DEFAULT_FRAMING,
        "format": "evaluation",
    }


def _evaluation_postability(count: int) -> int:
    """Synthetic postability for the cleaner sort: foods that appear in
    more comparisons rank higher. 1 mention = 5/10, 6+ mentions = 10/10."""
    return max(1, min(10, 4 + count))


def discovery_rows_from_candidates(
    candidates: list[dict], start_id: int,
) -> list[dict]:
    """Convert evaluation candidates into discovery_results.csv rows so
    they show up in the existing Streamlit Cleaner Mode queue alongside
    comparison/exposure candidates. Reference fields stay blank — the
    cleaner already handles single-food (exposure-style) layouts."""
    out: list[dict] = []
    for i, entry in enumerate(candidates):
        display = entry["display_name"]
        title = _evaluation_title(display)
        out.append({
            "discovery_id": str(start_id + i),
            "candidate_name_raw": display,
            "candidate_brand": "",  # not tracked at this stage
            "candidate_barcode": entry.get("sample_barcode") or "",
            "candidate_display_name_suggested": display,
            "candidate_label_suggested": entry.get("label") or display.lower(),
            "candidate_score": "",
            "candidate_interpretation": (
                f"Single-food evaluation candidate. "
                f"Mentioned in {entry['count']} comparison row(s)."
            ),
            "candidate_what_helps": "",
            "candidate_what_hurts": "",
            "reference_name_raw": "",
            "reference_brand": "",
            "reference_barcode": "",
            "reference_display_name_suggested": "",
            "reference_label_suggested": "",
            "reference_score": "",
            "reference_interpretation": "",
            "score_diff": "",
            "purpose": entry.get("purpose") or DEFAULT_PURPOSE,
            "framing_type": entry.get("framing") or DEFAULT_FRAMING,
            "format": "evaluation",
            "content_angle": f"is {entry['norm']} healthy",
            "postability_score": _evaluation_postability(entry["count"]),
            "product_quality_score": "",
            "status": "pending",
            "error_message": "",
        })
    return out


def existing_evaluation_norms_in_discovery(path: str) -> set[str]:
    """Normalized food keys already enqueued as evaluation rows in
    discovery_results.csv (any status). Lets `--enqueue` skip dupes."""
    p = Path(path)
    if not p.exists():
        return set()
    out: set[str] = set()
    with open(p, newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("format") or "").strip() != "evaluation":
                continue
            base = (
                (row.get("candidate_display_name_suggested") or "").strip()
                or (row.get("candidate_name_raw") or "").strip()
            )
            if base:
                out.add(normalize_food_name(base))
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Discover single-food evaluation candidates from input.csv. "
            "Default is dry-run — prints the slate without writing."
        ),
    )
    p.add_argument("--input-csv", default=INPUT_CSV_DEFAULT,
                   help=f"corpus to scan for unique foods (default {INPUT_CSV_DEFAULT})")
    p.add_argument("--enqueue", action="store_true",
                   help=f"write candidates to {DISCOVERY_CSV_PATH} as "
                        "format='evaluation' rows so they show up in the "
                        "Streamlit Cleaner Mode queue alongside comparisons. "
                        "Recommended workflow.")
    p.add_argument("--csv", dest="write_csv", default=None,
                   help="write candidate rows to this CSV in input.csv "
                        "schema (alternative to --enqueue, for manual review)")
    p.add_argument("--append-to-input", action="store_true",
                   help="append candidate rows directly to --input-csv, "
                        "skipping the cleaner review (use with care)")
    p.add_argument("--limit", type=int, default=None,
                   help="cap the number of candidates emitted")
    p.add_argument("--min-count", type=int, default=1,
                   help="skip candidates that appear in fewer than N "
                        "comparison rows (default 1)")
    p.add_argument("--include-vague", action="store_true",
                   help="don't filter generic single-word categories like "
                        "'smoothie' or 'yogurt'")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    input_path = args.input_csv

    candidates = collect_unique_foods(input_path)
    if not candidates:
        print(f"No candidates — {input_path} is empty or unreadable.",
              file=sys.stderr)
        return 0

    already_done = existing_evaluation_norms(input_path)
    # Also skip foods already in the cleaner queue with status≠approved/skipped.
    if args.enqueue:
        already_done |= existing_evaluation_norms_in_discovery(DISCOVERY_CSV_PATH)
    candidates = [c for c in candidates if c["count"] >= args.min_count]
    candidates = [c for c in candidates if c["norm"] not in already_done]
    if not args.include_vague:
        candidates = [c for c in candidates if not is_vague_name(c["norm"])]
    if args.limit:
        candidates = candidates[: args.limit]

    if not candidates:
        print("No new evaluation candidates after filtering "
              "(already covered, vague, or below --min-count).")
        return 0

    # Preview always prints — even when --append-to-input or --csv is set.
    print(f"=== Evaluation candidates ({len(candidates)}) ===")
    print(f"{'count':>5}  {'title':<55}  slug")
    print(f"{'-' * 5}  {'-' * 55}  {'-' * 40}")
    for entry in candidates:
        title = _evaluation_title(entry["display_name"])
        slug = _evaluation_slug(entry["display_name"])
        print(f"{entry['count']:>5}  {title[:55]:<55}  {slug}")

    if not args.enqueue and not args.write_csv and not args.append_to_input:
        print()
        print("Dry-run only. Options:")
        print(f"  --enqueue              → write to {DISCOVERY_CSV_PATH} "
              "(review in Streamlit Cleaner Mode)")
        print("  --csv FILE             → stage rows in a separate file "
              "(input.csv schema)")
        print("  --append-to-input      → write directly to input.csv "
              "(skips cleaner review)")
        return 0

    if args.enqueue:
        # Reuse discover_candidates' append helper so the discovery_id
        # numbering and append-vs-create-with-header semantics stay
        # consistent with comparison rows already in the queue.
        _, next_discovery_id = load_discovery_state(DISCOVERY_CSV_PATH)
        rows = discovery_rows_from_candidates(candidates, next_discovery_id)
        append_discovery_rows(DISCOVERY_CSV_PATH, rows)
        print(f"\nEnqueued {len(rows)} evaluation candidate(s) to "
              f"{DISCOVERY_CSV_PATH}")
        print("Open Streamlit (./venv/bin/streamlit run input_builder.py) → "
              "Cleaner Mode to review.")
        return 0

    start_id = next_pair_id(input_path)
    rows = [propose_row(c, start_id + i) for i, c in enumerate(candidates)]

    if args.append_to_input:
        target = input_path
        cols = existing_columns(input_path) or list(rows[0].keys())
        # Append without rewriting the existing rows / header.
        with open(target, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=cols)
            for r in rows:
                writer.writerow({c: r.get(c, "") for c in cols})
        print(f"\nAppended {len(rows)} evaluation row(s) to {target}")
    else:  # --csv FILE
        target = args.write_csv
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        cols = list(rows[0].keys())
        with open(target, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=cols)
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
        print(f"\nWrote {len(rows)} evaluation row(s) to {target}")
        print("Review, then copy/paste into input.csv (or rerun with "
              "--append-to-input).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
