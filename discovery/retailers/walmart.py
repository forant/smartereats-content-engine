"""Walmart category-page adapter.

Walmart's category pages are aggressively defended: Akamai bot manager
challenges, dynamic obfuscated DOM, and a TLS fingerprint check that
blocks non-browser clients. Best-effort static HTML scraping is unlikely
to return data — and bypassing those defenses would violate the project's
"no anti-bot bypass" rule.

This adapter is a placeholder so the abstraction stays consistent. It
always returns [] and prints a one-time hint pointing the user at the
SeedAdapter as the practical V1 source for Walmart-shelf brands. If you
want real Walmart data later, the right move is a partnership API
(SerpApi, Walmart Affiliate, etc.), wired in here as a new fetch path.
"""

from __future__ import annotations

import sys

from ..candidate import Candidate
from .base import Retailer


class WalmartAdapter(Retailer):
    name = "walmart"

    _warned_once: bool = False

    def fetch(self, category: str, limit: int = 50) -> list[Candidate]:
        if not WalmartAdapter._warned_once:
            print(
                "WARNING: WalmartAdapter is a stub — Walmart category pages "
                "are anti-bot defended. Use SeedAdapter for Walmart-shelf "
                "brands (set 'retailer': 'walmart' in config/discovery_seeds.json) "
                "until a partnership API is wired in.",
                file=sys.stderr,
            )
            WalmartAdapter._warned_once = True
        return []
