import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.agents import CopyOrchestrator, WorkContext  # noqa: E402
from ops_agent.config import LLMSettings  # noqa: E402
from ops_agent.llm import LLMError, LLMReply  # noqa: E402
from ops_agent.site_platform import SiteMetrics, SiteProduct, SiteVariant  # noqa: E402
from ops_agent.supervisor import (  # noqa: E402
    Benchmark,
    WeeklyPlan,
    diagnosis_to_dict,
    diagnose,
    price_band,
    refine_plan,
)
from ops_agent.workshop import build_proposals  # noqa: E402

SETTINGS = LLMSettings(
    provider="deepseek", base_url="http://model.local/v1", model="test-model", api_key="k"
)


class FakeClient:
    def __init__(self, replies):
        self.settings = SETTINGS
        self._replies = list(replies)
        self.messages = []
        self.calls = 0

    @property
    def enabled(self):
        return True

    def chat_json(self, messages, temperature=None, max_tokens=None):
        self.calls += 1
        self.messages.append(messages)
        item = self._next()
        return item, LLMReply(text=json.dumps(item, ensure_ascii=False), model="test-model")

    def chat(self, messages, temperature=None, max_tokens=None):
        self.calls += 1
        self.messages.append(messages)
        return LLMReply(text=json.dumps(self._next(), ensure_ascii=False), model="test-model")

    def _next(self):
        if not self._replies:
            raise LLMError("no replies left")
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_product():
    metrics = SiteMetrics(
        days=30, pv=900, uv=600, inquiries=9, orders=0, units=0,
        revenue_cents=0, reviews=6, avg_rating=3.6, negative_reviews=2,
    )
    variant = SiteVariant(
        variant_id="v1", sku_code="P1-01", price_cents=620,
        cost_cents=400, stock=800, attrs={},
    )
    return SiteProduct(
        product_id="p1", slug="p1", title_en="Ceramic Mug Set", status="active",
        category_id="c1", category_slug="home-kitchen", moq=100,
        lead_time_days=18, incoterm="FOB", variants=(variant,), metrics=metrics,
    )


def make_plan():
    product = make_product()
    benchmark = Benchmark(
        pv=1000, inquiry_rate=0.01, order_rate=0.05, revenue_cents=1000, margin=0.3
    )
    diagnosis = diagnose(product, benchmark)
    assert diagnosis is not None
    plan = WeeklyPlan(
        generated_at="2026-11-02T20:00:00", store_slug="demo", scanned=1,
        matched=1, selected=[diagnosis], notes=[],
    )
    return plan


def strategy(problem="low_order"):
    return {
        "planner": "llm",
        "problem": problem,
        "hypothesis": "买家对颜色一致性有顾虑，且缺少下单信任证据",
        "action": "用真实颜色实拍和小单验证降低首次采购风险",
        "success_metric": "下单率提升且颜色相关差评下降",
        "reason": "评价原文直接指出颜色问题，需要先消除描述与实物差距",
        "review_indices": [0],
        "angles": [
            {"angle": "真实颜色实拍", "focus": "展示自然光下的真实釉色", "price_index": 2},
            {"angle": "小单先行", "focus": "先验样再放大订单", "price_index": 0},
            {"angle": "出货前核对", "focus": "出货前核对批次颜色", "price_index": 1},
        ],
        "confidence": 0.86,
    }


def draft():
    return {
        "variants": [
            {
                "angle": f"文案自造角度{index}", "title": f"Ceramic Mug Set — {index}",
                "bullets": ["Natural-light product photos", "MOQ 100 pcs"],
                "description": "Ceramic mugs with clear product photos before ordering.",
                "price_index": 0,
            }
            for index in range(3)
        ]
    }


PASS = {"score": 0.9, "verdict": "pass", "issues": [], "keep_indices": [0, 1, 2]}


