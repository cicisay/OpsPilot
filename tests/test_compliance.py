import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.compliance import ComplianceChecker, ensure_lexicon  # noqa: E402


class ComplianceTests(unittest.TestCase):
    def setUp(self):
        self.checker = ComplianceChecker()

    def test_clean_copy_passes(self):
        report = self.checker.check(
            "Premium scented candle, 45-hour burn",
            "scene photo, no visible logos",
            "home-fragrance",
            "US",
        )
        self.assertTrue(report.passed)
        self.assertEqual(report.violations, [])

    def test_absolute_terms_are_blocked(self):
        report = self.checker.check(
            "100% guaranteed best seller",
            "scene photo",
            "home-fragrance",
            "US",
        )
        self.assertFalse(report.passed)
        rule_ids = {item.rule_id for item in report.violations}
        self.assertIn("ABS-001", rule_ids)
        self.assertIn("ABS-002", rule_ids)

    def test_brand_terms_are_blocked(self):
        report = self.checker.check(
            "Nike style candle",
            "scene photo",
            "home-fragrance",
            "US",
        )
        self.assertFalse(report.passed)
        self.assertEqual(report.violations[0].rule_id, "BRD-001")

    def test_copyright_flags_are_blocked(self):
        report = self.checker.check(
            "Premium candle",
            "image with brand logo close-up",
            "home-fragrance",
            "US",
        )
        self.assertFalse(report.passed)
        self.assertEqual(report.violations[0].rule_id, "CPY-001")

    def test_fullwidth_text_is_blocked(self):
        report = self.checker.check(
            "１００％ ＧＵＡＲＡＮＴＥＥＤ",
            "scene photo",
            "home-fragrance",
            "US",
        )
        self.assertFalse(report.passed)
        rule_ids = {item.rule_id for item in report.violations}
        self.assertIn("ABS-001", rule_ids)

    def test_spaced_percent_is_blocked(self):
        report = self.checker.check(
            "100 % guaranteed",
            "scene photo",
            "home-fragrance",
            "US",
        )
        self.assertFalse(report.passed)
        self.assertIn("ABS-001", {item.rule_id for item in report.violations})

    def test_zero_width_characters_do_not_bypass(self):
        report = self.checker.check(
            "best\u200bseller",
            "scene photo",
            "home-fragrance",
            "US",
        )
        self.assertFalse(report.passed)
        self.assertIn("ABS-003", {item.rule_id for item in report.violations})

    def test_zero_width_inside_brand_is_blocked(self):
        report = self.checker.check(
            "ni\u200bke style mug",
            "scene photo",
            "home-fragrance",
            "US",
        )
        self.assertFalse(report.passed)
        self.assertIn("BRD-001", {item.rule_id for item in report.violations})

    def test_market_specific_terms(self):
        de = self.checker.check("klimaneutral candle", "scene", "home-fragrance", "DE")
        self.assertFalse(de.passed)
        self.assertIn("DE-001", {item.rule_id for item in de.violations})

        us = self.checker.check("klimaneutral candle", "scene", "home-fragrance", "US")
        self.assertTrue(us.passed)

        uk = self.checker.check("risk-free candle", "scene", "home-fragrance", "UK")
        self.assertFalse(uk.passed)
        self.assertIn("UK-001", {item.rule_id for item in uk.violations})


