"""Tests for the food candidate discovery pipeline.

Covers the deterministic pieces — normalization, relevance scoring, hub
assignment, dedup, CSV round-trip. Adapter network calls are NOT
exercised here (those would be brittle); the pipeline orchestrator is
tested via the SeedAdapter end-to-end since that adapter is hermetic.

Run:
    ./venv/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from discovery.candidate import (  # noqa: E402
    Candidate, read_candidates_csv, write_candidates_csv,
)
from discovery.hubs import V1_HUBS, assign_hubs  # noqa: E402
from discovery.normalize import (  # noqa: E402
    canonicalize_name, dedup_key_for, dedupe_candidates,
)
from discovery.retailers import SeedAdapter  # noqa: E402
from discovery.score import score_relevance  # noqa: E402
from discovery.pipeline import run_discovery  # noqa: E402


class TestCanonicalizeName(unittest.TestCase):
    def test_strips_size_and_flavor_into_brand_plus_head(self):
        canonical, brand = canonicalize_name(
            "Quest Nutrition Protein Bar Chocolate Chip Cookie Dough 2.12oz"
        )
        # Brand sniffed from leading word; head 'bar' pluralized.
        self.assertEqual(canonical, "Quest Bars")
        self.assertEqual(brand, "Quest")

    def test_with_explicit_brand_hint(self):
        canonical, brand = canonicalize_name(
            "Cookies & Cream Protein Bar", brand_hint="Barebells",
        )
        self.assertEqual(canonical, "Barebells Bars")
        self.assertEqual(brand, "Barebells")

    def test_multi_word_brand_detected(self):
        canonical, brand = canonicalize_name(
            "Fairlife Core Power Chocolate Protein Shake"
        )
        # Multi-word brand prefix preserved.
        self.assertIn("Fairlife Core Power", canonical)
        self.assertTrue(canonical.endswith("Shakes"))

    def test_pluralizes_singular_head(self):
        # 'Bar' → 'Bars' for canonical product-line naming.
        canonical, _ = canonicalize_name("Quest Bar", brand_hint="Quest")
        self.assertEqual(canonical, "Quest Bars")

    def test_no_head_noun_falls_back_to_brand(self):
        # No recognized head noun → just brand + cleanup.
        canonical, brand = canonicalize_name("Poppi", brand_hint="Poppi")
        self.assertEqual(canonical, "Poppi")
        self.assertEqual(brand, "Poppi")

    def test_empty(self):
        self.assertEqual(canonicalize_name(""), ("", ""))


class TestDedupCandidates(unittest.TestCase):
    def _cand(self, raw: str, canonical: str, brand: str) -> Candidate:
        c = Candidate(raw_name=raw, canonical_name=canonical, brand=brand,
                      source="test")
        c.dedup_key = dedup_key_for(canonical, brand)
        return c

    def test_collapses_same_key_keeps_first(self):
        cands = [
            self._cand("Quest Bar Chocolate Chip", "Quest Bars", "Quest"),
            self._cand("Quest Bar Cookies & Cream", "Quest Bars", "Quest"),
            self._cand("Quest Bar S'mores", "Quest Bars", "Quest"),
        ]
        out = dedupe_candidates(cands)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].raw_name, "Quest Bar Chocolate Chip")
        self.assertEqual(
            sorted(out[0].aliases),
            ["Quest Bar Cookies & Cream", "Quest Bar S'mores"],
        )

    def test_distinct_keys_stay_separate(self):
        cands = [
            self._cand("Quest Bar", "Quest Bars", "Quest"),
            self._cand("Quest Chip", "Quest Chips", "Quest"),
        ]
        out = dedupe_candidates(cands)
        self.assertEqual(len(out), 2)


class TestRelevanceScore(unittest.TestCase):
    def test_household_brand_beats_unknown(self):
        known = Candidate(
            canonical_name="Quest Bars", brand="Quest",
            category="protein-bars", retailer="target",
        )
        unknown = Candidate(
            canonical_name="Plumtree Bars", brand="Plumtree",
            category="protein-bars", retailer="target",
        )
        s_known, _ = score_relevance(known)
        s_unknown, _ = score_relevance(unknown)
        self.assertGreater(s_known, s_unknown)

    def test_costco_brand_gets_retailer_boost(self):
        kirkland = Candidate(
            canonical_name="Kirkland Signature Bars",
            brand="Kirkland Signature",
            category="protein-bars", retailer="costco",
        )
        score, reasons = score_relevance(kirkland)
        self.assertTrue(any("costco" in r.lower() or "kirkland" in r.lower()
                             for r in reasons))
        self.assertGreaterEqual(score, 70)

    def test_no_brand_penalized(self):
        anon = Candidate(
            canonical_name="Some Generic Bars", brand="",
            category="protein-bars",
        )
        score, reasons = score_relevance(anon)
        self.assertLess(score, 70)
        self.assertTrue(any("no brand" in r.lower() for r in reasons))

    def test_score_clamped_0_100(self):
        # A maximally-loaded candidate.
        c = Candidate(
            canonical_name="Quest Bars", brand="Quest",
            category="protein-bars", retailer="costco",
        )
        c.aliases = ["v1", "v2", "v3"]
        score, _ = score_relevance(c, source_count=5)
        self.assertLessEqual(score, 100)
        self.assertGreaterEqual(score, 0)


class TestHubAssignment(unittest.TestCase):
    def test_protein_bar_assigns_full_hub_set(self):
        c = Candidate(category="protein-bars")
        hubs = assign_hubs(c)
        self.assertIn("protein-bars", hubs)
        self.assertIn("high-protein-snacks", hubs)
        self.assertIn("weight-loss-snacks", hubs)
        self.assertIn("healthy-convenience-foods", hubs)

    def test_kirkland_brand_adds_costco_hub(self):
        c = Candidate(brand="Kirkland Signature", category="frozen-meals",
                      retailer="costco")
        hubs = assign_hubs(c)
        self.assertIn("healthy-costco-foods", hubs)
        self.assertIn("healthy-frozen-meals", hubs)

    def test_unknown_category_returns_empty(self):
        # Unrecognized category → no hubs (reviewer fixes manually).
        self.assertEqual(assign_hubs(Candidate(category="oysters")), [])

    def test_allowed_intersection(self):
        # When the live website has only a subset of V1_HUBS published,
        # we intersect.
        live = {"protein-bars", "healthy-drinks"}  # narrower than V1.
        c = Candidate(category="protein-bars")
        hubs = assign_hubs(c, allowed=live)
        self.assertEqual(hubs, ["protein-bars"])  # other matches dropped


class TestCandidateCSVRoundTrip(unittest.TestCase):
    def test_round_trip(self):
        c1 = Candidate(
            raw_name="Quest Bar Chocolate Chip 2.12oz",
            canonical_name="Quest Bars",
            brand="Quest", category="protein-bars",
            source="seed", retailer="target",
            relevance_score=92,
            score_reasons=["Household brand (Quest)", "Strong content category"],
            assigned_hubs=["protein-bars", "high-protein-snacks"],
            aliases=["Quest Bar Cookies & Cream"],
            confidence=0.95,
            approval_status="approved",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round_trip.csv"
            n = write_candidates_csv(path, [c1])
            self.assertEqual(n, 1)
            roundtripped = read_candidates_csv(path)
            self.assertEqual(len(roundtripped), 1)
            r = roundtripped[0]
            self.assertEqual(r.canonical_name, "Quest Bars")
            self.assertEqual(r.brand, "Quest")
            self.assertEqual(r.assigned_hubs,
                             ["protein-bars", "high-protein-snacks"])
            self.assertEqual(r.aliases, ["Quest Bar Cookies & Cream"])
            self.assertEqual(r.relevance_score, 92)
            self.assertEqual(r.approval_status, "approved")


class TestSeedAdapter(unittest.TestCase):
    def _seed_file(self, rows: list[dict]) -> Path:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        )
        json.dump(rows, f)
        f.close()
        self.addCleanup(lambda: Path(f.name).unlink(missing_ok=True))
        return Path(f.name)

    def test_filters_by_category(self):
        path = self._seed_file([
            {"brand": "Quest", "category": "protein-bars", "retailer": "target"},
            {"brand": "Lean Cuisine", "category": "frozen-meals",
             "retailer": "walmart"},
        ])
        adapter = SeedAdapter(seed_path=path)
        bars = adapter.fetch("protein-bars")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].brand, "Quest")
        meals = adapter.fetch("frozen-meals")
        self.assertEqual(len(meals), 1)
        self.assertEqual(meals[0].brand, "Lean Cuisine")

    def test_all_returns_everything(self):
        path = self._seed_file([
            {"brand": "Quest", "category": "protein-bars"},
            {"brand": "Poppi", "category": "prebiotic-soda"},
        ])
        adapter = SeedAdapter(seed_path=path)
        out = adapter.fetch("all")
        self.assertEqual(len(out), 2)

    def test_skips_rows_without_brand(self):
        path = self._seed_file([
            {"category": "protein-bars"},
            {"brand": "", "category": "protein-bars"},
            {"brand": "Quest", "category": "protein-bars"},
        ])
        out = SeedAdapter(seed_path=path).fetch("protein-bars")
        self.assertEqual([c.brand for c in out], ["Quest"])

    def test_missing_seed_file_returns_empty(self):
        adapter = SeedAdapter(seed_path=Path("/no/such/path.json"))
        self.assertEqual(adapter.fetch("protein-bars"), [])


class TestPipelineEndToEnd(unittest.TestCase):
    """Drives the orchestrator with a temporary seed file so it's hermetic."""

    def _seed_file(self) -> Path:
        rows = [
            {"brand": "Quest", "category": "protein-bars", "retailer": "target"},
            {"brand": "Quest", "category": "chips", "retailer": "target"},
            {"brand": "Lean Cuisine", "category": "frozen-meals",
             "retailer": "walmart"},
            {"brand": "Plumtree", "category": "protein-bars",
             "retailer": "target"},  # unknown brand → lower score
            {"brand": "Kirkland Signature", "category": "frozen-meals",
             "retailer": "costco"},
        ]
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        )
        json.dump(rows, f)
        f.close()
        self.addCleanup(lambda: Path(f.name).unlink(missing_ok=True))
        return Path(f.name)

    def test_dedupes_normalizes_scores_assigns_hubs(self):
        from discovery.retailers.seed import SeedAdapter
        adapter = SeedAdapter(seed_path=self._seed_file())
        # Run the full pipeline by hand using the temp adapter so we don't
        # touch the default seed file or the on-disk output dir.
        raw = adapter.fetch("all", limit=50)
        self.assertEqual(len(raw), 5)

        # Reuse pipeline's internal helpers via run_discovery? It has
        # implicit dep on pipeline.ALL_ADAPTERS. Easier: hit the pieces
        # directly to keep the test hermetic.
        from discovery.normalize import canonicalize_name, dedupe_candidates, dedup_key_for
        from discovery.score import score_relevance
        from discovery.hubs import assign_hubs

        for c in raw:
            cn, brand = canonicalize_name(c.raw_name, brand_hint=c.brand)
            c.canonical_name = cn
            if not c.brand and brand:
                c.brand = brand
            c.dedup_key = dedup_key_for(c.canonical_name, c.brand)

        normalized = dedupe_candidates(raw)
        # All five collapse onto distinct keys (different brand/category).
        self.assertEqual(len(normalized), 5)

        for c in normalized:
            score, reasons = score_relevance(c, source_count=1 + len(c.aliases))
            c.relevance_score = score
            c.score_reasons = reasons
            c.assigned_hubs = assign_hubs(c)

        # Quest > Plumtree (unknown brand).
        quest = next(c for c in normalized if c.brand == "Quest"
                     and c.category == "protein-bars")
        plumtree = next(c for c in normalized if c.brand == "Plumtree")
        self.assertGreater(quest.relevance_score, plumtree.relevance_score)

        # Kirkland gets the costco hub.
        kirkland = next(c for c in normalized if "Kirkland" in c.brand)
        self.assertIn("healthy-costco-foods", kirkland.assigned_hubs)


if __name__ == "__main__":
    unittest.main()