class StrategistTests(unittest.TestCase):
    def test_model_chooses_supported_problem_and_real_review_evidence(self):
        plan = make_plan()
        self.assertIn("low_order", plan.selected[0].candidates)
        client = FakeClient([strategy()])
        reviews = {"p1": [{"rating": 2, "text": "The color differs from the photos"}]}
        refine_plan(plan, client=client, reviews=reviews)
        diagnosis = plan.selected[0]
        self.assertEqual(diagnosis.problem, "low_order")
        self.assertEqual(diagnosis.planner, "llm")
        self.assertEqual(diagnosis.price_band_cents, price_band(make_product().variants[0], "low_order"))
        self.assertTrue(any("color differs" in item for item in diagnosis.evidence))
        prompt = client.messages[0][1]["content"]
        self.assertIn("The color differs from the photos", prompt)
        self.assertIn("problem_options", prompt)

    def test_unsupported_problem_falls_back_to_rule(self):
        plan = make_plan()
        original = plan.selected[0].problem
        client = FakeClient([strategy(problem="low_margin")])
        refine_plan(plan, client=client)
        self.assertEqual(plan.selected[0].problem, original)
        self.assertEqual(plan.selected[0].planner, "rule (fallback)")

    def test_invalid_strategy_price_index_is_corrected(self):
        reply = strategy()
        reply["angles"][0]["price_index"] = 99
        reply["angles"][0]["price_cents"] = 1
        plan = make_plan()
        refine_plan(plan, client=FakeClient([reply]))
        result = plan.selected[0].strategy
        self.assertEqual(result["angles"][0]["price_index"], 0)
        self.assertNotIn("price_cents", result["angles"][0])
        self.assertTrue(result["violations"])

    def test_task_order_serializes_strategy_without_internal_options(self):
        plan = make_plan()
        refine_plan(plan, client=FakeClient([strategy()]))
        item = diagnosis_to_dict(plan.selected[0])
        self.assertEqual(item["planner"], "llm")
        self.assertEqual(item["strategy"]["angles"][1]["price_index"], 0)
        self.assertNotIn("strategy_options", item)

    def test_workshop_owns_angles_instead_of_legacy_strategist(self):
        """P2: the task order sets the band and the count, the workshop sets the angles."""
        plan = make_plan()
        refine_plan(plan, client=FakeClient([strategy()]))
        task = diagnosis_to_dict(plan.selected[0])
        self.assertIn("angles", task.get("strategy") or {})  # legacy field still present
        reply = {
            "variant_count": 2,
            "variant_reason": "价格带窄，两版够用",
            "variants": [
                {
                    "angle": "价格锚点",
                    "title": "Ceramic Mug Set - Tier Pricing",
                    "bullets": ["MOQ 100 pcs", "Lead time 18 days"],
                    "description": "Tier pricing for cafes that reorder monthly.",
                    "price_index": 0,
                    "material_brief": ["主体：四只装", "禁用：品牌 logo"],
                    "self_check": {"facts": "数字来自 listing", "differentiation": "价格角度", "readability": "短句"},
                    "self_review": "无改动",
                },
                {
                    "angle": "质检证据",
                    "title": "Ceramic Mug Set - Batch QC Included",
                    "bullets": ["Batch QC report included", "MOQ 100 pcs"],
                    "description": "A QC report ships with every batch.",
                    "price_index": 1,
                    "material_brief": ["主体：质检场景"],
                    "self_check": {"facts": "QC 来自 listing", "differentiation": "证据角度", "readability": "短句"},
                    "self_review": "无改动",
                },
            ],
        }
        proposals = build_proposals(
            [task], {"p1": make_product()}, limit=1, planner="llm", client=FakeClient([reply])
        )
        self.assertEqual([item.angle for item in proposals[0].variants], ["价格锚点", "质检证据"])
        self.assertEqual(len(proposals[0].variants), 2)

    def test_copywriter_cannot_override_strategist_angles_or_prices(self):
        task = {
            "problem": "low_order",
            "strategy": strategy(),
        }
        ctx = WorkContext(
            product=make_product(), task=task, metrics=make_product().metrics,
            price_points=[560, 620, 680], current_title="Ceramic Mug Set",
        )
        outcome = CopyOrchestrator(client=FakeClient([draft(), PASS])).generate(ctx)
        self.assertEqual([item["price_cents"] for item in outcome.variants], [680, 560, 620])
        self.assertEqual(
            [item["angle"] for item in outcome.variants],
            ["真实颜色实拍", "小单先行", "出货前核对"],
        )
        self.assertTrue(any("aligned to strategist" in item for item in outcome.violations))


if __name__ == "__main__":
    unittest.main()
