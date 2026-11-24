import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.agents.director import (  # noqa: E402
    build_catalogue,
    build_digest,
    new_run_id,
    normalize_selection,
    price_points,
    run_director,
)
from ops_agent.config import LLMSettings  # noqa: E402
from ops_agent.contracts import ContractError  # noqa: E402
from ops_agent.llm import LLMError, LLMReply  # noqa: E402
from ops_agent.memory import MemoryStore  # noqa: E402
from ops_agent.quota import QuotaPolicy  # noqa: E402
from ops_agent.site_platform import (  # noqa: E402
    SiteMetrics,
    SiteProduct,
    SiteStore,
    SiteVariant,
)

SETTINGS = LLMSettings(
    provider="deepseek", base_url="http://model.local/v1", model="test-model", api_key="k"
)

# Six active products, ratio 0.5 => ceil(3) => 3 is the ceiling; floor is 2.
POLICY = QuotaPolicy(ratio=0.5, min_skus=3, max_skus=10, review_cap=24)

STORE = SiteStore(
    store_id="s1", slug="globaltrade", name="Global Trade", country="US", currency="USD", timezone="UTC"
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

    def chat(self, messages, temperature=None, max_tokens=None):
        self.calls += 1
        self.messages.append(messages)
        if not self._replies:
            raise LLMError("no replies left")
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMReply(text=json.dumps(item, ensure_ascii=False), model="test-model")


def make_product(
    product_id: str,
    *,
    price: int = 620,
    cost: int = 400,
    stock: int = 800,
    pv: int = 900,
    orders: int = 0,
    inquiries: int = 9,
    negative: int = 2,
    rating: float = 3.6,
    units: int = 0,
    status: str = "active",
) -> SiteProduct:
    metrics = SiteMetrics(
        days=30,
        pv=pv,
        uv=int(pv * 0.7),
        inquiries=inquiries,
        orders=orders,
        units=units,
        revenue_cents=orders * price,
        reviews=6,
        avg_rating=rating,
        negative_reviews=negative,
    )
    variant = SiteVariant(
        variant_id=f"{product_id}-v1",
        sku_code=f"{product_id}-01",
        price_cents=price,
        cost_cents=cost,
        stock=stock,
        attrs={},
    )
    return SiteProduct(
        product_id=product_id,
        slug=product_id,
        title_en=f"Product {product_id}",
        status=status,
        category_id="c1",
        category_slug="home-kitchen",
        moq=100,
        lead_time_days=18,
        incoterm="FOB",
        variants=(variant,),
        metrics=metrics,
    )


def sku(product_id: str, **overrides) -> dict:
    base = {
        "product_id": product_id,
        "problem": "low_order",
        "problem_label": "下单低",
        "problem_detail": "询盘正常但下单率只有中位数的三分之一",
        "priority": 0.8,
        "evidence": ["下单率 0.00% vs 类目中位 1.20%", "库存 800 件 / 30 天卖出 0 件"],
        "hypothesis": "如果补上认证与交期信息，下单率会提升至少 12%",
        "action": "重写详情页并补充认证与交期",
        "success_metric": "下单率",
        "min_lift": 0.12,
        "window_days": 14,
        "price_low_cents": 580,
        "price_high_cents": 660,
        "price_reason": "维持现有价格带，先验证内容而非价格",
        "variant_min": 2,
        "variant_max": 3,
        "variant_reason": "价格带窄，三版足够",
        "must_include": ["MOQ"],
        "forbidden": ["guaranteed"],
    }
    base.update(overrides)
    return base


def reply(skus: list[dict], count: int | None = None, **health) -> dict:
    return {
        "store_health": {
            "score": health.get("score", 62),
            "summary": "转化集中在少数类目",
            "weak_categories": ["home-kitchen"],
            "top_problem": "下单率低",
        },
        "selected_count": len(skus) if count is None else count,
        "selection_reason": "异常集中且人工可审",
        "skus": skus,
    }


def products() -> list[SiteProduct]:
    return [make_product(f"p{index}") for index in range(1, 7)]


def mixed_week() -> list[dict]:
    """Three picks with three different problems: no FR-503 concentration."""
    return [
        sku("p1", problem="low_order"),
        sku("p2", problem="low_inquiry"),
        sku("p3", problem="overstock"),
    ]


class PricePointTests(unittest.TestCase):
    def test_points_are_evenly_spaced_for_five_variants(self):
        points = price_points(1000, 1400, 5)
        self.assertEqual(points, [1000, 1100, 1200, 1300, 1400])

    def test_zero_width_band_repeats_the_same_price(self):
        self.assertEqual(price_points(999, 999, 3), [999, 999, 999])


class CatalogueTests(unittest.TestCase):
    def test_infinite_stock_cover_becomes_null(self):
        catalogue = build_catalogue([make_product("p1", units=0, stock=50)], days=30)
        self.assertIsNone(catalogue[0]["metrics"]["stock_days"])

    def test_cost_floor_is_reported_to_the_model(self):
        catalogue = build_catalogue([make_product("p1", price=620, cost=400)], days=30)
        self.assertEqual(catalogue[0]["cost_floor_cents"], 477)

    def test_reviews_are_truncated_to_two(self):
        reviews = {"p1": [{"rating": 2, "text": f"bad {i}"} for i in range(5)]}
        catalogue = build_catalogue([make_product("p1")], days=30, reviews=reviews)
        self.assertEqual(len(catalogue[0]["reviews"]), 2)

    def test_digest_carries_quota_and_benchmarks(self):
        digest = build_digest(
            STORE,
            products(),
            days=30,
            reviews={},
            memory_context={},
            policy=POLICY,
        )
        self.assertEqual(digest["catalogue_size"], 6)
        # 6 products at POLICY's 50% ratio: the ceiling is 3 and so is the usual week.
        self.assertEqual((digest["may_select_low"], digest["may_select_high"]), (3, 3))
        self.assertIn("all", digest["benchmarks"])
        self.assertEqual(len(digest["catalogue"]), 6)

    def test_human_limit_override_tightens_the_ceiling(self):
        digest = build_digest(
            STORE, products(), days=30, reviews={}, memory_context={}, policy=POLICY, limit_override=2
        )
        self.assertEqual(digest["may_select_high"], 2)


class NormalizeTests(unittest.TestCase):
    def test_valid_selection_becomes_task_orders(self):
        docs, issues, requested, quota = normalize_selection(
            reply(mixed_week()),
            products_by_id={item.product_id: item for item in products()},
            run_id="run-1",
            policy=POLICY,
            active_count=6,
            margin_floor=0.16,
        )
        self.assertEqual(len(docs), 3)
        self.assertEqual(requested, 3)
        self.assertFalse(quota["guarded"])
        self.assertFalse(quota["underfill"])
        self.assertEqual(docs[0]["task_id"], "run-1:p1")
        self.assertEqual(docs[0]["planner"], "llm")
        self.assertEqual(len(docs[0]["price_points"]), 3)
        self.assertEqual(issues, [])

    def test_smaller_week_without_a_reason_is_reported(self):
        # 50 active products at a 50% ratio: the usual week is 3, so a single
        # pick is an underfill that has to be explained.
        docs, issues, _, quota = normalize_selection(
            reply([sku("p1")]),
            products_by_id={item.product_id: item for item in products()},
            run_id="run-1",
            policy=POLICY,
            active_count=50,
            margin_floor=0.16,
        )
        # The pick survives — the defect is the missing sentence, not the count.
        self.assertEqual(len(docs), 1)
        self.assertTrue(quota["underfill"])
        self.assertTrue(any(item.startswith("only 1 product(s) selected") for item in issues))

    def test_smaller_week_with_a_reason_is_clean(self):
        data = reply([sku("p1")])
        data["underfill_reason"] = "其余商品 30 天零 PV，属于新品期"
        docs, issues, _, quota = normalize_selection(
            data,
            products_by_id={item.product_id: item for item in products()},
            run_id="run-1",
            policy=POLICY,
            active_count=50,
            margin_floor=0.16,
        )
        self.assertEqual(len(docs), 1)
        self.assertTrue(quota["underfill"])
        self.assertEqual(quota["underfill_reason"], "其余商品 30 天零 PV，属于新品期")
        self.assertEqual(issues, [])

    def test_count_above_the_ceiling_is_capped_and_reported(self):
        docs, issues, requested, quota = normalize_selection(
            reply([sku("p1"), sku("p2"), sku("p3"), sku("p4")], count=40),
            products_by_id={item.product_id: item for item in products()},
            run_id="run-1",
            policy=POLICY,
            active_count=6,
            margin_floor=0.16,
        )
        self.assertEqual(len(docs), 3)
        self.assertEqual(requested, 40)
        self.assertTrue(quota["guarded"])
        self.assertTrue(any("adjusted to 3" in item for item in issues))

    def test_unknown_product_is_dropped_not_invented(self):
        docs, issues, _, _ = normalize_selection(
            reply([sku("p1"), sku("ghost")]),
            products_by_id={item.product_id: item for item in products()},
            run_id="run-1",
            policy=POLICY,
            active_count=6,
            margin_floor=0.16,
        )
        self.assertEqual([item["product_id"] for item in docs], ["p1"])
        self.assertTrue(any("unknown product_id" in item for item in issues))

    def test_price_below_cost_floor_is_raised(self):
        docs, issues, _, _ = normalize_selection(
            reply([sku("p1", price_low_cents=1, price_high_cents=660)]),
            products_by_id={item.product_id: item for item in products()},
            run_id="run-1",
            policy=POLICY,
            active_count=6,
            margin_floor=0.16,
        )
        self.assertEqual(docs[0]["price_band_cents"][0], 477)
        self.assertTrue(any("cost floor" in item for item in issues))

    def test_variant_target_drives_price_point_count(self):
        docs, _, _, _ = normalize_selection(
            reply([sku("p1", variant_min=2, variant_max=4)]),
            products_by_id={item.product_id: item for item in products()},
            run_id="run-1",
            policy=POLICY,
            active_count=6,
            margin_floor=0.16,
        )
        self.assertEqual(docs[0]["variant_max"], 4)
        self.assertEqual(len(docs[0]["price_points"]), 4)

    def test_variant_request_above_capacity_is_clamped(self):
        # A cap of four versions across three SKUs leaves two each; the request
        # for five must lose to the review budget, not to the contract.
        policy = QuotaPolicy(ratio=0.5, min_skus=3, max_skus=10, review_cap=4)
        docs, _, _, _ = normalize_selection(
            reply([sku("p1", variant_min=2, variant_max=5), sku("p2"), sku("p3")]),
            products_by_id={item.product_id: item for item in products()},
            run_id="run-1",
            policy=policy,
            active_count=6,
            margin_floor=0.16,
        )
        self.assertEqual(docs[0]["variant_max"], 2)

    def test_missing_hypothesis_is_rejected(self):
        docs, issues, _, _ = normalize_selection(
            reply([sku("p1", hypothesis="")]),
            products_by_id={item.product_id: item for item in products()},
            run_id="run-1",
            policy=POLICY,
            active_count=6,
            margin_floor=0.16,
        )
        self.assertEqual(docs, [])
        self.assertTrue(any("hypothesis" in item for item in issues))

    def test_concentration_is_reported_but_not_enforced_by_dropping(self):
        docs, issues, _, _ = normalize_selection(
            reply([sku("p1"), sku("p2"), sku("p3")]),
            products_by_id={item.product_id: item for item in products()},
            run_id="run-1",
            policy=POLICY,
            active_count=6,
            margin_floor=0.16,
        )
        self.assertEqual(len(docs), 3)
        self.assertTrue(any("concentration" in item for item in issues))


class RunDirectorTests(unittest.TestCase):
    def setUp(self):
        self.products = products()
        self.by_id = {item.product_id: item for item in self.products}

    def test_one_call_builds_a_plan(self):
        client = FakeClient([reply([sku("p1"), sku("p2")])])
        result = run_director(
            store=STORE,
            products=self.products,
            client=client,
            policy=POLICY,
            run_id="run-1",
            limit_override=2,
        )
        self.assertEqual(len(result.selected), 2)
        self.assertEqual(result.store_health["score"], 62)
        self.assertEqual(result.requested_count, 2)
        self.assertEqual(client.calls, 1)

    def test_invalid_first_reply_triggers_exactly_one_repair(self):
        broken = reply([sku("p1", hypothesis=""), sku("p2")])
        fixed = reply([sku("p1"), sku("p2")])
        client = FakeClient([broken, fixed])
        result = run_director(
            store=STORE,
            products=self.products,
            client=client,
            policy=POLICY,
            run_id="run-1",
            limit_override=2,
        )
        self.assertEqual(len(result.selected), 2)
        self.assertEqual(result.repairs, 1)
        self.assertEqual(client.calls, 2)

    def test_repair_failure_aborts_instead_of_falling_back_to_rules(self):
        client = FakeClient([reply([sku("p1", hypothesis="")]), LLMError("boom")])
        with self.assertRaises(ContractError):
            run_director(
                store=STORE,
                products=self.products,
                client=client,
                policy=POLICY,
                run_id="run-1",
            )

    def test_unconfigured_model_raises(self):
        client = FakeClient([])
        client.settings = LLMSettings(provider="deepseek", base_url="", model="", api_key="")
        with self.assertRaises(LLMError):
            run_director(
                store=STORE,
                products=self.products,
                client=client,
                policy=POLICY,
                run_id="run-1",
            )

    def test_memory_is_written_once_and_replayable(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(tmp)
            client = FakeClient(
                [
                    reply(mixed_week()),
                    reply(mixed_week()),
                ]
            )
            run_director(
                store=STORE,
                products=self.products,
                client=client,
                memory=memory,
                policy=POLICY,
                run_id="run-fixed",
            )
            run_director(
                store=STORE,
                products=self.products,
                client=client,
                memory=memory,
                policy=POLICY,
                run_id="run-fixed",
            )
            health = memory.health()
            self.assertEqual(health["decisions"], 1)
            self.assertEqual(health["experiments"], 0)
            self.assertTrue(health["profile"])
            self.assertTrue((Path(tmp) / "snapshots" / "run-fixed.json").exists())
            profile = memory.read_profile()
            self.assertEqual(profile["active_products"], 6)
            self.assertEqual(profile["last_selected_count"], 3)

    def test_run_id_is_sortable_and_unique(self):
        first, second = new_run_id(), new_run_id()
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^\d{8}-\d{6}-[0-9a-f]{4}$")


if __name__ == "__main__":
    unittest.main()
