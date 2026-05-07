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


class TestTopicsField(unittest.TestCase):
    """The `topics:` frontmatter field must only ever reference slugs that
    exist in WEBSITE_TOPICS_DIR — referencing an unknown slug fails the
    website build."""

    BASE = (
        '---\n'
        'title: "Are Protein Bars Healthy?"\n'
        'description: "..."\n'
        'date: "2026-05-06"\n'
        'topics: {topics}\n'
        '---\n\n'
        '# Are Protein Bars Healthy?\n\nbody.'
    )

    def test_keeps_valid_topics(self):
        md = self.BASE.format(topics='["protein-bars", "high-protein-snacks"]')
        out, dropped = main._validate_topics_field(
            md, ["protein-bars", "high-protein-snacks", "weight-loss"],
        )
        self.assertIn('topics: ["protein-bars", "high-protein-snacks"]', out)
        self.assertEqual(dropped, [])

    def test_strips_unknown_topics(self):
        md = self.BASE.format(topics='["protein-bars", "fake-hub", "high-protein-snacks"]')
        out, dropped = main._validate_topics_field(
            md, ["protein-bars", "high-protein-snacks"],
        )
        self.assertIn('topics: ["protein-bars", "high-protein-snacks"]', out)
        self.assertEqual(dropped, ["fake-hub"])

    def test_drops_line_when_all_invalid(self):
        md = self.BASE.format(topics='["fake-1", "fake-2"]')
        out, dropped = main._validate_topics_field(md, ["protein-bars"])
        self.assertNotIn("topics:", out)
        self.assertEqual(set(dropped), {"fake-1", "fake-2"})

    def test_strips_when_no_allowlist(self):
        # Empty allowlist = no topics dir configured. Drop any topics line.
        md = self.BASE.format(topics='["something"]')
        out, dropped = main._validate_topics_field(md, [])
        self.assertNotIn("topics:", out)
        self.assertEqual(dropped, ["something"])

    def test_no_topics_line_no_op(self):
        md = (
            '---\n'
            'title: "Are Protein Bars Healthy?"\n'
            'description: "..."\n'
            'date: "2026-05-06"\n'
            '---\n\nbody.'
        )
        out, dropped = main._validate_topics_field(md, ["protein-bars"])
        self.assertEqual(out, md)
        self.assertEqual(dropped, [])


class TestPublishedTopicsIndex(unittest.TestCase):
    def test_unset_returns_empty(self):
        self.assertEqual(main.load_published_topics_index(None), [])
        self.assertEqual(main.load_published_topics_index(""), [])

    def test_invalid_path_warns_and_returns_empty(self):
        # Doesn't raise; prints a warning to stderr.
        self.assertEqual(
            main.load_published_topics_index("/no-such-path-xyz"), []
        )

    def test_lists_mdx_and_md_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("protein-bars.mdx", "weight-loss.md", "ignored.txt"):
                Path(tmp, name).write_text("---\ntitle: t\n---\n")
            slugs = main.load_published_topics_index(tmp)
            self.assertEqual(slugs, ["protein-bars", "weight-loss"])


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


class TestAuditorLanguageGuardrails(unittest.TestCase):
    """The auditor must catch disease claims as errors and fear/absolute
    language as warnings — per the SmarterEats editorial spec."""

    def setUp(self):
        from audit_blog_posts import (
            DISEASE_CLAIM_PATTERNS,
            FEAR_RHETORIC_PATTERNS,
            ABSOLUTE_LANGUAGE_PATTERNS,
            _scan_patterns,
        )
        self.scan = _scan_patterns
        self.disease = DISEASE_CLAIM_PATTERNS
        self.fear = FEAR_RHETORIC_PATTERNS
        self.absolutes = ABSOLUTE_LANGUAGE_PATTERNS

    # ---- disease / treatment claims (must fire → ERROR) ----
    def test_disease_treatment_verbs_flagged(self):
        for phrase in [
            "prevents heart disease",
            "treats diabetes",
            "cures cancer",
            "reverses insulin resistance",
            "fights inflammation",
            "lowers cholesterol",
            "boosts immunity",
            "boosts your immunity",
            "detoxifies",
            "detoxifying",
            "cleanses your gut",
            "heals your gut",
            "repairs metabolism",
        ]:
            self.assertTrue(
                self.scan(phrase, self.disease),
                f"expected disease claim to flag {phrase!r}",
            )

    def test_risk_reduction_claims_flagged(self):
        for phrase in [
            "lowers risk of cancer",
            "reduces the risk of heart disease",
            "cuts your risk of stroke",
        ]:
            self.assertTrue(
                self.scan(phrase, self.disease),
                f"expected risk-reduction claim to flag {phrase!r}",
            )

    def test_descriptive_nutrient_language_does_not_flag(self):
        # Per spec: descriptive language is fine; only treatment claims fire.
        ok_phrases = [
            "high in vitamin C and zinc, both associated with normal immune function",
            "contains 12g of added sugar per serving",
            "may cause sharper post-meal energy swings due to refined carbs",
            "high in fiber for satiety",
        ]
        for phrase in ok_phrases:
            self.assertFalse(
                self.scan(phrase, self.disease),
                f"disease patterns should NOT fire on {phrase!r}",
            )

    # ---- fear rhetoric (must fire → WARNING) ----
    def test_fear_rhetoric_flagged(self):
        for phrase in [
            "this is toxic",
            "fake food",
            "garbage food",
            "dangerous chemicals in this snack",
            "endocrine-disrupting",
            "endocrine disrupting",
            "addictive poison",
            "destroys your metabolism",
            "wrecks your gut",
            "ruins your hormones",
        ]:
            self.assertTrue(
                self.scan(phrase, self.fear),
                f"expected fear rhetoric to flag {phrase!r}",
            )

    # ---- bare absolutes (must fire → WARNING) ----
    def test_bare_absolutes_flagged(self):
        for phrase in [
            "this is healthy",
            "this is unhealthy",
            "the healthiest choice",
            "the healthiest option",
            "everyone should avoid this",
            "never eat seed oils",
            "always eat protein first",
        ]:
            self.assertTrue(
                self.scan(phrase, self.absolutes),
                f"expected absolute to flag {phrase!r}",
            )

    def test_goal_context_framing_does_not_flag_absolutes(self):
        # Goal-context phrasing is the preferred replacement — must not fire.
        ok_phrases = [
            "may fit goals focused on satiety",
            "the better-aligned option for weight loss",
            "less ideal for energy stability",
            "this is a healthy snack for most readers",  # "a healthy" not "is healthy"
            "depending on your goals, may be reasonable",
        ]
        for phrase in ok_phrases:
            self.assertFalse(
                self.scan(phrase, self.absolutes),
                f"absolute patterns should NOT fire on {phrase!r}",
            )


if __name__ == "__main__":
    unittest.main()
