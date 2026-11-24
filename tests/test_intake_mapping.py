"""Step 3 的映射层测试：意图结果 → 合约 dict（dry-run）。

覆盖 PRD 要求的四类：
1. 产物形状：dict，键名走 contracts 那一套（task_id / product_id / price_low_cents），
   且能过 validate_task_order——不再实例化 models.TaskOrder；
2. 多 SKU 拆分：批量上新 → N 条单，编号稳定、可复现；
3. 缺槽位补默认：补过的值必须落在 slots["assumed"] 里，不能悄悄补；
4. 空列表场景：不满足最小条件时给空列表 + 原因，绝不抛异常。

另外两条边界，是本轮修订新增的：
- draft 单豁免 evidence 下限，但其余校验不变；
- draft 单不得进入内容工坊（workshop 拒绝，而不是靠调用方自觉）。
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.contracts import MAX_WINDOW_DAYS, MIN_WINDOW_DAYS, validate_task_order  # noqa: E402
from ops_agent.intake import Intent, IntentResult, classify  # noqa: E402
from ops_agent.intake.mapping import (  # noqa: E402
    MAX_ORDERS,
    ORDER_ARTIFACTS,
    plan_intent,
    to_task_orders,
)
from ops_agent.intake.schema import INTENT_SPECS  # noqa: E402
from ops_agent.workshop import build_proposals  # noqa: E402


def make_products(count: int, *, status: str = "draft", price_cents: int = 2999,
                  cost_cents: int | None = 1200, start: int = 1) -> list[dict]:
    return [
        {
            "product_id": f"SKU-{index:04d}",
            "title": f"Widget {index}",
            "status": status,
            "price_cents": price_cents,
            "cost_cents": cost_cents,
        }
        for index in range(start, start + count)
    ]


def make_ctx(products: list[dict] | None = None, **over) -> dict:
    ctx = {
        "store_slug": "demo",
        "default_market": "US",
        "default_language": "en",
        "margin_floor": 0.35,
        "budget_tokens": 60000,
        "products": list(products or []),
    }
    ctx.update(over)
    return ctx


def result_for(text: str) -> IntentResult:
    return classify(text)


def band(order: dict) -> tuple[int, int]:
    return order["price_low_cents"], order["price_high_cents"]


class ContractShapeTests(unittest.TestCase):
    """产物必须是合约 dict：键名对得上 contracts，且真的能过校验。"""

    def test_order_is_a_plain_dict_not_a_dataclass(self):
        outcome = plan_intent(result_for("这20个新品上到美国站"), make_ctx(make_products(2)))
        order = outcome.orders[0]
        self.assertIsInstance(order, dict)
        self.assertEqual("BATCH_UPLOAD:SKU-0001", order["task_id"])
        self.assertEqual("SKU-0001", order["product_id"])
        self.assertIn("price_low_cents", order)
        self.assertIn("price_high_cents", order)
        self.assertNotIn("order_id", order)
        self.assertNotIn("sku_id", order)
        self.assertNotIn("price_band", order)

    def test_every_order_passes_the_contract(self):
        for text in ("这20个新品上到美国站", "把这条链接的标题重写一下 SKU-0003"):
            outcome = plan_intent(result_for(text), make_ctx(make_products(5)))
            self.assertTrue(outcome.orders, text)
            for order in outcome.orders:
                with self.subTest(text=text, task=order["task_id"]):
                    self.assertEqual([], validate_task_order(order))

    def test_draft_orders_declare_status_and_source(self):
        outcome = plan_intent(result_for("这20个新品上到美国站"), make_ctx(make_products(2)))
        for order in outcome.orders:
            self.assertEqual("draft", order["status"])
            self.assertEqual("intent", order["source"])

    def test_order_carries_a_bounded_intake_trace(self):
        text = "这20个新品上到美国站"
        order = plan_intent(result_for(text), make_ctx(make_products(1))).orders[0]
        intake = order["intake"]
        self.assertEqual("BATCH_UPLOAD", intake["intent"])
        self.assertEqual("task_orders", intake["artifact"])
        self.assertEqual(text, intake["text"])
        self.assertFalse(intake["text_truncated"])

    def test_long_intake_text_is_truncated_in_the_order_but_not_in_history(self):
        # 订单会被落进 task_orders.json，原文只留摘要；完整原话由 history.jsonl 负责。
        long_text = "这2个新品上到美国站，" + "备注" * 200
        order = plan_intent(result_for(long_text), make_ctx(make_products(1))).orders[0]
        self.assertLessEqual(len(order["intake"]["text"]), 200)
        self.assertTrue(order["intake"]["text_truncated"])

    def test_contract_defaults_are_valid_ranges(self):
        order = plan_intent(result_for("这20个新品上到美国站"), make_ctx(make_products(1))).orders[0]
        self.assertTrue(MIN_WINDOW_DAYS <= order["window_days"] <= MAX_WINDOW_DAYS)
        self.assertLessEqual(order["variant_min"], order["variant_max"])
        self.assertTrue(0 < order["min_lift"] <= 1)

    def test_orders_are_json_serialisable(self):
        outcome = plan_intent(result_for("这20个新品上到美国站"), make_ctx(make_products(3)))
        dumped = json.dumps(outcome.to_dict(), ensure_ascii=False)
        self.assertIn('"task_id"', dumped)


class DraftContractTests(unittest.TestCase):
    """draft 只在 evidence 上享受豁免，其它校验一个都不能少。"""

    def test_draft_is_exempt_from_the_evidence_floor(self):
        outcome = plan_intent(result_for("把这条链接的标题重写一下 SKU-0001"),
                              make_ctx(make_products(1)))
        order = outcome.orders[0]
        self.assertEqual([], order["evidence"])
        self.assertEqual([], validate_task_order(order))

    def test_empty_evidence_still_fails_without_draft_status(self):
        outcome = plan_intent(result_for("把这条链接的标题重写一下 SKU-0001"),
                              make_ctx(make_products(1)))
        order = dict(outcome.orders[0], status="ready")
        errors = validate_task_order(order)
        self.assertTrue(any("evidence" in item for item in errors), errors)

    def test_draft_does_not_bypass_the_price_band_checks(self):
        outcome = plan_intent(result_for("把这条链接的标题重写一下 SKU-0001"),
                              make_ctx(make_products(1)))
        broken = dict(outcome.orders[0], price_points=[1])
        self.assertTrue(validate_task_order(broken))

    def test_draft_does_not_bypass_the_window_range(self):
        outcome = plan_intent(result_for("把这条链接的标题重写一下 SKU-0001"),
                              make_ctx(make_products(1)))
        broken = dict(outcome.orders[0], window_days=3)
        self.assertTrue(validate_task_order(broken))

    def test_status_is_normalised_to_lowercase(self):
        from ops_agent.contracts import normalize_task_order

        doc = normalize_task_order({"task_id": "T", "status": "DRAFT"})
        self.assertEqual("draft", doc["status"])


class BatchSplitTests(unittest.TestCase):
    def test_batch_upload_splits_into_one_order_per_sku(self):
        outcome = plan_intent(result_for("这20个新品上到美国站"), make_ctx(make_products(30)))
        self.assertEqual(20, len(outcome.orders))
        self.assertEqual(20, len({order["product_id"] for order in outcome.orders}))
        first = outcome.orders[0]["slots"]
        self.assertEqual([1, 20], [first["batch_index"], first["batch_size"]])

    def test_quantity_larger_than_candidates_is_capped_and_explained(self):
        outcome = plan_intent(result_for("这20个新品上到美国站"), make_ctx(make_products(8)))
        self.assertEqual(8, len(outcome.orders))
        self.assertTrue(any("候选只有 8 条" in note for note in outcome.notes))

    def test_quantity_respects_max_orders_cap(self):
        outcome = plan_intent(result_for("这200个新品上到美国站"), make_ctx(make_products(80)))
        self.assertEqual(MAX_ORDERS, len(outcome.orders))
        self.assertTrue(any("上限" in note for note in outcome.notes))

    def test_missing_quantity_falls_back_to_candidate_count(self):
        result = IntentResult(intent=Intent.BATCH_UPLOAD, confidence=0.9, slots={"market": "US"})
        outcome = plan_intent(result, make_ctx(make_products(3)))
        self.assertEqual(3, len(outcome.orders))
        self.assertTrue(any("没说数量" in note for note in outcome.notes))

    def test_drafts_are_preferred_over_active_products(self):
        products = make_products(2, status="active", start=1) + make_products(2, status="draft", start=10)
        outcome = plan_intent(result_for("把这2个新品上到美国站"), make_ctx(products))
        self.assertEqual(["SKU-0010", "SKU-0011"], [order["product_id"] for order in outcome.orders])


class AssumedSlotTests(unittest.TestCase):
    def test_defaulted_slots_are_marked_assumed(self):
        outcome = plan_intent(result_for("这20个新品上到美国站"), make_ctx(make_products(5)))
        slots = outcome.orders[0]["slots"]
        self.assertEqual("US", slots["market"])
        self.assertEqual("en", slots["language"])
        self.assertTrue(slots["assumed"].get("language"))
        self.assertEqual(["source_path"], slots["pending_input"])

    def test_market_from_text_is_not_marked_assumed(self):
        outcome = plan_intent(result_for("把这2个新品上到德国站"), make_ctx(make_products(4)))
        self.assertEqual("DE", outcome.orders[0]["slots"]["market"])
        self.assertNotIn("market", outcome.orders[0]["slots"]["assumed"])

    def test_store_default_market_is_used_when_text_is_silent(self):
        result = IntentResult(intent=Intent.BATCH_UPLOAD, confidence=0.9, slots={"quantity": 1})
        outcome = plan_intent(result, make_ctx(make_products(2), default_market="UK"))
        self.assertEqual("UK", outcome.orders[0]["slots"]["market"])
        self.assertTrue(outcome.orders[0]["slots"]["assumed"].get("market"))


class EmptyOutcomeTests(unittest.TestCase):
    """不满足最小条件 → 空列表 + 原因，不抛异常。"""

    def test_clarify_produces_no_order(self):
        outcome = plan_intent(result_for("这个产品的价格和销量怎么样"), make_ctx(make_products(3)))
        self.assertFalse(outcome.ok)
        self.assertIn("意图未定", outcome.reason)

    def test_unknown_produces_no_order(self):
        outcome = plan_intent(result_for("今天天气不错"), make_ctx(make_products(3)))
        self.assertEqual([], outcome.orders)
        self.assertTrue(outcome.reason)

    def test_read_only_artifact_produces_no_order(self):
        outcome = plan_intent(result_for("上周美国站销售额和转化率是多少"), make_ctx(make_products(3)))
        self.assertEqual([], outcome.orders)
        self.assertIn("answer", outcome.reason)

    def test_memory_manage_produces_no_order(self):
        outcome = plan_intent(result_for("记住，以后宠物类目一律不做"), make_ctx(make_products(3)))
        self.assertEqual([], outcome.orders)
        self.assertIn("memory_record", outcome.reason)

    def test_customer_outreach_produces_no_order(self):
        outcome = plan_intent(result_for("给这个买家写一封催评邮件"), make_ctx(make_products(3)))
        self.assertEqual([], outcome.orders)
        self.assertIn("outreach_draft", outcome.reason)

    def test_no_candidates_means_no_order(self):
        outcome = plan_intent(result_for("这20个新品上到美国站"), make_ctx([]))
        self.assertEqual([], outcome.orders)
        self.assertIn("候选", outcome.reason)

    def test_single_intent_without_sku_and_many_candidates_asks_instead_of_guessing(self):
        outcome = plan_intent(result_for("把这条链接的标题重写一下，突出防水卖点"), make_ctx(make_products(5)))
        self.assertEqual([], outcome.orders)
        self.assertIn("缺 sku", outcome.reason)

    def test_single_intent_with_one_candidate_binds_it(self):
        outcome = plan_intent(result_for("把这条链接的标题重写一下，突出防水卖点"), make_ctx(make_products(1)))
        self.assertEqual(1, len(outcome.orders))
        self.assertEqual("SKU-0001", outcome.orders[0]["product_id"])
        self.assertTrue(any("只有一条候选" in note for note in outcome.notes))

    def test_explicit_sku_wins(self):
        outcome = plan_intent(
            result_for("把这条链接的标题重写一下，突出防水卖点 SKU-0007"),
            make_ctx(make_products(10)),
        )
        self.assertEqual(1, len(outcome.orders))
        self.assertEqual("SKU-0007", outcome.orders[0]["product_id"])
        self.assertEqual("标题", outcome.orders[0]["slots"]["field"])

    def test_pasted_product_id_binds_that_product(self):
        products = make_products(5)
        text = f"把 {products[2]['product_id']} 这条链接的标题重写一下，突出防水卖点"
        outcome = plan_intent(result_for(text), make_ctx(products))
        self.assertEqual(1, len(outcome.orders))
        self.assertEqual(products[2]["product_id"], outcome.orders[0]["product_id"])
        self.assertNotIn("sku", outcome.orders[0]["slots"]["pending_input"])

    def test_product_title_in_text_binds_that_product(self):
        products = [
            {"product_id": "abc123", "title": "ANC Wireless Earbuds with Charging Case",
             "status": "draft", "price_cents": 2999, "cost_cents": 1200},
            {"product_id": "def456", "title": "Stainless Steel Water Bottle 1L",
             "status": "draft", "price_cents": 1999, "cost_cents": 800},
        ]
        text = "把 Stainless Steel Water Bottle 1L 的标题重写一下"
        outcome = plan_intent(result_for(text), make_ctx(products))
        self.assertEqual(["def456"], [order["product_id"] for order in outcome.orders])

    def test_unknown_sku_is_reported_not_invented(self):
        result = IntentResult(intent=Intent.LISTING_REWRITE, confidence=0.9, slots={"sku": "SKU-9999"})
        outcome = plan_intent(result, make_ctx(make_products(3)))
        self.assertEqual([], outcome.orders)
        self.assertIn("SKU-9999", outcome.reason)

    def test_product_without_price_is_skipped(self):
        products = [{"product_id": "SKU-0001", "title": "无价", "status": "draft", "price_cents": 0}]
        outcome = plan_intent(result_for("把这条链接的标题重写一下"), make_ctx(products))
        self.assertEqual([], outcome.orders)
        self.assertIn("价格", outcome.reason)


class PriceBandTests(unittest.TestCase):
    def test_band_straddles_current_price(self):
        outcome = plan_intent(result_for("把这条链接的标题重写一下 SKU-0001"),
                              make_ctx(make_products(1, price_cents=10000, cost_cents=2000)))
        low, high = band(outcome.orders[0])
        self.assertLess(low, 10000)
        self.assertGreater(high, 10000)

    def test_band_low_respects_margin_floor(self):
        outcome = plan_intent(result_for("把这条链接的标题重写一下 SKU-0001"),
                              make_ctx(make_products(1, price_cents=2000, cost_cents=1800)))
        low, _high = band(outcome.orders[0])
        # cost 18 / (1-0.35) = 27.69 —— 下沿必须抬到毛利线之上，不能按 90% 给 18
        self.assertGreaterEqual(low, 2769)

    def test_invalid_margin_floor_falls_back_to_default(self):
        # margin_floor=1.5 不合法 → 回落 0.35；用价格带下沿证明回落生效（不暴露内部字段）。
        ctx = make_ctx(make_products(1, price_cents=2000, cost_cents=1800), margin_floor=1.5)
        outcome = plan_intent(result_for("把这条链接的标题重写一下 SKU-0001"), ctx)
        low, _high = band(outcome.orders[0])
        self.assertGreaterEqual(low, 2769)  # 落在 0.35 毛利线上，而不是 90% 的 1800

    def test_price_points_are_distinct_and_inside_the_band(self):
        outcome = plan_intent(result_for("把这条链接的标题重写一下 SKU-0001"),
                              make_ctx(make_products(1, price_cents=2999, cost_cents=1200)))
        order = outcome.orders[0]
        low, high = band(order)
        points = order["price_points"]
        self.assertGreaterEqual(len(points), 2)
        self.assertEqual(len(set(points)), len(points))
        self.assertTrue(all(low <= point <= high for point in points))

    def test_one_cent_product_still_satisfies_the_contract(self):
        # 极端低价：0.9/1.1 四舍五入后同价，合约会拒绝重复的 price_points。
        outcome = plan_intent(result_for("把这条链接的标题重写一下 SKU-0001"),
                              make_ctx(make_products(1, price_cents=1, cost_cents=None)))
        self.assertEqual(1, len(outcome.orders))
        self.assertEqual([], validate_task_order(outcome.orders[0]))
        self.assertEqual(len(set(outcome.orders[0]["price_points"])),
                         len(outcome.orders[0]["price_points"]))


class IdempotencyTests(unittest.TestCase):
    def test_two_runs_produce_identical_json(self):
        ctx = make_ctx(make_products(12))
        result = result_for("这20个新品上到美国站")
        first = json.dumps(plan_intent(result, ctx).orders, ensure_ascii=False)
        second = json.dumps(plan_intent(result, ctx).orders, ensure_ascii=False)
        self.assertEqual(first, second)

    def test_task_id_is_derived_from_intent_and_sku(self):
        outcome = plan_intent(result_for("把这条链接的标题重写一下 SKU-0003"), make_ctx(make_products(5)))
        self.assertEqual("LISTING_REWRITE:SKU-0003", outcome.orders[0]["task_id"])

    def test_wrapper_matches_plan_intent(self):
        ctx = make_ctx(make_products(5))
        result = result_for("这20个新品上到美国站")
        self.assertEqual(plan_intent(result, ctx).orders, to_task_orders(result, ctx))


class PurityTests(unittest.TestCase):
    """G7：映射层不得产生任何 I/O；它必须是纯函数。"""

    def test_mapping_never_touches_the_filesystem(self):
        texts = [
            "这20个新品上到美国站",
            "把这个产品上架到德国站",
            "把这条链接的标题重写一下 SKU-0007",
            "上周美国站销售额和转化率是多少",
            "今天天气不错",
        ]
        ctx = make_ctx(make_products(6))
        real_open = open
        with mock.patch("builtins.open", side_effect=AssertionError("映射层不该读文件")):
            for text in texts:
                plan_intent(result_for(text), ctx)
        self.assertIs(real_open, open)

    def test_bad_argument_is_a_type_error(self):
        with self.assertRaises(TypeError):
            plan_intent({"intent": "BATCH_UPLOAD"}, make_ctx())


class ArtifactCoverageTests(unittest.TestCase):
    """12 个意图要么能出单，要么给出非空原因——不允许出现"悄悄的空白"。"""

    def test_every_intent_is_handled_explicitly(self):
        ctx = make_ctx(make_products(4))
        for intent in Intent:
            with self.subTest(intent=intent.value):
                result = IntentResult(intent=intent, confidence=0.9, slots={"sku": "SKU-0001"})
                outcome = plan_intent(result, ctx)
                if INTENT_SPECS[intent]["artifact"] in ORDER_ARTIFACTS:
                    self.assertTrue(outcome.ok, f"{intent.value} 应该能出单")
                else:
                    self.assertFalse(outcome.ok)
                    self.assertTrue(outcome.reason, f"{intent.value} 必须说明为什么不出单")

    def test_batch_and_single_upload_declare_task_order_artifacts(self):
        self.assertIn(INTENT_SPECS[Intent.BATCH_UPLOAD]["artifact"], ORDER_ARTIFACTS)
        self.assertIn(INTENT_SPECS[Intent.SINGLE_UPLOAD]["artifact"], ORDER_ARTIFACTS)


class DraftGuardTests(unittest.TestCase):
    """draft 单不得进入内容工坊——拦截点在工坊，不靠调用方自觉。"""

    def _products(self) -> dict:
        from ops_agent.site_platform import SiteProduct, SiteVariant

        variant = SiteVariant(
            variant_id="v1", sku_code="SKU-0001-V1", price_cents=2999,
            cost_cents=1200, stock=50, attrs={},
        )
        return {
            "SKU-0001": SiteProduct(
                product_id="SKU-0001", slug="sku-0001", title_en="Widget 1", status="draft",
                category_id="c1", category_slug="home", moq=100, lead_time_days=15,
                incoterm="FOB", variants=(variant,), metrics=None,
            )
        }

    def test_workshop_refuses_draft_orders(self):
        orders = plan_intent(result_for("把这条链接的标题重写一下 SKU-0001"),
                             make_ctx(make_products(1))).orders
        failures: list[dict] = []
        proposals = build_proposals(orders, self._products(), limit=5, failures=failures)
        self.assertEqual([], proposals)
        self.assertEqual(1, len(failures))
        self.assertIn("draft", failures[0]["reason"])

    def test_ready_orders_still_reach_the_workshop(self):
        """同一批商品，只把 status 改成非 draft，工坊就照常干活——拦截确实只看状态。"""
        orders = plan_intent(result_for("把这条链接的标题重写一下 SKU-0001"),
                             make_ctx(make_products(1))).orders
        for order in orders:
            order["status"] = "ready"
        failures: list[dict] = []
        proposals = build_proposals(orders, self._products(), limit=5, failures=failures)
        self.assertEqual(1, len(proposals))
        self.assertEqual([], failures)


if __name__ == "__main__":
    unittest.main()
