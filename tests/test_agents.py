import json
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.agents import CopyOrchestrator, RedLineGuard, WorkContext, allowed_tools  # noqa: E402
from ops_agent.config import LLMSettings  # noqa: E402
from ops_agent.llm import LLMError, LLMReply  # noqa: E402
from ops_agent.site_platform import SiteMetrics, SiteProduct, SiteVariant  # noqa: E402
from ops_agent.workshop import build_proposals, proposals_to_dict  # noqa: E402

SETTINGS = LLMSettings(
    provider="deepseek",
    base_url="http://model.local/v1",
    model="test-model",
    api_key="k",
    max_calls_per_item=6,
    max_revision_rounds=2,
)


class FakeClient:
    """Stands in for LLMClient; no network, deterministic replies."""

    def __init__(self, replies, settings: LLMSettings | None = None) -> None:
        self.settings = settings or SETTINGS
        self._replies = list(replies)
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return True

    def chat_json(self, messages, temperature=None, max_tokens=None):
        self.calls += 1
        return self._next()

    def chat(self, messages, temperature=None, max_tokens=None):
        # The workshop agent talks to the plain chat endpoint, like LLMClient.
        self.calls += 1
        return self._next()[1]

    def _next(self):
        if not self._replies:
            raise LLMError("no replies left")
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, LLMReply(text=json.dumps(item, ensure_ascii=False), model="test-model",
                              prompt_tokens=10, completion_tokens=5)


def make_product(product_id: str = "p1") -> SiteProduct:
    metrics = SiteMetrics(days=30, pv=900, uv=600, inquiries=3, orders=0, units=0,
                          revenue_cents=0, reviews=6, avg_rating=3.6, negative_reviews=2)
    variant = SiteVariant(variant_id="v1", sku_code="P1-01", price_cents=620,
                          cost_cents=400, stock=800, attrs={})
    return SiteProduct(product_id=product_id, slug=product_id, title_en="Ceramic Mug Set",
                       status="active", category_id="c1", category_slug="home-kitchen",
                       moq=100, lead_time_days=18, incoterm="FOB",
                       variants=(variant,), metrics=metrics)


def make_task() -> dict:
    return {"product_id": "p1", "title": "Ceramic Mug Set", "problem": "high_negative",
            "price_band_cents": [560, 680], "margin_floor": 0.16, "budget_tokens": 120000}


def draft(count: int = 3, price_index: int = 1, extra: dict | None = None) -> dict:
    variants = []
    for index in range(count):
        item = {
            "angle": f"角度{index + 1}",
            "title": f"Ceramic Mug Set — Angle {index + 1}",
            "bullets": ["Batch QC report included", "MOQ 100 pcs"],
            "description": "Stoneware mugs with reactive glaze, batch QC before shipment.",
            "price_index": price_index,
            "expected_lift": 0.08,
            "rationale": "差评集中在色差，所以强调实拍与质检",
        }
        if extra:
            item.update(extra)
        variants.append(item)
    return {"variants": variants}


PASS_CRITIQUE = {"score": 0.82, "verdict": "pass", "issues": [], "keep_indices": [0, 1, 2]}
REVISE_CRITIQUE = {"score": 0.30, "verdict": "revise", "issues": ["没有回应色差"], "keep_indices": []}


def workshop_reply(count: int = 2) -> dict:
    """A workshop-agent reply: self-check, brief and price index included."""
    variants = []
    for index in range(count):
        variants.append(
            {
                "angle": f"角度{index + 1}",
                "title": f"Ceramic Mug Set - Angle {index + 1}",
                "bullets": ["Batch QC report included", "MOQ 100 pcs"],
                "description": "Stoneware mugs with reactive glaze, batch QC before shipment.",
                "price_index": index,
                "material_brief": ["主体：四只装马克杯", "字幕：MOQ 100 pcs", "禁用：品牌 logo"],
                "self_check": {
                    "facts": "数字全部来自 listing",
                    "differentiation": "与另一版角度不同",
                    "readability": "两句话读完",
                },
                "self_review": "删掉了一个自造的容量数字",
            }
        )
    return {
        "variant_count": count,
        "variant_reason": "价格带窄、库存深，两版够用",
        "variants": variants,
    }


def make_ctx(price_points=None) -> WorkContext:
    return WorkContext(
        product=make_product(),
        task=make_task(),
        market="US",
        metrics=make_product().metrics,
        reviews=[{"rating": 2, "text": "颜色和图片不一样", "locale": "us"}],
        price_points=price_points or [560, 620, 680],
        current_title="Ceramic Mug Set",
        current_bullets=["old bullet"],
    )


