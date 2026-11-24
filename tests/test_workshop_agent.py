import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.agents.base import RedLineGuard, WorkContext, known_facts_text  # noqa: E402
from ops_agent.agents.workshop_agent import (  # noqa: E402
    WorkshopError,
    build_payload,
    normalize_attempt,
    price_points_for,
    run_workshop_task,
    variant_range,
)
from ops_agent.config import LLMSettings  # noqa: E402
from ops_agent.llm import LLMError, LLMReply  # noqa: E402
from ops_agent.site_platform import SiteMetrics, SiteProduct, SiteVariant  # noqa: E402
from ops_agent.store_metrics import entry_variant  # noqa: E402

SETTINGS = LLMSettings(
    provider="deepseek",
    base_url="http://model.local/v1",
    model="test-model",
    api_key="k",
    max_calls_per_item=3,
    max_revision_rounds=2,
)


class FakeClient:
    def __init__(self, replies) -> None:
        self.settings = SETTINGS
        self._replies = list(replies)
        self.calls = 0
        self.messages: list[list[dict]] = []

    @property
    def enabled(self) -> bool:
        return True

    def chat(self, messages, temperature=None, max_tokens=None):
        self.calls += 1
        self.messages.append(messages)
        if not self._replies:
            raise LLMError("no replies left")
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMReply(text=json.dumps(item, ensure_ascii=False), model="test-model")


def make_product(price: int = 620, cost: int = 400) -> SiteProduct:
    metrics = SiteMetrics(30, 900, 600, 9, 0, 0, 0, 6, 3.6, 2)
    variant = SiteVariant("v1", "P1-01", price, cost, 800, {})
    return SiteProduct(
        product_id="p1",
        slug="p1",
        title_en="Ceramic Mug Set",
        status="active",
        category_id="c1",
        category_slug="home-kitchen",
        moq=100,
        lead_time_days=18,
        incoterm="FOB",
        variants=(variant,),
        metrics=metrics,
    )


def make_task(**overrides) -> dict:
    task = {
        "task_id": "run-1:p1",
        "run_id": "run-1",
        "product_id": "p1",
        "problem": "low_order",
        "problem_detail": "询盘正常但下单率为零",
        "evidence": ["下单率 0.00% vs 类目中位 1.20%", "库存 800 件"],
        "hypothesis": "补上阶梯价后下单率提升 12%",
        "success_metric": "order_rate",
        "min_lift": 0.12,
        "window_days": 14,
        "price_low_cents": 560,
        "price_high_cents": 680,
        "price_points": [560, 620, 680],
        "variant_min": 2,
        "variant_max": 3,
    }
    task.update(overrides)
    return task


