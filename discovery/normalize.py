"""Name normalization + dedup for food candidates.

Retailer titles are noisy: "Quest Nutrition Protein Bar Chocolate Chip
Cookie Dough 2.12oz 4ct" should collapse to a canonical entity "Quest
Protein Bars" with the original title kept as an alias for traceability.

Key invariants:
- The CANONICAL NAME stays brand-led + category-led, drops flavor / size /
  count / pack / "nutrition" / "natural" type marketing fluff.
- The DEDUP KEY is `(brand_norm, head_noun_phrase)` — different flavors of
  the same product line collapse onto the same key.
- Source variants are preserved in `aliases` so we can audit how a
  canonical entity was constructed.
"""

from __future__ import annotations

import re
from collections import OrderedDict

from .candidate import Candidate


# Trailing size / count noise that almost always appears on retailer titles.
# Order matters — longest first so 'fl oz' beats 'oz'.
_SIZE_RE = re.compile(
    r"\b(?:net\s*wt\.?\s*[\d./\s]+\s*(?:oz|kg|lb|ml|g|l|fl)?"
    r"|\d+(?:\.\d+)?\s*(?:fl\s*oz|count|ct|pack|pk|pieces|piece|kg|lb|ml|oz|g|l)"
    r"|\d+\s*[-x]\s*\d+"
    r"|\(?\d+(?:\.\d+)?\s*(?:fl\s*)?oz\)?)\b",
    re.IGNORECASE,
)

# Marketing modifiers that shouldn't change the canonical entity.
_NOISE_TOKENS: frozenset[str] = frozenset({
    "original", "classic", "regular", "natural", "all", "naturally",
    "premium", "select", "choice", "new", "improved", "limited", "edition",
    "low", "fat", "free", "lite", "light", "diet", "zero", "no", "added",
    "reduced", "less", "100%", "100",
    "with", "and", "or", "the", "a", "an", "in", "for", "of", "by",
    "family", "value", "sharing", "party", "king", "fun", "snack",
    "size", "sized", "pack", "edition",
    "gluten-free", "gluten", "non-gmo", "organic", "vegan", "kosher",
})

# Common flavor / variant tokens. When we see them between the brand and
# the head noun, we drop them from the canonical name (but keep the raw
# title as an alias). NOT exhaustive — extending is cheap.
_FLAVOR_TOKENS: frozenset[str] = frozenset({
    "chocolate", "vanilla", "strawberry", "blueberry", "peanut", "butter",
    "caramel", "cookie", "dough", "cinnamon", "honey", "maple", "brown",
    "sugar", "lemon", "lime", "orange", "raspberry", "berry", "mixed",
    "cookies", "cream", "creamy", "smores", "smore", "mocha", "coffee",
    "mint", "espresso", "hazelnut", "coconut", "banana", "apple", "peach",
    "mango", "pineapple", "watermelon", "cherry", "grape", "pomegranate",
    "acai", "pumpkin", "salted", "cheddar", "ranch", "barbecue", "bbq",
    "sour", "spicy", "cool", "hot", "buffalo", "garlic", "italian",
    "herb", "rosemary", "thyme", "parmesan", "cheese", "white", "dark",
    "milk", "double", "triple", "extra", "ultra",
    "fruit", "fruits", "tropical", "jalapeno", "habanero", "nacho",
})

# Common product-type "head nouns" — what the entity actually IS. The
# canonical name is constructed as `{brand} {head}` (pluralized when the
# original head is plural).
_HEAD_NOUNS: frozenset[str] = frozenset({
    # Plural-form heads (kept plural in canonical names).
    "bars", "chips", "cookies", "crackers", "crisps", "snacks", "flakes",
    "gummies", "pops", "wraps", "rolls", "puffs", "cubes", "nuggets",
    "sticks", "wedges", "strips", "fries", "pretzels", "berries",
    "olives", "pickles", "tomatoes", "beans", "lentils", "oats",
    "raisins", "nuts", "seeds", "almonds", "veggies", "shakes",
    "drinks", "waters", "juices", "smoothies", "meals", "yogurts",
    "cereals", "granolas", "bites", "thins", "minis", "squares", "straws",
    # Singular / mass-noun heads.
    "bar", "chip", "cookie", "cracker", "snack", "drink", "shake",
    "water", "juice", "smoothie", "meal", "yogurt", "cereal", "granola",
    "milk", "oatmeal", "soda", "tea", "coffee", "soup", "pasta", "rice",
    "bread", "butter",
})

