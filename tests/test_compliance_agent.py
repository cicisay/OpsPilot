import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.agents.compliance_agent import (  # noqa: E402
    ComplianceError,
    apply_hard_ban,
    finalize_verdicts,
    index_verdicts,
    lexicon_suspects,
    run_compliance,
)
from ops_agent.compliance import DEFAULT_LEXICON, ComplianceChecker  # noqa: E402
from ops_agent.config import LLMSettings  # noqa: E402
from ops_agent.llm import LLMError, LLMReply  # noqa: E402
from ops_agent.site_platform import SiteMetrics, SiteProduct, SiteVariant  # noqa: E402

SETTINGS = LLMSettings(
    provider="deepseek", base_url="http://model.local/v1", model="test-model", api_key="k"
)


class FakeClient:
    def __init__(self, replies) -> None:
        self.settings = SETTINGS
        self._replies = list(replies)
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return True

    def chat(self, messages, temperature=None, max_tokens=None):
        self.calls += 1
        if not self._replies:
            raise LLMError("no replies left")
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMReply(text=json.dumps(item, ensure_ascii=False), model="test-model")


def make_product(category: str = "home-kitchen") -> SiteProduct:
    metrics = SiteMetrics(30, 900, 600, 9, 0, 0, 0, 6, 3.6, 2)
    variant = SiteVariant("v1", "P1-01", 620, 400, 800, {})
    return SiteProduct(
        product_id="p1",
        slug="p1",
        title_en="Ceramic Mug Set",
        status="active",
        category_id="c1",
        category_slug=category,
        moq=100,
        lead_time_days=18,
        incoterm="FOB",
        variants=(variant,),
        metrics=metrics,
    )


def make_proposal(count: int = 2) -> dict:
    variants = []
    for index in range(count):
        variants.append(
            {
                "variant_key": f"p1-v{index + 1}",
                "angle": f"角度{index + 1}",
                "title": f"Ceramic Mug Set - Angle {index + 1}",
                "bullets": ["MOQ 100 pcs", "Batch QC report included"],
                "description": "Stoneware mugs for cafes and gift shops.",
                "material_brief": ["主体：四只装", "禁用：品牌 logo"],
                "price_cents": 620,
            }
        )
    return {"product_id": "p1", "variants": variants}


def verdict(key: str, **overrides) -> dict:
    item = {
        "variant_key": key,
        "c1_absolute": "pass",
        "c2_ip": "pass",
        "c3_category": "pass",
        "c4_market": "pass",
        "findings": [],
    }
    item.update(overrides)
    return item


def reply(*items) -> dict:
    return {"verdicts": list(items)}


class LexiconTests(unittest.TestCase):
    def setUp(self):
        self.checker = ComplianceChecker()

    def test_clean_copy_has_no_suspects(self):
        suspects = lexicon_suspects(
            self.checker, make_proposal()["variants"], category="home-kitchen", market="US"
        )
        self.assertEqual(suspects, {})

    def test_absolute_claim_is_returned_as_evidence(self):
        proposal = make_proposal(1)
        proposal["variants"][0]["title"] = "100% Guaranteed Mug"
        suspects = lexicon_suspects(
            self.checker, proposal["variants"], category="home-kitchen", market="US"
        )
        rules = {hit["rule_id"] for hit in suspects["p1-v1"]}
        self.assertIn("ABS-001", rules)
        self.assertIn("ABS-002", rules)

    def test_brand_is_returned_as_a_hard_hit(self):
        proposal = make_proposal(1)
        proposal["variants"][0]["title"] = "Nike Style Mug"
        suspects = lexicon_suspects(
            self.checker, proposal["variants"], category="home-kitchen", market="US"
        )
        self.assertIn("BRD-001", {hit["rule_id"] for hit in suspects["p1-v1"]})