def variant(index: int, **overrides) -> dict:
    item = {
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
    item.update(overrides)
    return item


def reply(count: int = 2, **overrides) -> dict:
    payload = {
        "variant_count": count,
        "variant_reason": "价格带窄、库存深，两版够用",
        "variants": [variant(index) for index in range(count)],
    }
    payload.update(overrides)
    return payload


def normalize(raw: dict, task: dict | None = None, max_rounds: int = 0):
    task = task or make_task()
    product = make_product()
    entry = entry_variant(product)
    low, high = variant_range(task)
    prices = price_points_for(task, entry, high)
    ctx = WorkContext(
        product=product,
        task=task,
        market="US",
        metrics=product.metrics,
        price_points=prices,
        current_title=product.title_en,
    )
    guard = RedLineGuard(prices, limit=high, facts_text=known_facts_text(ctx))
    return normalize_attempt(
        raw, task=task, ctx=ctx, guard=guard, variant_min=low, variant_max=high
    )


class VariantRangeTests(unittest.TestCase):
    def test_missing_bounds_default_to_the_minimum(self):
        self.assertEqual(variant_range({}), (2, 2))

    def test_bounds_are_clamped_to_the_contract(self):
        self.assertEqual(variant_range({"variant_min": 1, "variant_max": 9}), (2, 5))

    def test_inverted_bounds_collapse_to_the_lower_value(self):
        self.assertEqual(variant_range({"variant_min": 4, "variant_max": 2}), (4, 4))

    def test_valid_bounds_are_kept(self):
        self.assertEqual(variant_range({"variant_min": 3, "variant_max": 4}), (3, 4))


class PricePointTests(unittest.TestCase):
    def setUp(self):
        self.entry = entry_variant(make_product())

    def test_explicit_points_are_used_in_order(self):
        task = make_task(price_points=[500, 550, 600])
        self.assertEqual(price_points_for(task, self.entry, 3), [500, 550, 600])

    def test_short_list_falls_back_to_the_band(self):
        task = make_task(price_points=[560])
        self.assertEqual(price_points_for(task, self.entry, 3), [560, 620, 680])

    def test_band_without_points_is_split_evenly(self):
        task = make_task(price_points=None, price_low_cents=500, price_high_cents=700)
        self.assertEqual(price_points_for(task, self.entry, 3), [500, 600, 700])

    def test_zero_width_band_repeats_the_same_price(self):
        task = make_task(price_points=None, price_low_cents=600, price_high_cents=600)
        self.assertEqual(price_points_for(task, self.entry, 3), [600, 600, 600])


class NormalizeTests(unittest.TestCase):
    def test_clean_reply_has_no_issues(self):
        doc, issues = normalize(reply(2))
        self.assertEqual(issues, [])
        self.assertEqual(doc["variant_count"], 2)
        self.assertEqual([item["price_cents"] for item in doc["variants"]], [560, 620])

    def test_model_chooses_three_variants_inside_the_range(self):
        doc, issues = normalize(reply(3))
        self.assertEqual(issues, [])
        self.assertEqual(len(doc["variants"]), 3)

    def test_above_max_is_trimmed_without_a_repair(self):
        doc, issues = normalize(reply(4), max_rounds=1)
        self.assertEqual(len(doc["variants"]), 3)
        self.assertTrue(any("trimmed to variant_max" in item for item in issues))

    def test_below_min_is_reported(self):
        doc, issues = normalize(reply(1))
        self.assertTrue(any("at least 2" in item for item in issues))

    def test_missing_variant_reason_is_reported(self):
        doc, issues = normalize(reply(2, variant_reason=""))
        self.assertTrue(any("variant_reason" in item for item in issues))

    def test_duplicate_angles_are_rejected(self):
        raw = reply(2)
        raw["variants"][1]["angle"] = raw["variants"][0]["angle"]
        _, issues = normalize(raw)
        self.assertTrue(any("duplicate angle" in item for item in issues))

    def test_missing_self_check_is_reported(self):
        raw = reply(2)
        raw["variants"][0]["self_check"] = {"facts": "有核对"}
        _, issues = normalize(raw)
        self.assertTrue(any("self_check missing" in item for item in issues))

    def test_missing_material_brief_is_reported(self):
        raw = reply(2)
        raw["variants"][0]["material_brief"] = []
        _, issues = normalize(raw)
        self.assertTrue(any("material_brief is empty" in item for item in issues))

    def test_chinese_copy_is_dropped(self):
        raw = reply(2)
        raw["variants"][0]["title"] = "陶瓷马克杯四件套"
        doc, issues = normalize(raw)
        self.assertEqual(len(doc["variants"]), 1)
        self.assertTrue(any("not English" in item for item in issues))

    def test_invented_specification_is_removed_from_bullets(self):
        raw = reply(2)
        raw["variants"][0]["bullets"] = ["Weighs 1.9kg", "MOQ 100 pcs"]
        doc, issues = normalize(raw)
        self.assertEqual(len(doc["variants"]), 2)
        bullets = doc["variants"][0]["bullets"]
        self.assertNotIn("Weighs 1.9kg", bullets)
        self.assertTrue(any("unsupported numbers" in item for item in issues))

    def test_invented_number_in_title_drops_the_variant(self):
        raw = reply(2)
        raw["variants"][0]["title"] = "Ceramic Mug Set - 1.9kg Edition"
        doc, _ = normalize(raw)
        self.assertEqual(len(doc["variants"]), 1)
        self.assertEqual(doc["variants"][0]["angle"], "角度2")

    def test_price_index_out_of_range_is_forced_to_zero(self):
        raw = reply(2)
        raw["variants"][0]["price_index"] = 99
        doc, issues = normalize(raw)
        self.assertEqual(doc["variants"][0]["price_cents"], 560)
        self.assertTrue(any("price_index out of range" in item for item in issues))


class RunWorkshopTests(unittest.TestCase):
    def test_clean_first_draft_uses_one_call(self):
        client = FakeClient([reply(2)])
        result = run_workshop_task(task=make_task(), product=make_product(), client=client)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.calls, 1)
        self.assertEqual(len(result.variants), 2)

    def test_below_min_triggers_one_revision(self):
        client = FakeClient([reply(1), reply(2)])
        result = run_workshop_task(task=make_task(), product=make_product(), client=client)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(result.variants), 2)

    def test_revision_payload_carries_previous_and_issues(self):
        client = FakeClient([reply(1), reply(2)])
        run_workshop_task(task=make_task(), product=make_product(), client=client)
        second = json.loads(client.messages[1][1]["content"])
        self.assertIn("validation_issues", second)
        self.assertIn("previous_attempt", second)
        self.assertTrue(second["validation_issues"])

    def test_invented_number_rewrites_instead_of_falling_back(self):
        bad = reply(2)
        bad["variants"][0]["title"] = "Ceramic Mug Set - 1.9kg Edition"
        client = FakeClient([bad, reply(2)])
        result = run_workshop_task(task=make_task(), product=make_product(), client=client)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(result.variants), 2)
        self.assertNotIn("1.9", json.dumps(result.proposal))

    def test_revision_stops_at_the_budget(self):
        client = FakeClient([reply(1), reply(1), reply(1)])
        with self.assertRaises(WorkshopError):
            run_workshop_task(task=make_task(), product=make_product(), client=client)
        self.assertLessEqual(client.calls, 3)

    def test_no_usable_variant_raises(self):
        empty = {"variant_count": 0, "variant_reason": "x", "variants": []}
        client = FakeClient([empty, dict(empty), dict(empty)])
        with self.assertRaises(WorkshopError):
            run_workshop_task(task=make_task(), product=make_product(), client=client)

    def test_model_failure_propagates(self):
        client = FakeClient([LLMError("boom")])
        with self.assertRaises(LLMError):
            run_workshop_task(task=make_task(), product=make_product(), client=client)

    def test_broken_json_is_retried_once(self):
        client = FakeClient([LLMError("reply is not valid JSON"), reply(2)])
        result = run_workshop_task(task=make_task(), product=make_product(), client=client)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(result.variants), 2)

    def test_broken_json_twice_raises_a_model_error(self):
        client = FakeClient(
            [LLMError("reply is not valid JSON"), LLMError("reply is not valid JSON")]
        )
        with self.assertRaises(LLMError):
            run_workshop_task(
                task=make_task(), product=make_product(), client=client, max_rounds=1
            )

    def test_trimmed_reply_does_not_trigger_a_second_call(self):
        client = FakeClient([reply(4)])
        result = run_workshop_task(
            task=make_task(variant_max=3), product=make_product(), client=client
        )
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(result.variants), 3)


