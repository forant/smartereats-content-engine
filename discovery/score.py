"""Content relevance scoring for food candidates.

This is NOT nutrition scoring. The question this answers is:
    "Is this entity worth creating content about?"

Inputs: a `Candidate` (with brand, canonical_name, category, source,
source_count, etc.).
Output: (relevance_score 0-100, reasons list).

Heuristic + explainable. Each rule contributes a delta and a short
reason; the final score is clamped to [0, 100]. Easy to tune by tweaking
the per-rule weights or adding new rules.
"""

from __future__ import annotations

import re

from .candidate import Candidate


# Brand recognition. Two tiers:
#   "household": well-known to the median US shopper (huge SEO target).
#   "premium":   recognizable in the health/fitness aisle (good SEO).
_HOUSEHOLD_BRANDS: frozenset[str] = frozenset(s.lower() for s in (
    "Quest", "Barebells", "Pure Protein", "RXBAR", "Clif",
    "Kind", "Nature Valley", "Special K", "Kashi", "Quaker",
    "Chobani", "Yoplait", "Fage", "Oikos", "Dannon",
    "Coca-Cola", "Pepsi", "Gatorade", "Powerade", "Mountain Dew",
    "Lay's", "Doritos", "Tostitos", "Pringles", "Cheetos", "Cheez-Its",
    "Goldfish", "Triscuit", "Wheat Thins", "Ritz",
    "Snickers", "Mars", "Hershey", "Reese's", "M&M's", "Skittles", "Starburst",
    "Cheerios", "Frosted Flakes", "Lucky Charms", "Honey Bunches of Oats",
    "Lean Cuisine", "Healthy Choice", "Stouffer's", "Marie Callender's",
    "Smart Ones", "Amy's", "EVOL",
    "Naked Juice", "Tropicana", "Minute Maid", "Ocean Spray", "V8",
    "Welch's", "Capri Sun", "Honest Kids",
    "Halo Top", "Ben & Jerry's", "Haagen-Dazs", "Talenti", "Breyers",
    "Pop-Tarts", "Eggo", "Bisquick", "Kellogg's",
    "Kirkland", "Kirkland Signature",
))

_PREMIUM_BRANDS: frozenset[str] = frozenset(s.lower() for s in (
    "Fairlife", "Core Power", "Muscle Milk", "Premier Protein",
    "Owyn", "Orgain", "Soylent", "Ensure", "Glucerna", "Boost",
    "Larabar", "Perfect Bar", "ThinkThin", "Built", "Power Crunch",
    "GoMacro", "Bobo's", "Bobos", "Aloha",
    "Poppi", "Olipop", "Spindrift", "Liquid Death", "AHA", "La Croix",
    "Bubly", "Topo Chico",
    "Siggi's", "Stonyfield", "Wallaby", "Fage Total",
    "Skinny Pop", "Pirate's Booty", "Pop Corners", "Bare Snacks",
    "Annie's Homegrown", "Beanitos", "Hippeas",
    "Halo Top", "Talenti", "Yasso", "Enlightened",
    "Magic Spoon", "Three Wishes", "Catalina Crunch",
    "Made Good", "Larabar Minis", "RXBAR Minis",
))


# Categories with strong known content potential — supports goal-oriented
# guides ("Best high-protein snacks", "Best frozen meals for weight loss").
_STRONG_CATEGORIES: frozenset[str] = frozenset({
    "protein-bars", "protein-drinks", "protein-shakes",
    "frozen-meals", "granola-bars", "snack-bars",
    "yogurts", "greek-yogurts", "protein-yogurts",
    "sparkling-water", "energy-drinks", "soda", "prebiotic-soda",
    "cereals", "breakfast-cereals", "oatmeal",
    "snacks", "chips", "popcorn", "crackers",
})

# Retailers with strong query intent — "Best Costco X" is a real search.
_GOAL_RICH_RETAILERS: frozenset[str] = frozenset({
    "costco", "trader joes", "trader-joes", "whole foods", "whole-foods",
    "aldi", "kirkland",
})

# Hint words in the canonical name that suggest a specific obscure SKU
# rather than a product line. Penalize lightly — keeps drift in check.
_OBSCURE_HINTS: frozenset[str] = frozenset({
    "limited", "edition", "imported", "international", "sample",
})


def score_relevance(c: Candidate, source_count: int = 1) -> tuple[int, list[str]]:
    """Compute a 0-100 relevance score with explainable reasons.

    `source_count` is how many adapters or seed entries surfaced this
    canonical entity — passing 1 disables the multi-source boost. The
    pipeline passes the actual count after dedup."""
    score = 50  # neutral baseline; modest score so unknown entities don't ship
    reasons: list[str] = []

    brand_lc = (c.brand or "").lower().strip()
    name_lc = (c.canonical_name or "").lower()
    category_lc = (c.category or "").lower().strip()
    retailer_lc = (c.retailer or "").lower().strip()

    # Brand recognition (largest single signal — drives SEO traffic).
    if brand_lc in _HOUSEHOLD_BRANDS:
        score += 25
        reasons.append(f"Household brand ({c.brand})")
    elif brand_lc in _PREMIUM_BRANDS:
        score += 18
        reasons.append(f"Recognizable health/fitness brand ({c.brand})")
    elif brand_lc:
        score += 5
        reasons.append("Branded product (less recognized)")
    else:
        score -= 10
        reasons.append("No brand identified")

    # Category fit.
    if category_lc in _STRONG_CATEGORIES:
        score += 12
        reasons.append(f"Strong content category ({category_lc})")
    elif category_lc:
        score += 3
        reasons.append(f"Category {category_lc!r} present")
    else:
        score -= 5
        reasons.append("No category assigned")

    # Retailer prominence — Costco / Kirkland / Trader Joe's open up
    # retailer-guide content.
    if retailer_lc in _GOAL_RICH_RETAILERS or "kirkland" in brand_lc:
        score += 10
        reasons.append(f"Retailer drives goal-guide content ({c.retailer or 'kirkland'})")

    # Multi-source signal: same entity surfaced by >1 adapter / seed.
    if source_count >= 3:
        score += 10
        reasons.append(f"Surfaced by {source_count} sources")
    elif source_count == 2:
        score += 5
        reasons.append("Surfaced by 2 sources")

    # Penalize obscure-SKU hints in the canonical name.
    obscure_hits = [w for w in _OBSCURE_HINTS if w in name_lc]
    if obscure_hits:
        score -= 8
        reasons.append(f"Obscure-SKU hint: {', '.join(obscure_hits)}")

    # Penalize titles that still look like a single SKU after normalization
    # (suggests the canonical collapse missed something).
    if _looks_like_single_sku(c.canonical_name):
        score -= 5
        reasons.append("Canonical name still reads like a single SKU")

    # Reward presence of an aliases list — means the dedupe pass found
    # variants, which usually correlates with a real product line.
    if len(c.aliases) >= 2:
        score += 5
        reasons.append(f"Multiple source variants found ({len(c.aliases)})")

    return max(0, min(100, score)), reasons


def _looks_like_single_sku(name: str) -> bool:
    """Heuristic: long names with multiple flavor / size hints suggest the
    canonical pass left raw-SKU residue."""
    if not name:
        return False
    if len(name) > 50:
        return True
    if re.search(r"\b\d+\s*(?:oz|g|lb|ml|pack|ct)\b", name, re.IGNORECASE):
        return True
    return False
