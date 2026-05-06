"""Tests for the deterministic pieces of the blog generator.

Covers:
  - is/are grammar pick for evaluation titles
  - evaluation title + slug shape
  - comparison title + slug stay unchanged (backward compat)
  - input.csv → evaluation candidate normalization + dedup
  - Related Comparisons emitter only links to published slugs

Runs with stdlib unittest:
    ./venv/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

# Make repo root importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main
import discover_evaluations as de


class TestEvaluationGrammar(unittest.TestCase):
    def test_singular_mass_nouns(self):
        for food in ("Popcorn", "Oat Milk", "Greek Yogurt", "Kombucha", "Hummus"):
            self.assertFalse(
                main._is_plural_food_name(food),
                f"{food!r} should be treated as singular",
            )

    def test_plural_categories(self):
        for food in ("Protein Bars", "Kettle Chips", "Crackers", "Cookies",
                     "Frosted Flakes", "Organic Raisins"):
            self.assertTrue(
                main._is_plural_food_name(food),
                f"{food!r} should be treated as plural",
            )

    def test_plural_treated_brands(self):
        # Brand names that look plural and ARE plural-treated.
        for brand in ("Doritos", "Skittles", "Cheerios", "Cheez-Its",
                      "Pringles", "Pop-Tarts", "Pop Corners", "Triscuits"):
            self.assertTrue(
                main._is_plural_food_name(brand),
                f"plural-treated brand {brand!r} should be 'Are'",
            )

    def test_singular_brands_ending_in_s(self):
        # Brand names ending in -s that read as singular.
        for brand in ("Snickers", "Gogurt", "Fruifuls", "Reese's", "M&M's"):
            self.assertFalse(
                main._is_plural_food_name(brand),
                f"singular brand {brand!r} should stay 'Is'",
            )

    def test_head_noun_wins_over_modifier(self):
        # 'Cheerios Protein Bar' — Cheerios is a modifier, head is 'Bar'.
        self.assertFalse(
            main._is_plural_food_name("Cheerios Protein Bar"),
            "head noun 'Bar' is singular even though modifier is plural",
        )
        # 'Welch's Fruit Snacks' — head is 'Snacks'.
        self.assertTrue(
            main._is_plural_food_name("Welch's Fruit Snacks"),
            "head noun 'Snacks' is plural",
        )
        # 'Kashi … Bars' — head is 'Bars' (plural).
        self.assertTrue(
            main._is_plural_food_name(
                "Kashi Honey Oat Flax Crunchy 7 Grain With Quinoa Bars"
            )
        )
        # 'Quaker … Granola Bar S'mores' — head is 'Bar S'mores'? No, head
        # noun is 'Bar' modified by 'S'mores'. Last word here is "s'mores"
        # which has no -s plural marker → falls through to default Is.
        self.assertFalse(
            main._is_plural_food_name("Quaker Chewy Granola Bar S'mores")
        )

    def test_x_with_xs_pattern(self):
        # 'X with Xs' — trailing plural is a modifier echoing the head.
        # Head is the pre-connector singular word.
        self.assertFalse(
            main._is_plural_food_name("KIND Caramel Almond With Almonds"),
            "head 'Almond' is singular; 'Almonds' is the modifier",
        )
        # Same idea with '&' / 'and'.
        self.assertFalse(
            main._is_plural_food_name("Cashew & Cashews Mix"),
        )

    def test_with_clause_not_modifier_echo(self):
        # 'With Quinoa Bars' — 'Bars' is NOT the plural of 'Grain' (the
        # word before 'with'), so the connector-stripping heuristic
        # does NOT fire and the last word ('Bars') wins.
        self.assertTrue(
            main._is_plural_food_name(
                "Kashi Honey Oat Flax Crunchy 7 Grain With Quinoa Bars"
            )
        )

    def test_uncountable_substances(self):
        for food in ("Quaker Maple & Brown Sugar Instant Oatmeal",
                     "Special K Fruit & Yogurt", "Greek Yogurt", "Oat Milk"):
            self.assertFalse(
                main._is_plural_food_name(food),
                f"uncountable {food!r} should be 'Is'",
            )

    def test_empty_input(self):
        self.assertFalse(main._is_plural_food_name(""))
        self.assertFalse(main._is_plural_food_name("   "))


class TestEvaluationTitle(unittest.TestCase):
    def test_singular(self):
        self.assertEqual(main._evaluation_title("Popcorn"), "Is Popcorn Healthy?")
        self.assertEqual(main._evaluation_title("oat milk"), "Is Oat Milk Healthy?")
        self.assertEqual(main._evaluation_title("Chobani Greek Yogurt"),
                         "Is Chobani Greek Yogurt Healthy?")

    def test_plural(self):
        self.assertEqual(main._evaluation_title("Protein Bars"),
                         "Are Protein Bars Healthy?")
        self.assertEqual(main._evaluation_title("kettle chips"),
                         "Are Kettle Chips Healthy?")

    def test_hyphenated_capitalization(self):
        # Smart title-case capitalizes after hyphens (Coca-Cola, not Coca-cola).
        self.assertEqual(main._evaluation_title("coca-cola"), "Is Coca-Cola Healthy?")

    def test_empty_falls_back(self):
        # Doesn't crash; gives a usable string.
        self.assertEqual(main._evaluation_title(""), "Is This Food Healthy?")
        self.assertEqual(main._evaluation_title(None), "Is This Food Healthy?")  # type: ignore[arg-type]


class TestEvaluationSlug(unittest.TestCase):
    def test_singular(self):
        self.assertEqual(main._evaluation_slug("Popcorn"), "is-popcorn-healthy")
        self.assertEqual(main._evaluation_slug("Coca-Cola"), "is-coca-cola-healthy")
        self.assertEqual(main._evaluation_slug("Chobani Greek Yogurt"),
                         "is-chobani-greek-yogurt-healthy")

    def test_plural(self):
        self.assertEqual(main._evaluation_slug("Protein Bars"),
                         "are-protein-bars-healthy")
        self.assertEqual(main._evaluation_slug("Kettle Chips"),
                         "are-kettle-chips-healthy")

    def test_no_collision_with_comparison_slugs(self):
        eval_slug = main._evaluation_slug("Gatorade")
        cmp_slug = main._seo_slug("Gatorade", "Coca-Cola")
        self.assertNotEqual(eval_slug, cmp_slug)
        self.assertEqual(eval_slug, "is-gatorade-healthy")
        self.assertEqual(cmp_slug, "is-gatorade-healthy-vs-coca-cola")


class TestComparisonHelpersUnchanged(unittest.TestCase):
    """Backward-compat sanity: changes to evaluation must NOT shift the
    existing comparison title / slug shapes."""

    def test_comparison_title(self):
        self.assertEqual(
            main._seo_title("Gatorade", "Coca-Cola"),
            "Is Gatorade Healthy? (Gatorade vs Coca-Cola)",
        )

    def test_exposure_title_no_food_b(self):
        self.assertEqual(main._seo_title("Nutella", ""), "Is Nutella Healthy?")

    def test_comparison_slug(self):
        self.assertEqual(
            main._seo_slug("Gatorade", "Coca-Cola"),
            "is-gatorade-healthy-vs-coca-cola",
        )


class TestNormalizeFoodName(unittest.TestCase):
    def test_strips_size_noise(self):
        self.assertEqual(
            de.normalize_food_name("Frosted Flakes Family Size 12 oz"),
            "frosted flakes",
        )
        self.assertEqual(
            de.normalize_food_name("Coca-Cola Classic 1L"),
            "coca-cola",
        )

    def test_dedup_collapses_variants(self):
        # All four collapse to the same key.
        keys = {
            de.normalize_food_name("Coca-Cola Classic"),
            de.normalize_food_name("Coca-Cola Original"),
            de.normalize_food_name("Coca-Cola"),
            de.normalize_food_name("Coca-Cola Classic 12 oz"),
        }
        self.assertEqual(len(keys), 1, f"expected single key, got {keys}")

    def test_empty_returns_empty(self):
        self.assertEqual(de.normalize_food_name(""), "")
        self.assertEqual(de.normalize_food_name("   "), "")


class TestVagueNameFilter(unittest.TestCase):
    def test_single_word_categories_are_vague(self):
        for n in ("smoothie", "yogurt", "snack", "drink", "juice"):
            self.assertTrue(de.is_vague_name(n), f"{n!r} should be vague")

    def test_multi_word_or_branded_not_vague(self):
        for n in ("greek yogurt", "chobani yogurt", "fruit smoothie",
                  "naked juice green machine"):
            self.assertFalse(de.is_vague_name(n), f"{n!r} should NOT be vague")


class TestCollectUniqueFoods(unittest.TestCase):
    def _write_csv(self, rows: list[dict]) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, newline="",
        )
        cols = [
            "pair_id",
            "food_a_name", "food_a_display_name", "food_a_label", "food_a_barcode",
            "food_b_name", "food_b_display_name", "food_b_label", "food_b_barcode",
            "purpose", "framing_type", "format",
        ]
        writer = csv.DictWriter(tmp, fieldnames=cols)
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in cols})
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return Path(tmp.name)

    def test_extracts_unique_foods_from_both_sides(self):
        path = self._write_csv([
            {"pair_id": "1", "food_a_display_name": "Snickers",
             "food_b_display_name": "Coca-Cola", "format": "comparison"},
            {"pair_id": "2", "food_a_display_name": "Frosted Flakes",
             "food_b_display_name": "Snickers", "format": "comparison"},
        ])
        out = de.collect_unique_foods(str(path))
        norms = {e["norm"] for e in out}
        # Three unique foods, with Snickers showing twice.
        self.assertEqual(norms, {"snickers", "coca-cola", "frosted flakes"})
        snickers = next(e for e in out if e["norm"] == "snickers")
        self.assertEqual(snickers["count"], 2)

    def test_empty_or_missing_csv(self):
        self.assertEqual(de.collect_unique_foods("/nonexistent.csv"), [])

    def test_skips_existing_evaluation_rows_for_dedup(self):
        path = self._write_csv([
            {"pair_id": "1", "food_a_display_name": "Snickers",
             "food_b_display_name": "Coca-Cola", "format": "comparison"},
            {"pair_id": "2", "food_a_display_name": "Snickers",
             "format": "evaluation"},  # already done
        ])
        already = de.existing_evaluation_norms(str(path))
        self.assertIn("snickers", already)


class TestRelatedSlugSafety(unittest.TestCase):
    """The Related Comparisons assembly must only emit slugs that exist
    in the published index."""

    def test_picker_excludes_self(self):
        published = ["is-popcorn-healthy", "is-gatorade-healthy",
                     "is-nutella-healthy", "are-protein-bars-healthy"]
        # current = published[0] should not appear in result
        out = main._pick_related_slugs(
            published, current_slug="is-popcorn-healthy", rotation_idx=0, n=3,
        )
        self.assertNotIn("is-popcorn-healthy", out)
        self.assertEqual(len(out), 3)

    def test_picker_returns_empty_when_too_few(self):
        # 2 total, 1 self → 1 candidate → empty per spec
        self.assertEqual(
            main._pick_related_slugs(
                ["a", "b"], current_slug="a", rotation_idx=0, n=3,
            ),
            [],
        )

    def test_picker_only_returns_published_slugs(self):
        published = ["a", "b", "c", "d"]
        out = main._pick_related_slugs(published, "x", 0, n=3)
        for slug in out:
            self.assertIn(slug, published)


if __name__ == "__main__":
    unittest.main()