# Plural-form ⇄ singular-form lookup so we can pluralize a singular head
# when the brand sells "Quest Bar" (singular SKU title) but should canonicalize
# to "Quest Bars" (plural product line).
_PLURAL_OF_SINGULAR: dict[str, str] = {
    "bar": "bars", "chip": "chips", "cookie": "cookies",
    "cracker": "crackers", "snack": "snacks", "drink": "drinks",
    "shake": "shakes", "water": "waters", "juice": "juices",
    "smoothie": "smoothies", "meal": "meals", "yogurt": "yogurts",
    "cereal": "cereals", "granola": "granolas",
}

# Brand names that are themselves multi-word — preserve verbatim in
# canonical names, don't try to collapse them. Compared against the
# leading prefix of the lowercased raw_name.
_KNOWN_MULTI_WORD_BRANDS: tuple[str, ...] = (
    "nature valley", "kind bar", "pop tarts", "pop-tarts", "ben & jerry's",
    "ben and jerry's", "fairlife core power", "fairlife yup", "core power",
    "general mills", "post foods", "kellogg's", "lean cuisine",
    "amy's kitchen", "amy s kitchen", "annie's homegrown", "morning star",
    "morningstar farms", "evol foods", "stouffer's", "marie callender's",
    "healthy choice", "smart ones", "udi's", "udis", "celsius energy",
    "celsius drinks", "muscle milk", "premier protein", "owyn protein",
    "rxbar minis", "halo top", "naked juice", "honest tea", "honest kids",
    "minute maid", "ocean spray", "welch's", "perfect bar", "kashi go",
    "special k", "frosted flakes", "rice krispies", "honey nut cheerios",
    "cheez-it", "cheez-its", "cheez it", "cheez its",
)


def _collapse_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _strip_size_noise(s: str) -> str:
    return _collapse_spaces(_SIZE_RE.sub(" ", s))


def _strip_punct(s: str) -> str:
    """Keep alphanumerics, spaces, hyphens, apostrophes, ampersands."""
    return re.sub(r"[^\w\s'\-&]", " ", s)


def normalize_brand(brand: str) -> str:
    """Lowercase + collapse whitespace; preserve apostrophes and hyphens."""
    return _collapse_spaces((brand or "").lower())


def _detect_known_brand(raw_lower: str) -> str:
    """Try to match the longest known multi-word brand at the start of the
    raw name. Returns the matched brand verbatim (lowercase) or empty."""
    for brand in sorted(_KNOWN_MULTI_WORD_BRANDS, key=len, reverse=True):
        if raw_lower.startswith(brand + " ") or raw_lower == brand:
            return brand
    return ""


def _tokens(s: str) -> list[str]:
    return [t for t in _strip_punct(s).split() if t]


# Multi-word flavor compounds that contain words which are ALSO valid head
# nouns. When we see one of these in a title, the constituent tokens get
# masked out so head detection doesn't latch onto them — for example,
# "Cookie" in "Cookies & Cream Protein Bar" is the flavor, not the head;
# "Bar" is the head.
_FLAVOR_COMPOUNDS: tuple[tuple[str, ...], ...] = (
    ("cookies", "&", "cream"),
    ("cookies", "and", "cream"),
    ("cookie", "dough"),
    ("cookie", "crisp"),
    ("chocolate", "chip"),
    ("chocolate", "chips"),
    ("peanut", "butter"),
    ("salted", "caramel"),
    ("birthday", "cake"),
    ("vanilla", "ice", "cream"),
    ("brown", "sugar"),
    ("maple", "brown", "sugar"),
    ("apple", "pie"),
    ("banana", "bread"),
    ("pumpkin", "spice"),
    ("smores",),
    ("s'mores",),
)


def _flavor_compound_indices(tokens: list[str]) -> set[int]:
    """Indices of tokens that are part of a known flavor compound. The
    head-noun scan skips these so 'Cookies & Cream Protein Bar' resolves
    to head 'Bar' instead of head 'Cookies'."""
    masked: set[int] = set()
    lower = [t.lower() for t in tokens]
    for compound in _FLAVOR_COMPOUNDS:
        clen = len(compound)
        if clen == 0:
            continue
        i = 0
        while i <= len(lower) - clen:
            if tuple(lower[i : i + clen]) == compound:
                masked.update(range(i, i + clen))
                i += clen
            else:
                i += 1
    return masked


def _find_head_noun(tokens: list[str]) -> tuple[int, str]:
    """Find the rightmost token that is a recognized head noun, skipping
    tokens masked by `_flavor_compound_indices` so flavor words don't get
    misread as the product type. Returns (index, token), or (-1, '')."""
    masked = _flavor_compound_indices(tokens)
    for i in range(len(tokens) - 1, -1, -1):
        if i in masked:
            continue
        if tokens[i].lower() in _HEAD_NOUNS:
            return i, tokens[i].lower()
    return -1, ""


