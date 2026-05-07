"""Topic-hub assignment for food candidates.

Maps category + brand + retailer signals onto the website's topic-hub
slugs. Output should always be a subset of the V1 hub list below — the
auditor enforces hub validity downstream when posts are generated.

Each candidate gets 1+ hubs. We intentionally over-include rather than
under-include: it's easier for a reviewer to remove an irrelevant hub
in Streamlit than to remember to add a missing one.
"""

from __future__ import annotations

from .candidate import Candidate


# V1 hub list. These are slugs the website's /content/topics/ should already
# have (the auditor will catch any drift). Edit as the website grows.
V1_HUBS: tuple[str, ...] = (
    "high-protein-snacks",
    "weight-loss-snacks",
    "healthy-costco-foods",
    "protein-bars",
    "healthy-frozen-meals",
    "healthy-drinks",
    "healthy-convenience-foods",
)


# Category → hub mapping. A category can map to multiple hubs.
_CATEGORY_HUBS: dict[str, tuple[str, ...]] = {
    "protein-bars": (
        "protein-bars", "high-protein-snacks", "weight-loss-snacks",
        "healthy-convenience-foods",
    ),
    "protein-drinks": (
        "high-protein-snacks", "healthy-drinks", "healthy-convenience-foods",
    ),
    "protein-shakes": (
        "high-protein-snacks", "healthy-drinks", "healthy-convenience-foods",
    ),
    "frozen-meals": (
        "healthy-frozen-meals", "weight-loss-snacks",
        "healthy-convenience-foods",
    ),
    "granola-bars": (
        "high-protein-snacks", "healthy-convenience-foods",
    ),
    "snack-bars": (
        "high-protein-snacks", "healthy-convenience-foods",
    ),
    "yogurts": (
        "high-protein-snacks", "healthy-convenience-foods",
    ),
    "greek-yogurts": (
        "high-protein-snacks", "healthy-convenience-foods",
    ),
    "protein-yogurts": (
        "high-protein-snacks", "healthy-convenience-foods",
    ),
    "sparkling-water": (
        "healthy-drinks",
    ),
    "soda": (
        "healthy-drinks",
    ),
    "prebiotic-soda": (
        "healthy-drinks",
    ),
    "energy-drinks": (
        "healthy-drinks",
    ),
    "cereals": (
        "healthy-convenience-foods",
    ),
    "breakfast-cereals": (
        "healthy-convenience-foods",
    ),
    "oatmeal": (
        "healthy-convenience-foods",
    ),
    "snacks": (
        "healthy-convenience-foods",
    ),
    "chips": (
        "healthy-convenience-foods",
    ),
    "popcorn": (
        "healthy-convenience-foods",
    ),
    "crackers": (
        "healthy-convenience-foods",
    ),
}


# Retailer → extra hub. Add when the retailer has dedicated reader intent.
_RETAILER_HUBS: dict[str, tuple[str, ...]] = {
    "costco": ("healthy-costco-foods",),
    "kirkland": ("healthy-costco-foods",),
}


def assign_hubs(c: Candidate, allowed: set[str] | None = None) -> list[str]:
    """Pick the hubs that fit this candidate. Always returns a list; may
    be empty for unrecognized categories (Streamlit reviewer can fix).

    `allowed`, when provided, is the live `WEBSITE_TOPICS_DIR` slug set —
    we intersect with it so we never assign a hub that doesn't exist on
    disk. Pass None to skip the live check (V1 default — the auditor
    re-checks before publish)."""
    out: list[str] = []
    seen: set[str] = set()

    cat = (c.category or "").strip().lower()
    for h in _CATEGORY_HUBS.get(cat, ()):
        if h not in seen:
            seen.add(h)
            out.append(h)

    retailer = (c.retailer or "").strip().lower()
    brand = (c.brand or "").strip().lower()
    for key in (retailer, brand):
        for h in _RETAILER_HUBS.get(key, ()):
            if h not in seen:
                seen.add(h)
                out.append(h)
        if "kirkland" in brand and "healthy-costco-foods" not in seen:
            seen.add("healthy-costco-foods")
            out.append("healthy-costco-foods")

    if allowed is not None:
        out = [h for h in out if h in allowed]
    return out
