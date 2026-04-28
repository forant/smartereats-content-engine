"""Punchy bullet transformer.

Turns the backend's whatHelps / whatHurts strings into short, hard-hitting,
emotionally-aligned bullet phrases for the social card. Deterministic for
now; the dispatcher leaves a clean OpenAI hook point for later.

Rules enforced here (per spec):
    - ≤ 6 words per bullet
    - single phrase (no commas, no clauses)
    - emotionally framed, not neutral
    - no hedging language: can / may / helps / offers / some / relatively
    - role-aligned:
        AVOID / SWAP OUT  → only negatives, increase discomfort
        BEST / PICK       → only positives, reinforce action
        NEUTRAL           → emphasize sameness, create doubt
    - ignore vitamins, vague benefits, flavor descriptions
    - max 2 (preferred), 3 absolute max
"""

from __future__ import annotations

import re
from typing import Optional


# --- Light parsing of the " ; "-joined CSV column --------------------------

def split_raw_bullets(raw: str) -> list[str]:
    """Split a `" ; "`-joined whatHelps/whatHurts column into a list."""
    if not raw:
        return []
    parts = re.split(r"\s*;\s*", str(raw))
    return [p.strip() for p in parts if p and p.strip()]


# --- Theme + polarity detection --------------------------------------------

# Theme lookup table — order matters: longer / more specific keywords first.
_THEME_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("added_sugar", ("added sugar", "added-sugar")),
    ("sugar",       ("sugar",)),
    ("protein",     ("protein",)),
    ("fiber",       ("fiber", "fibre")),
    ("processing",  ("ultra-processed", "ultra processed", "ultraprocessed",
                     "processed", "refined",
                     "real ingredient", "whole food", "whole-food",
                     "minimally processed")),
    ("satiety",     ("fill you", "fill up", "satiat", "satiety",
                     "keeps you full", "keep you full", "filling")),
    ("calories",    ("empty calorie", "calorie-dense", "calorie dense",
                     "high calorie")),
]

# Themes we deliberately ignore per spec ("minor vitamins / vague benefits").
_IGNORE_TOKENS = (
    " vitamin ", " calcium ", " iron ", " potassium ", " magnesium ",
    " zinc ", " omega ",
)

# "20g protein", "35 g of sugar", "10g added sugar"
_NUMERIC_RE = re.compile(
    r"(\d+)\s*g(?:rams?)?\s*(?:of\s+)?(added\s+sugar|sugar|protein|fiber|fibre)",
    re.I,
)


def _theme_of(raw: str) -> Optional[str]:
    rl = " " + raw.lower() + " "
    if any(tok in rl for tok in _IGNORE_TOKENS):
        return None
    for theme, kws in _THEME_KEYWORDS:
        for kw in kws:
            if kw in rl:
                return theme
    return None


# --- Phrase library --------------------------------------------------------

_PHRASES: dict[tuple[str, str], str] = {
    # AVOID / SWAP OUT — increase discomfort, reinforce "bad choice"
    ("avoid", "added_sugar"): "Sugar overload",
    ("avoid", "sugar"):       "Sugar overload",
    ("avoid", "protein"):     "Barely any protein",
    ("avoid", "fiber"):       "No real fiber",
    ("avoid", "processing"):  "Ultra-processed junk",
    ("avoid", "satiety"):     "Won't fill you up",
    ("avoid", "calories"):    "Empty calories",
    # BEST CHOICE / PICK THIS — reinforce action
    ("pick",  "added_sugar"): "Less sugar",
    ("pick",  "sugar"):       "Less sugar",
    ("pick",  "protein"):     "Real protein",
    ("pick",  "fiber"):       "Real fiber",
    ("pick",  "processing"):  "Real ingredients",
    ("pick",  "satiety"):     "Actually keeps you full",
    ("pick",  "calories"):    "Real fuel",
}


def _punch_one(raw: str, role: str) -> Optional[str]:
    """Transform one raw bullet into a phrase, or None to drop it.

    The caller is expected to have already split bullets by polarity
    (helps → role='pick', hurts → role='avoid'), so we trust the role
    rather than re-deriving polarity from the text.
    """
    if not raw or len(raw.strip()) < 2:
        return None

    # Numeric facts get a vivid concrete phrase.
    m = _NUMERIC_RE.search(raw)
    if m:
        amount = m.group(1)
        nutrient = m.group(2).lower()
        if "sugar" in nutrient:
            if role == "avoid":
                return f"{amount}g sugar hit"
            if role == "pick":
                return "Less sugar"
        elif "protein" in nutrient:
            if role == "pick":
                return f"{amount}g protein"
            if role == "avoid":
                return "Barely any protein"
        elif "fiber" in nutrient or "fibre" in nutrient:
            if role == "pick":
                return f"{amount}g fiber"
            if role == "avoid":
                return "No real fiber"

    theme = _theme_of(raw)
    if theme is None:
        return None
    return _PHRASES.get((role, theme))


# --- Public API ------------------------------------------------------------

def punch_up_bullets_deterministic(
    raw_bullets: list[str],
    role: str,
    *,
    max_bullets: int = 2,
) -> list[str]:
    """Transform raw whatHelps/whatHurts into ≤6-word punchy phrases.

    role: 'avoid' | 'pick' | 'neutral'.
    """
    if role == "neutral":
        # Neutral cards (close-call comparisons) ignore the raw lists and emit
        # sameness/doubt phrases. Theme-aware so the bullets at least nod to
        # what's actually wrong with both items.
        combined = " ".join(raw_bullets or []).lower()
        if "sugar" in combined:
            return ["Same sugar problem", "No real upgrade"][:max_bullets]
        if any(p in combined for p in ("process", "refined", "ultra")):
            return ["Same processed junk", "No real upgrade"][:max_bullets]
        return ["Basically the same", "No real upgrade"][:max_bullets]

    out: list[str] = []
    seen_phrases: set[str] = set()
    seen_themes: set[str] = set()
    for raw in (raw_bullets or []):
        phrase = _punch_one(raw, role)
        if not phrase:
            continue
        if len(phrase.split()) > 6:  # safety net — templates already comply
            continue
        if phrase in seen_phrases:
            continue
        theme = _theme_of(raw) or "_other"
        if theme in seen_themes:
            continue
        seen_phrases.add(phrase)
        seen_themes.add(theme)
        out.append(phrase)
        if len(out) >= max_bullets:
            break
    return out


def punch_up_bullets_with_openai(  # pragma: no cover - placeholder
    raw_bullets: list[str],
    role: str,
    *,
    max_bullets: int = 2,
) -> list[str]:
    """TODO: wire to OpenAI once integration is approved.

    Should accept the same inputs and return a list of ≤6-word phrases
    aligned to the role. Until then, callers fall back to the
    deterministic transformer.
    """
    raise NotImplementedError(
        "OpenAI bullet generation is not yet integrated; use "
        "punch_up_bullets_deterministic for now."
    )


def punch_up_bullets(
    raw_bullets: list[str],
    role: str,
    *,
    max_bullets: int = 2,
) -> list[str]:
    """Top-level dispatcher. Always uses the deterministic transformer for
    now — swap to OpenAI here when ready."""
    return punch_up_bullets_deterministic(raw_bullets, role, max_bullets=max_bullets)