class IndexTests(unittest.TestCase):
    def test_keys_map_by_variant_key(self):
        indexed, issues = index_verdicts(
            reply(verdict("p1-v2")), ["p1-v1", "p1-v2"]
        )
        self.assertEqual(list(indexed), ["p1-v2"])
        self.assertEqual(issues, [])

    def test_unknown_key_falls_back_to_position(self):
        indexed, _ = index_verdicts(
            reply({**verdict(""), "variant_key": "nope"}), ["p1-v1", "p1-v2"]
        )
        self.assertIn("p1-v1", indexed)

    def test_duplicate_key_keeps_the_first(self):
        indexed, issues = index_verdicts(
            reply(verdict("p1-v1", c1_absolute="block"), verdict("p1-v1", c1_absolute="pass")),
            ["p1-v1"],
        )
        self.assertEqual(indexed["p1-v1"]["c1_absolute"], "block")
        self.assertTrue(any("duplicate" in item for item in issues))

    def test_non_list_payload_is_reported(self):
        indexed, issues = index_verdicts({"verdicts": {}}, ["p1-v1"])
        self.assertEqual(indexed, {})
        self.assertTrue(issues)


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        self.variants = make_proposal(2)["variants"]

    def test_worst_check_wins(self):
        verdicts, dropped, _ = finalize_verdicts(
            reply(verdict("p1-v1", c3_category="warn"), verdict("p1-v2", c1_absolute="block",
                                                              findings=[{"category": "c1_absolute",
                                                                         "quote": "100%",
                                                                         "rule": "绝对化",
                                                                         "suggestion": "删除"}])),
            variants=self.variants,
            suspects={},
        )
        self.assertEqual(len(verdicts), 2)
        self.assertEqual(dropped, [])
        self.assertEqual(verdicts[0]["severity"], "warn")
        self.assertTrue(verdicts[0]["passed"])
        self.assertEqual(verdicts[1]["severity"], "block")
        self.assertFalse(verdicts[1]["passed"])

    def test_missing_check_is_dropped(self):
        item = verdict("p1-v1")
        item.pop("c4_market")
        verdicts, dropped, _ = finalize_verdicts(
            reply(item, verdict("p1-v2")), variants=self.variants, suspects={}
        )
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(dropped[0]["variant_key"], "p1-v1")
        self.assertIn("c4_market", dropped[0]["reason"])

    def test_missing_variant_verdict_is_dropped(self):
        verdicts, dropped, _ = finalize_verdicts(
            reply(verdict("p1-v1")), variants=self.variants, suspects={}
        )
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(dropped[0]["variant_key"], "p1-v2")

    def test_block_without_findings_is_dropped(self):
        verdicts, dropped, _ = finalize_verdicts(
            reply(verdict("p1-v1", c1_absolute="block"), verdict("p1-v2")),
            variants=self.variants,
            suspects={},
        )
        self.assertEqual(len(verdicts), 1)
        self.assertIn("缺少依据", dropped[0]["reason"])

    def test_checks_nested_under_checks_key_are_accepted(self):
        item = {"variant_key": "p1-v1", "checks": {key: "pass" for key in
                ("c1_absolute", "c2_ip", "c3_category", "c4_market")}, "findings": []}
        verdicts, dropped, _ = finalize_verdicts(
            reply(item, verdict("p1-v2")), variants=self.variants, suspects={}
        )
        self.assertEqual(dropped, [])
        self.assertEqual(verdicts[0]["severity"], "pass")

    def test_headline_lists_all_four_checks(self):
        verdicts, _, _ = finalize_verdicts(
            reply(verdict("p1-v1"), verdict("p1-v2")), variants=self.variants, suspects={}
        )
        self.assertEqual(verdicts[0]["headline"].count(": "), 4)


