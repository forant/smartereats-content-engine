"""Target.com category-page adapter (best-effort).

Target's product grid is rendered client-side via React/Redux, but the
initial state is shipped in a `<script id="__TGT_DATA__">` (or similar)
JSON island in the HTML — when the page returns 200 without a Cloudflare
challenge, we can extract product names + brand fields from there.

This is best-effort: Target updates their layout / DOM IDs frequently, and
they aggressively rate-limit / 403 unfamiliar User-Agents. When the fetch
fails or the JSON layout changes, this adapter logs a warning and returns
[] — never raises.

Polite behavior:
- One request per category, no pagination crawling in V1.
- Default 1.5s delay before request (in case multiple categories run
  back-to-back).
- Respects the User-Agent set in `Retailer.user_agent`.
- Honors `--dry-run` upstream (the pipeline never calls `fetch` in dry-run).

If you want this adapter to consistently produce data for production, that
work belongs in a downstream task: rotating User-Agents, residential
proxies, or a partnership API. Out of scope for V1.
"""

from __future__ import annotations

import json
import re
import sys
import time
from typing import Optional

import requests

from ..candidate import Candidate
from .base import Retailer, make_candidate


# Static category → URL map. Add entries to expand coverage.
_CATEGORY_URLS: dict[str, str] = {
    "protein-bars":   "https://www.target.com/c/protein-energy-bars-snacks-grocery/-/N-tj4vr",
    "protein-drinks": "https://www.target.com/c/protein-shakes-drinks-meal-replacements-grocery/-/N-tj4vp",
    "frozen-meals":   "https://www.target.com/c/frozen-meals-foods-grocery/-/N-tj4uo",
    "sparkling-water":"https://www.target.com/c/sparkling-water-beverages-grocery/-/N-tj4uy",
    "granola-bars":   "https://www.target.com/c/granola-cereal-bars-snacks-grocery/-/N-tj4vu",
}


# Embedded-JSON island Target uses for SSR state. Layout changes break this;
# treat it as a heuristic, not a contract.
_JSON_ISLAND_RE = re.compile(
    r'<script[^>]+id="__TGT_DATA__"[^>]*>\s*({.*?})\s*</script>',
    re.DOTALL,
)
_FALLBACK_JSON_LD_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL,
)


class TargetAdapter(Retailer):
    name = "target"
    request_delay_s = 1.5

    def supports(self, category: str) -> bool:
        return (category or "").strip().lower() in _CATEGORY_URLS

    def fetch(self, category: str, limit: int = 50) -> list[Candidate]:
        cat = (category or "").strip().lower()
        url = _CATEGORY_URLS.get(cat)
        if not url:
            print(
                f"WARNING: TargetAdapter has no URL for category {cat!r}. "
                "Add one to _CATEGORY_URLS or use SeedAdapter.",
                file=sys.stderr,
            )
            return []
        try:
            time.sleep(self.request_delay_s)
            resp = requests.get(
                url,
                headers={"User-Agent": self.user_agent, "Accept": "text/html"},
                timeout=self.timeout_s,
            )
        except requests.RequestException as e:
            print(f"WARNING: Target fetch failed for {cat}: {e}", file=sys.stderr)
            return []

        if resp.status_code != 200:
            print(
                f"WARNING: Target returned {resp.status_code} for {cat} — "
                "likely anti-bot. Falling back to SeedAdapter for this run.",
                file=sys.stderr,
            )
            return []

        products = _extract_products(resp.text)
        if not products:
            print(
                f"WARNING: Target HTML for {cat} didn't yield a product list — "
                "the layout may have changed. Falling back.",
                file=sys.stderr,
            )
            return []

        out: list[Candidate] = []
        for i, p in enumerate(products[:limit]):
            cand = make_candidate(
                raw_name=p.get("title", "").strip(),
                brand=p.get("brand", "").strip(),
                category=cat,
                source=self.name,
                retailer=self.name,
                source_url=p.get("url", "") or url,
                source_rank=i,
                upc=p.get("upc", "").strip(),
                image_url=p.get("image_url", "").strip(),
            )
            if not cand.raw_name:
                continue
            cand.confidence = 0.7   # less than seed; layout is fragile
            out.append(cand)
        return out


def _extract_products(html: str) -> list[dict]:
    """Pull product titles/brands out of Target's HTML. Tries the SSR JSON
    island first, then falls back to JSON-LD. Returns [] if neither yields
    something parseable — caller decides what to do."""
    products: list[dict] = []
    m = _JSON_ISLAND_RE.search(html)
    if m:
        try:
            data = json.loads(m.group(1))
            products = _walk_target_state(data)
        except json.JSONDecodeError:
            products = []
    if products:
        return products
    # Fallback: JSON-LD blocks list `Product` items with name + brand.
    for jm in _FALLBACK_JSON_LD_RE.finditer(html):
        try:
            blob = json.loads(jm.group(1).strip())
        except json.JSONDecodeError:
            continue
        if isinstance(blob, dict) and blob.get("@type") == "ItemList":
            for el in blob.get("itemListElement") or []:
                if not isinstance(el, dict):
                    continue
                item = el.get("item") or {}
                products.append({
                    "title": item.get("name", ""),
                    "brand": (item.get("brand") or {}).get("name", "")
                        if isinstance(item.get("brand"), dict)
                        else (item.get("brand") or ""),
                    "url": item.get("url", ""),
                    "image_url": item.get("image", "") if isinstance(item.get("image"), str) else "",
                })
    return products


def _walk_target_state(data: object) -> list[dict]:
    """Walk a Target SSR state blob looking for product-grid entries.
    Target's structure changes; we look for any object with a `tcin` field
    (Target's product ID) plus `title` / `brand` and gather them."""
    out: list[dict] = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            if "tcin" in node and ("title" in node or "name" in node):
                out.append({
                    "title": node.get("title") or node.get("name") or "",
                    "brand": _extract_brand_field(node),
                    "url": node.get("buy_url") or node.get("canonical_url") or "",
                    "upc": str(node.get("upc") or "").strip(),
                    "image_url": _extract_image(node),
                })
                return
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(data)
    # De-dupe by title (Target sometimes lists the same product multiple
    # times in different facets).
    seen = set()
    unique = []
    for p in out:
        key = p.get("title", "").lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def _extract_brand_field(node: dict) -> str:
    b = node.get("brand")
    if isinstance(b, str):
        return b
    if isinstance(b, dict):
        return b.get("name", "") or b.get("display_name", "")
    return ""


def _extract_image(node: dict) -> str:
    img = node.get("primary_image") or node.get("image") or ""
    if isinstance(img, dict):
        return img.get("base_url", "") or img.get("url", "")
    return img if isinstance(img, str) else ""