class GuardTests(unittest.TestCase):
    def test_forbidden_fields_are_dropped(self):
        guard = RedLineGuard([100, 200, 300])
        result = guard.check_variants([
            {"title": "T", "bullets": ["b"], "description": "D",
             "status": "inactive", "costCents": 1, "slug": "hack"}
        ])
        self.assertEqual(len(result.variants), 1)
        cleaned = result.variants[0]
        self.assertNotIn("status", cleaned)
        self.assertNotIn("costCents", cleaned)
        self.assertNotIn("slug", cleaned)
        self.assertTrue(any("forbidden field" in item for item in result.violations))

    def test_out_of_range_price_index_is_forced_to_first_point(self):
        guard = RedLineGuard([100, 200, 300])
        result = guard.check_variants([
            {"title": "T", "bullets": ["b"], "description": "D", "price_index": 99}
        ])
        self.assertEqual(result.variants[0]["price_cents"], 100)

    def test_model_written_price_is_ignored(self):
        guard = RedLineGuard([100, 200, 300])
        result = guard.check_variants([
            {"title": "T", "bullets": ["b"], "description": "D", "price_cents": 1, "price_index": 2}
        ])
        self.assertEqual(result.variants[0]["price_cents"], 300)

    def test_incomplete_variant_is_dropped(self):
        guard = RedLineGuard([100])
        result = guard.check_variants([{"title": "", "bullets": [], "description": ""}])
        self.assertEqual(result.variants, [])
        self.assertTrue(result.violations)

    def test_bullets_are_capped(self):
        guard = RedLineGuard([100])
        result = guard.check_variants([
            {"title": "T", "bullets": [f"b{i}" for i in range(9)], "description": "D"}
        ])
        self.assertEqual(len(result.variants[0]["bullets"]), 5)

    def test_variant_limit_is_respected(self):
        guard = RedLineGuard([100], limit=2)
        result = guard.check_variants(draft(count=5)["variants"])
        self.assertEqual(len(result.variants), 2)

    def test_chinese_copy_is_rejected_for_english_fields(self):
        """titleEn / bulletsEn must never receive Chinese text."""
        guard = RedLineGuard([100])
        result = guard.check_variants([
            {"title": "三人弹出式沙滩帐", "bullets": ["UPF 50+"], "description": "适合三人使用"},
        ])
        self.assertEqual(result.variants, [])
        self.assertTrue(any("not English" in item for item in result.violations))

    def test_english_copy_with_cjk_punctuation_survives(self):
        guard = RedLineGuard([100])
        result = guard.check_variants([
            {"title": "3-Person Pop-Up Beach Tent — UPF 50+",
             "bullets": ["Sets up in seconds"],
             "description": "Built for family beach trips."},
        ])
        self.assertEqual(len(result.variants), 1)

    def test_bullet_with_invented_spec_number_is_dropped(self):
        """A spec the model was never given is a false claim on a live storefront."""
        guard = RedLineGuard([100], facts_text="3-Person Pop-Up Beach Tent UPF 50+ MOQ 100")
        result = guard.check_variants([
            {"title": "3-Person Pop-Up Beach Tent UPF 50+",
             "bullets": ["600D Oxford fabric", "MOQ 100 units"],
             "description": "A practical beach tent for family trips."},
        ])
        self.assertEqual(result.variants[0]["bullets"], ["MOQ 100 units"])
        self.assertTrue(any("unsupported numbers" in item for item in result.violations))

    def test_invented_number_in_description_drops_variant(self):
        guard = RedLineGuard([100], facts_text="3-Person Pop-Up Beach Tent")
        result = guard.check_variants([
            {"title": "3-Person Pop-Up Beach Tent",
             "bullets": ["Pop-up frame"],
             "description": "Weighs 2.2kg and packs to 38cm."},
        ])
        self.assertEqual(result.variants, [])
        self.assertTrue(any("unsupported numeric claim" in item for item in result.violations))

    def test_invented_percentage_is_dropped(self):
        """Two-digit percentages used to slip past the guard entirely."""
        guard = RedLineGuard([100], facts_text="Scented Candle, 45-hour burn")
        result = guard.check_variants([
            {"title": "Scented Candle",
             "bullets": ["Burns up to 30% longer", "Pop-up design"],
             "description": "A calm, clean-burning candle."},
        ])
        self.assertEqual(result.variants[0]["bullets"], ["Pop-up design"])
        self.assertTrue(any("unsupported numbers" in item for item in result.violations))

    def test_percentage_given_in_facts_is_allowed(self):
        guard = RedLineGuard([100], facts_text="Scented Candle, 99% soy wax, 45-hour burn")
        result = guard.check_variants([
            {"title": "Scented Candle",
             "bullets": ["99% soy wax"],
             "description": "A calm, clean-burning candle."},
        ])
        self.assertEqual(result.variants[0]["bullets"], ["99% soy wax"])

    def test_numbers_from_current_listing_are_allowed(self):
        guard = RedLineGuard([100], facts_text="Foldable Chair 150kg Load, 1.9kg, 38cm")
        result = guard.check_variants([
            {"title": "Foldable Chair — 150kg Load",
             "bullets": ["Folds to 38cm", "Weighs 1.9kg"],
             "description": "A compact chair for camping."},
        ])
        self.assertEqual(len(result.variants), 1)

    def test_thousands_separator_is_one_number(self):
        """5,000 must not be parsed as 5 and 000, which would look invented."""
        guard = RedLineGuard([100], facts_text="Custom Woven Label Roll 5,000 pcs MOQ")
        result = guard.check_variants([
            {"title": "Custom Woven Labels at a 5,000-Piece MOQ",
             "bullets": ["5,000-piece minimum order"],
             "description": "Labels supplied on a roll for bulk buyers."},
        ])
        self.assertEqual(len(result.variants), 1)