def canonicalize_name(raw_name: str, brand_hint: str = "") -> tuple[str, str]:
    """Return (canonical_name, brand) from a noisy retailer title.

    Strategy:
    1. Strip size / count noise.
    2. Identify the brand — explicit hint wins; otherwise sniff a known
       multi-word brand prefix; otherwise take the first capitalized word.
    3. Find the rightmost head noun token. If singular, pluralize it (so
       'Quest Protein Bar' canonicalizes to 'Quest Bars').
    4. Compose `{Brand Title-Case} {Head Plural Title-Case}`.
    5. Fall back to brand alone, or to the cleaned raw, when no head noun
       is detected.

    Examples:
    - 'Quest Nutrition Protein Bar Chocolate Chip Cookie Dough 2.12oz'
      → ('Quest Bars', 'Quest')
    - 'Barebells Cookies & Cream Protein Bar'
      → ('Barebells Bars', 'Barebells')
    - 'Fairlife Core Power Chocolate Protein Shake'
      → ('Fairlife Core Power Shakes', 'Fairlife Core Power')
    - 'Poppi Prebiotic Soda Strawberry Lemon'
      → ('Poppi Soda', 'Poppi')
    """
    if not raw_name:
        return "", brand_hint.strip()
    raw = _strip_size_noise(raw_name)
    raw_lower = raw.lower()

    # 1. Brand resolution.
    brand_norm = ""
    brand_display = ""
    if brand_hint and brand_hint.strip():
        brand_norm = brand_hint.strip().lower()
        brand_display = brand_hint.strip()
    else:
        sniffed = _detect_known_brand(raw_lower)
        if sniffed:
            brand_norm = sniffed
            brand_display = " ".join(w.capitalize() for w in sniffed.split())
        else:
            tks = _tokens(raw)
            if tks:
                brand_norm = tks[0].lower()
                brand_display = tks[0]

    # 2. Strip the brand off the head of the name so we don't double-count.
    name_after_brand = raw
    if brand_norm and raw_lower.startswith(brand_norm + " "):
        name_after_brand = raw[len(brand_norm):].lstrip()
    elif brand_norm and raw_lower == brand_norm:
        name_after_brand = ""

    tks_remaining = _tokens(name_after_brand)
    # 3. Head-noun detection.
    head_idx, head = _find_head_noun(tks_remaining)
    if head:
        head_plural = _PLURAL_OF_SINGULAR.get(head, head)
        canonical = f"{_title_case_words(brand_display)} {head_plural.title()}"
        return _collapse_spaces(canonical), brand_display

    # 4. No head noun detected — fall back to brand alone (cleaner than
    # carrying flavor / fluff into the canonical entity).
    if brand_display:
        return _title_case_words(brand_display), brand_display
    cleaned = _strip_punct(raw)
    return _title_case_words(cleaned), brand_display


def _title_case_words(s: str) -> str:
    parts = []
    for w in s.split():
        if not w:
            continue
        # Preserve apostrophes and hyphens; capitalize the first alpha char
        # of each piece.
        sub = re.split(r"(['\-])", w)
        capped = []
        for piece in sub:
            if piece in ("'", "-"):
                capped.append(piece)
            elif piece:
                capped.append(piece[0].upper() + piece[1:].lower())
        parts.append("".join(capped))
    return " ".join(parts)


def dedup_key_for(canonical_name: str, brand: str) -> str:
    """Deterministic key for collapsing variants. Same canonical entity =
    same key, regardless of source ordering or surface variation."""
    a = (brand or "").strip().lower()
    b = (canonical_name or "").strip().lower()
    return f"{a}|{b}"


def dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Collapse candidates with the same dedup_key into one entity. The
    first candidate seen wins (so source ordering controls representative
    choice — usually the SeedAdapter row, since it has the cleanest name).
    Aliases from collapsed candidates are folded in for traceability."""
    by_key: OrderedDict[str, Candidate] = OrderedDict()
    for c in candidates:
        key = dedup_key_for(c.canonical_name, c.brand)
        if not key.strip("|"):
            continue
        c.dedup_key = key
        first = by_key.get(key)
        if first is None:
            by_key[key] = c
            continue
        # Fold this candidate into the existing canonical entity.
        if c.raw_name and c.raw_name not in first.aliases and c.raw_name != first.raw_name:
            first.aliases.append(c.raw_name)
        for alias in c.aliases:
            if alias and alias not in first.aliases and alias != first.raw_name:
                first.aliases.append(alias)
        # Carry over UPC / image when missing on the canonical entity.
        if not first.upc and c.upc:
            first.upc = c.upc
        if not first.image_url and c.image_url:
            first.image_url = c.image_url
        if not first.source_url and c.source_url:
            first.source_url = c.source_url
        # Confidence: keep the higher.
        if c.confidence > first.confidence:
            first.confidence = c.confidence
    return list(by_key.values())
