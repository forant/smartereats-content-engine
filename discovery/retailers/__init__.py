"""Retailer / source adapters for food candidate ingestion.

Each adapter implements the Retailer ABC. The pipeline orchestrator calls
`fetch(category, limit)` and gets back a list of `Candidate` objects with
provenance fields populated. Everything downstream of fetch (normalize,
score, hub-assign) doesn't care which adapter produced the row.

V1 ships:
- SeedAdapter   — reads a curated JSON of {brand, category, retailer} pairs.
                  Always works. Primary ideation source for V1.
- TargetAdapter — best-effort static-HTML scrape of Target category pages.
                  Fails gracefully when the page is anti-bot defended.
- WalmartAdapter — stub that documents the block. Useful as a placeholder.

To add a new retailer: subclass Retailer, implement `fetch`, register it
in `discovery.pipeline.RETAILERS`.
"""

from .base import Retailer
from .seed import SeedAdapter
from .target import TargetAdapter
from .walmart import WalmartAdapter

__all__ = ["Retailer", "SeedAdapter", "TargetAdapter", "WalmartAdapter"]