class ToolBoundaryTests(unittest.TestCase):
    def test_publish_tools_are_not_offered_to_agents(self):
        class Registry:
            def specs(self):
                return [
                    {"name": "get_shop_snapshot", "side_effect": "read"},
                    {"name": "price_calc", "side_effect": "compute"},
                    {"name": "publish_listing", "side_effect": "write"},
                    {"name": "rollback", "side_effect": "write"},
                ]

        self.assertEqual(allowed_tools(Registry()), ["get_shop_snapshot", "price_calc"])


class OrchestratorTests(unittest.TestCase):
    def test_pass_on_first_critique(self):
        client = FakeClient([draft(), PASS_CRITIQUE])
        outcome = CopyOrchestrator(client=client).generate(make_ctx())
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.rounds, 1)
        self.assertEqual(outcome.calls, 2)
        self.assertEqual(len(outcome.variants), 3)
        self.assertEqual(outcome.variants[0]["price_cents"], 620)

    def test_revise_triggers_one_rewrite(self):
        client = FakeClient([draft(), REVISE_CRITIQUE, draft(price_index=0), PASS_CRITIQUE])
        outcome = CopyOrchestrator(client=client).generate(make_ctx())
        self.assertEqual(outcome.rounds, 2)
        self.assertEqual(client.calls, 4)
        self.assertEqual(outcome.variants[0]["price_cents"], 560)
        self.assertEqual(outcome.critique["verdict"], "pass")

    def test_budget_cap_stops_extra_calls(self):
        tight = LLMSettings(provider="deepseek", base_url="http://x/v1", model="m",
                            api_key="k", max_calls_per_item=3, max_revision_rounds=5)
        client = FakeClient([draft(), REVISE_CRITIQUE, draft(), REVISE_CRITIQUE], settings=tight)
        outcome = CopyOrchestrator(client=client).generate(make_ctx())
        self.assertLessEqual(client.calls, 3)
        self.assertTrue(outcome.ok)

    def test_empty_after_guard_raises(self):
        bad = {"variants": [{"title": "", "bullets": [], "description": ""}]}
        client = FakeClient([bad])
        with self.assertRaises(LLMError):
            CopyOrchestrator(client=client).generate(make_ctx())

    def test_no_price_points_raises(self):
        ctx = make_ctx(price_points=[])
        with self.assertRaises(LLMError):
            CopyOrchestrator(client=FakeClient([draft()])).generate(ctx)


class WorkshopPlannerTests(unittest.TestCase):
    def test_rule_planner_is_default(self):
        proposals = build_proposals([make_task()], {"p1": make_product()}, limit=1)
        self.assertEqual(proposals[0].planner, "rule")

    def test_llm_planner_uses_the_workshop_agent(self):
        client = FakeClient([workshop_reply(2)])
        proposals = build_proposals(
            [make_task()], {"p1": make_product()}, limit=1, planner="llm", client=client
        )
        self.assertEqual(proposals[0].planner, "llm")
        self.assertEqual(proposals[0].revisions, 0)
        self.assertEqual(len(proposals[0].variants), 2)
        self.assertEqual(proposals[0].variants[0].self_check["facts"], "数字全部来自 listing")
        self.assertEqual(len(proposals[0].variants[0].material_brief), 3)
        self.assertTrue(proposals[0].variants[0].compliance_passed)

    def test_llm_failure_is_reported_not_replaced_by_rule(self):
        """P2 changed this on purpose: no silent template fallback."""
        failures: list[dict] = []
        client = FakeClient([LLMError("boom")])
        proposals = build_proposals(
            [make_task()],
            {"p1": make_product()},
            limit=1,
            planner="llm",
            client=client,
            failures=failures,
        )
        self.assertEqual(proposals, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("boom", failures[0]["reason"])

    def test_payload_carries_planner_metadata(self):
        client = FakeClient([workshop_reply(2)])
        proposals = build_proposals(
            [make_task()], {"p1": make_product()}, limit=1, planner="llm", client=client
        )
        payload = proposals_to_dict("demo", proposals)
        item = payload["proposals"][0]
        self.assertEqual(item["planner"], "llm")
        self.assertEqual(item["critique"]["planner"], "workshop-agent")
        self.assertEqual(item["variants"][0]["self_review"], "删掉了一个自造的容量数字")

    def test_no_stacked_suffix_on_republish(self):
        from dataclasses import replace

        product = replace(make_product(), title_en="Ceramic Mug Set — Bulk Pricing Available")
        proposals = build_proposals([make_task()], {"p1": product}, limit=1)
        for variant in proposals[0].variants:
            self.assertNotIn("Bulk Pricing Available — ", variant.title)


if __name__ == "__main__":
    unittest.main()