class PayloadTests(unittest.TestCase):
    def test_payload_carries_task_price_ladder_and_facts(self):
        task = make_task()
        product = make_product()
        ctx = WorkContext(
            product=product,
            task=task,
            market="DE",
            price_points=[560, 620, 680],
            current_title=product.title_en,
            current_bullets=["old bullet"],
        )
        payload = build_payload(ctx, variant_min=2, variant_max=3)
        self.assertEqual(payload["variant_min"], 2)
        self.assertEqual(payload["variant_max"], 3)
        self.assertEqual(payload["market"], "DE")
        self.assertEqual(payload["known_facts"]["moq_pcs"], 100)
        self.assertEqual(payload["task"]["hypothesis"], task["hypothesis"])
        self.assertEqual([item["cents"] for item in payload["price_points"]], [560, 620, 680])

    def test_payload_does_not_leak_cost_or_internal_ids(self):
        product = make_product()
        ctx = WorkContext(
            product=product,
            task=make_task(),
            market="US",
            price_points=[560, 620, 680],
            current_title=product.title_en,
        )
        payload = json.dumps(build_payload(ctx, variant_min=2, variant_max=3))
        self.assertNotIn("cost", payload.lower())
        self.assertNotIn("cost_cents", payload)


if __name__ == "__main__":
    unittest.main()