class ObfuscationTests(unittest.TestCase):
    """A denied term must survive the disguises a seller can apply to it."""

    def setUp(self):
        self.checker = ComplianceChecker()

    def assert_blocks(self, text, rule_id, brief="scene photo", market="US"):
        report = self.checker.check(text, brief, "home-fragrance", market)
        self.assertFalse(report.passed, f"{text!r} passed but should hit {rule_id}")
        self.assertIn(rule_id, {item.rule_id for item in report.violations})

    def test_soft_hyphen_does_not_split_a_brand(self):
        self.assert_blocks("ni\u00adke style mug", "BRD-001")

    def test_word_joiner_does_not_split_a_brand(self):
        self.assert_blocks("ni\u2060ke style mug", "BRD-001")

    def test_letter_spacing_does_not_split_a_brand(self):
        self.assert_blocks("n i k e style mug", "BRD-001")
        self.assert_blocks("N I K E", "BRD-001")

    def test_cyrillic_lookalike_is_folded(self):
        self.assert_blocks("n\u0456ke style mug", "BRD-001")

    def test_absolutes_in_any_spelling(self):
        for text in ("bestseller", "best-seller", "bestselling", "no1", "no 1", "no.1"):
            with self.subTest(text=text):
                self.assert_blocks(text, "ABS-003" if "best" in text else "ABS-004")

    def test_percent_words_are_the_same_claim(self):
        for text in ("100 percent cotton", "100 pct cotton", "100% cotton"):
            with self.subTest(text=text):
                self.assert_blocks(text, "ABS-001")

    def test_market_term_without_the_hyphen(self):
        self.assert_blocks("risk free candle", "UK-001", market="UK")

    def test_design_brief_is_checked_like_copy(self):
        """The brief carries the brand logo claim; it used to be invisible."""
        self.assert_blocks("premium candle", "BRD-001", brief="主图：nike logo 特写")

    def test_plain_counters_are_not_mistaken_for_claims(self):
        report = self.checker.check(
            "100 count cotton towels, piano 1 piece", "plain background", "textiles", "US"
        )
        self.assertTrue(report.passed, [item.rule_id for item in report.violations])

    def test_violation_quotes_what_matched(self):
        report = self.checker.check("nike shoes", "scene", "apparel", "US")
        self.assertEqual(report.violations[0].quote, "nike")


class LexiconMergeTests(unittest.TestCase):
    """The file on disk is what operators edit, so it must never under-enforce."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "compliance" / "lexicon.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, payload) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.path.write_text(text, encoding="utf-8")

    def test_legacy_file_keeps_market_rules(self):
        # Exactly the shape shipped before market rules existed.
        self.write(
            {
                "version": "2026-10-30",
                "absolute_terms": [
                    {
                        "term": "100%",
                        "rule_id": "ABS-001",
                        "message": "绝对化用语：100%",
                        "suggestion": "改条件",
                    }
                ],
                "brand_terms": ["nike"],
                "copyright_flags": ["getty"],
                "prohibited_categories": [],
            }
        )
        checker = ComplianceChecker(self.path)
        self.assertEqual(checker.lexicon_filled, ["market_terms"])

        de = checker.check("klimaneutral candle", "scene", "home-fragrance", "DE")
        self.assertFalse(de.passed)
        self.assertIn("DE-001", {item.rule_id for item in de.violations})
        uk = checker.check("risk-free candle", "scene", "home-fragrance", "UK")
        self.assertIn("UK-001", {item.rule_id for item in uk.violations})

    def test_explicit_empty_section_stays_empty(self):
        # An operator who empties a section means it, so it is not backfilled.
        self.write({"version": "2026-11-01", "market_terms": {}})
        checker = ComplianceChecker(self.path)
        self.assertNotIn("market_terms", checker.lexicon_filled)
        report = checker.check("klimaneutral candle", "scene", "home-fragrance", "DE")
        self.assertTrue(report.passed)
        self.assertEqual(report.lexicon_version, "2026-11-01")

    def test_operator_edits_win_over_baseline(self):
        self.write(
            {
                "version": "2026-11-02",
                "absolute_terms": [
                    {
                        "term": "handmade",
                        "rule_id": "OPS-001",
                        "message": "运营新增：手工艺品需可溯源",
                        "suggestion": "补充产地与工序说明",
                    }
                ],
            }
        )
        checker = ComplianceChecker(self.path)
        hit = checker.check("handmade ceramic mug", "scene", "home-kitchen", "US")
        self.assertIn("OPS-001", {item.rule_id for item in hit.violations})
        # The baseline term was replaced, not merged item by item.
        self.assertTrue(checker.check("100% cotton", "scene", "textiles", "US").passed)

    def test_corrupt_file_falls_back_to_baseline(self):
        self.write("{ this is not json")
        checker = ComplianceChecker(self.path)
        report = checker.check("klimaneutral candle", "scene", "home-fragrance", "DE")
        self.assertIn("DE-001", {item.rule_id for item in report.violations})

    def test_ensure_lexicon_backfills_the_file(self):
        self.write({"version": "2026-10-30", "brand_terms": ["nike"]})
        filled = ensure_lexicon(self.path)
        self.assertIn("market_terms", filled)

        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("market_terms", saved)
        self.assertIn("nike", saved["brand_terms"])
        self.assertEqual(ensure_lexicon(self.path), [])


if __name__ == "__main__":
    unittest.main()
