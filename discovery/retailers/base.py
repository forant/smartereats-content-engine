"""Retailer adapter interface.

The contract is intentionally narrow:
- `fetch(category, limit)` returns a list of `Candidate` objects.
- Adapters MUST fail gracefully (return [] + print warning) on network
  errors, anti-bot blocks, or layout changes — never raise.
- Adapters MUST be polite — sleep between requests, set a User-Agent,
  honor robots.txt, never hammer endpoints.
- Adapters MUST NOT bypass paywalls, logins, anti-bot, or use private APIs.
"""

from __future__ import annotations

import abc
from typing import Optional

from ..candidate import Candidate


# Politeness defaults — override per-adapter if a site asks for more.
DEFAULT_USER_AGENT = (
    "SmarterEatsContentEngine/0.1 "
    "(food candidate discovery; +https://smartereats.com/contact)"
)
DEFAULT_REQUEST_TIMEOUT_S = 15
DEFAULT_REQUEST_DELAY_S = 1.0  # between requests in the same fetch() call


class Retailer(abc.ABC):
    """ABC for retailer / source adapters."""

    name: str = ""             # short identifier, used in source/retailer fields
    user_agent: str = DEFAULT_USER_AGENT
    timeout_s: int = DEFAULT_REQUEST_TIMEOUT_S
    request_delay_s: float = DEFAULT_REQUEST_DELAY_S

    @abc.abstractmethod
    def fetch(self, category: str, limit: int = 50) -> list[Candidate]:
        """Return up to `limit` candidate rows for `category`. Empty list
        on any failure — never raises. Each candidate gets `source` and
        `retailer` populated."""
        ...

    def supports(self, category: str) -> bool:
        """Whether this adapter has any chance of returning data for the
        given category. Default: always supports — subclasses can narrow."""
        return True


def make_candidate(
    *,
    raw_name: str,
    brand: str = "",
    category: str = "",
    source: str,
    retailer: str = "",
    source_url: str = "",
    source_rank: Optional[int] = None,
    upc: str = "",
    package_size: str = "",
    image_url: str = "",
) -> Candidate:
    """Build a fresh Candidate from an adapter, with provenance set and a
    UTC discovered_at stamp. Normalization / scoring fill in the rest."""
    c = Candidate(
        raw_name=raw_name.strip(),
        brand=brand.strip(),
        category=category.strip(),
        source=source.strip(),
        retailer=(retailer or source).strip(),
        source_url=source_url.strip(),
        source_rank=source_rank,
        upc=upc.strip(),
        package_size=package_size.strip(),
        image_url=image_url.strip(),
    )
    c.now_stamp()
    return c