class HardBanTests(unittest.TestCase):
    def test_brand_hit_overrides_a_pass(self):
        verdicts, _, _ = finalize_verdicts(
            reply(verdict("p1-v1")),
            variants=make_proposal(1)["variants"],
            suspects={"p1-v1": [{"rule_id": "BRD-001", "message": "未授权品牌词", "suggestion": "删除"}]},
        )
        self.assertEqual(verdicts[0]["severity"], "block")
        self.assertEqual(verdicts[0]["overridden_by"], "code")
        self.assertTrue(verdicts[0]["findings"])

    def test_prohibited_category_forces_block(self):
        checker = ComplianceChecker(
            lexicon={**DEFAULT_LEXICON, "prohibited_categories": ["home-kitchen"]}
        )
        suspects = lexicon_suspects(
            checker, make_proposal(1)["variants"], category="home-kitchen", market="US"
        )
        verdicts, _, _ = finalize_verdicts(
            reply(verdict("p1-v1")), variants=make_proposal(1)["variants"], suspects=suspects
        )
        self.assertEqual(verdicts[0]["severity"], "block")
        self.assertEqual(verdicts[0]["overridden_by"], "code")

    def test_absolute_term_does_not_force_a_block(self):
        """'100% cotton' is a composition, not a promise; the model decides."""
        suspects = {"p1-v1": [{"rule_id": "ABS-001", "message": "绝对化", "suggestion": "改写"}]}
        item = verdict("p1-v1")
        verdicts, _, _ = finalize_verdicts(
            reply(item), variants=make_proposal(1)["variants"], suspects=suspects
        )
        self.assertEqual(verdicts[0]["severity"], "pass")
        self.assertEqual(verdicts[0]["overridden_by"], "")

    def test_headline_reflects_the_code_override(self):
        verdicts, _, _ = finalize_verdicts(
            reply(verdict("p1-v1")),
            variants=make_proposal(1)["variants"],
            suspects={"p1-v1": [{"rule_id": "BRD-001", "message": "未授权品牌词", "suggestion": "删除"}]},
        )
        # Only the dimension the lexicon actually caught is painted red. Telling
        # the approver that absolute claims and market wording also failed would
        # send them rewriting copy the rules never complained about.
        self.assertEqual(verdicts[0]["checks"]["c2_ip"], "block")
        self.assertEqual(verdicts[0]["checks"]["c1_absolute"], "pass")
        self.assertEqual(verdicts[0]["checks"]["c4_market"], "pass")
        self.assertEqual(verdicts[0]["severity"], "block")
        self.assertEqual(verdicts[0]["headline"].count("否决"), 1)

    def test_hard_ban_finding_quotes_the_matched_term(self):
        verdicts, _, _ = finalize_verdicts(
            reply(verdict("p1-v1")),
            variants=make_proposal(1)["variants"],
            suspects={
                "p1-v1": [
                    {
                        "rule_id": "BRD-001",
                        "message": "未授权品牌词：nike",
                        "suggestion": "删除",
                        "quote": "nike",
                    }
                ]
            },
        )
        finding = verdicts[0]["findings"][0]
        self.assertEqual(finding["quote"], "nike")
        self.assertNotEqual(finding["quote"], "BRD-001")

    def test_apply_hard_ban_does_not_duplicate_findings(self):
        verdict = {
            "checks": {"c1_absolute": "pass", "c2_ip": "pass", "c3_category": "pass", "c4_market": "pass"},
            "severity": "pass",
            "findings": [{"category": "c2_ip", "quote": "BRD-001", "rule": "未授权品牌词", "suggestion": "删除"}],
        }
        result = apply_hard_ban(verdict, [{"rule_id": "BRD-001", "message": "未授权品牌词", "suggestion": "删除"}])
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["severity"], "block")


class RunComplianceTests(unittest.TestCase):
    def test_one_call_covers_all_variants(self):
        client = FakeClient([reply(verdict("p1-v1"), verdict("p1-v2"), verdict("p1-v3"))])
        result = run_compliance(
            proposal=make_proposal(3), product=make_product(), client=client
        )
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(result.verdicts), 3)

    def test_missing_verdict_triggers_one_repair(self):
        client = FakeClient(
            [reply(verdict("p1-v1")), reply(verdict("p1-v1"), verdict("p1-v2"))]
        )
        result = run_compliance(
            proposal=make_proposal(2), product=make_product(), client=client
        )
        self.assertEqual(client.calls, 2)
        self.assertEqual(len(result.verdicts), 2)

    def test_all_missing_raises(self):
        client = FakeClient([reply(), reply()])
        with self.assertRaises(ComplianceError):
            run_compliance(proposal=make_proposal(2), product=make_product(), client=client)

    def test_model_failure_propagates(self):
        client = FakeClient([LLMError("boom")])
        with self.assertRaises(LLMError):
            run_compliance(proposal=make_proposal(1), product=make_product(), client=client)

    def test_blocked_helper_counts_only_block(self):
        client = FakeClient(
            [
                reply(
                    verdict("p1-v1", c1_absolute="block", findings=[{"category": "c1_absolute",
                            "quote": "100%", "rule": "绝对化", "suggestion": "删除"}]),
                    verdict("p1-v2", c4_market="warn"),
                )
            ]
        )
        result = run_compliance(
            proposal=make_proposal(2), product=make_product(), client=client
        )
        self.assertEqual(len(result.blocked), 1)
        self.assertEqual(result.blocked[0]["variant_key"], "p1-v1")

    def test_empty_proposal_raises(self):
        with self.assertRaises(ComplianceError):
            run_compliance(
                proposal={"product_id": "p1", "variants": []},
                product=make_product(),
                client=FakeClient([reply()]),
            )


if __name__ == "__main__":
    unittest.main()
