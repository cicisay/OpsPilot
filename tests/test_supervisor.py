import json
import math
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.site_platform import SiteMetrics, SiteProduct, SiteVariant, load_snapshot  # noqa: E402
from ops_agent.supervisor import (  # noqa: E402
    build_plan,
    compute_benchmarks,
    diagnose,
    price_band,
    stock_days,
)


def make_product(
    product_id: str,
    pv: int = 1000,
    inquiries: int = 10,
    orders: int = 5,
    units: int = 100,
    reviews: int = 0,
    negative: int = 0,
    rating: float = 4.6,
    price: int = 1290,
    cost: int = 774,
    stock: int = 3000,
    status: str = "active",
    category: str = "audio",
) -> SiteProduct:
    metrics = SiteMetrics(
        days=30,
        pv=pv,
        uv=int(pv * 0.7),
        inquiries=inquiries,
        orders=orders,
        units=units,
        revenue_cents=orders * price,
        reviews=reviews,
        avg_rating=rating,
        negative_reviews=negative,
    )
    variant = SiteVariant(
        variant_id=f"v-{product_id}",
        sku_code=product_id.upper(),
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
        category_slug=category,
        moq=100,
        lead_time_days=15,
        incoterm="FOB",
        variants=(variant,),
        metrics=metrics,
    )


def baseline(count: int = 3) -> list[SiteProduct]:
    return [make_product(f"base{i}") for i in range(count)]


class SupervisorTests(unittest.TestCase):
    def test_low_inquiry_is_diagnosed(self):
        target = make_product("target", inquiries=2)
        plan = build_plan(baseline() + [target], "s", limit=5)
        self.assertEqual(plan.selected[0].problem, "low_inquiry")

    def test_low_traffic_is_diagnosed(self):
        target = make_product("target", pv=400, inquiries=4, orders=2)
        plan = build_plan(baseline() + [target], "s", limit=5)
        self.assertEqual(plan.selected[0].problem, "low_traffic")

    def test_high_negative_is_diagnosed(self):
        target = make_product("target", reviews=10, negative=3, rating=3.5)
        plan = build_plan(baseline() + [target], "s", limit=5)
        self.assertEqual(plan.selected[0].problem, "high_negative")

    def test_stock_risk_is_diagnosed(self):
        target = make_product("target", units=300, stock=100)
        plan = build_plan(baseline() + [target], "s", limit=5)
        self.assertEqual(plan.selected[0].problem, "stock_risk")

    def test_overstock_without_sales_uses_label(self):
        target = make_product("target", pv=900, inquiries=9, orders=4, units=0, stock=5000)
        plan = build_plan(baseline(9) + [target], "s", limit=5)
        overstock = [item for item in plan.selected if item.problem == "overstock"]
        self.assertTrue(overstock)
        self.assertTrue(any("无销量" in evidence for evidence in overstock[0].evidence))

    def test_low_margin_is_diagnosed(self):
        target = make_product("target", price=1000, cost=850)
        plan = build_plan(baseline() + [target], "s", limit=5)
        self.assertEqual(plan.selected[0].problem, "low_margin")

    def test_healthy_product_is_not_selected(self):
        plan = build_plan(baseline(), "s", limit=5)
        self.assertEqual(plan.selected, [])

    def test_plan_respects_limit(self):
        targets = [make_product(f"t{i}", inquiries=1) for i in range(6)]
        plan = build_plan(baseline(9) + targets, "s", limit=5)
        self.assertEqual(len(plan.selected), 5)

    def test_price_band_respects_margin_floor(self):
        target = make_product("target", price=1000, cost=850)
        plan = build_plan(baseline() + [target], "s", limit=5)
        diagnosis = plan.selected[0]
        floor_price = math.ceil(850 / (1 - 0.16))
        self.assertGreaterEqual(diagnosis.price_band_cents[0], floor_price)
        self.assertLessEqual(diagnosis.price_band_cents[0], diagnosis.price_band_cents[1])

    def test_stock_days(self):
        product = make_product("target", units=300, stock=100)
        self.assertAlmostEqual(stock_days(product, 30), 10.0)

    def test_compute_benchmarks_uses_category_median(self):
        by_category, global_benchmark = compute_benchmarks(baseline())
        self.assertIn("audio", by_category)
        self.assertAlmostEqual(global_benchmark.pv, 1000)

    def test_offline_snapshot_round_trip(self):
        product = make_product("p1")
        snapshot = {
            "store": {
                "store_id": "s1",
                "slug": "globaltrade",
                "name": "GlobalTrade",
                "country": "CN",
                "currency": "USD",
                "timezone": "Asia/Shanghai",
            },
            "products": [
                {
                    "product_id": product.product_id,
                    "slug": product.slug,
                    "title_en": product.title_en,
                    "status": product.status,
                    "category_id": product.category_id,
                    "category_slug": product.category_slug,
                    "moq": product.moq,
                    "lead_time_days": product.lead_time_days,
                    "incoterm": product.incoterm,
                    "variants": [
                        {
                            "variant_id": "v-p1",
                            "sku_code": "P1",
                            "price_cents": 1290,
                            "cost_cents": 774,
                            "stock": 3000,
                            "attrs": {},
                        }
                    ],
                    "metrics": {
                        "days": 30,
                        "pv": 1000,
                        "uv": 700,
                        "inquiries": 10,
                        "orders": 5,
                        "units": 100,
                        "revenue_cents": 6450,
                        "reviews": 0,
                        "avg_rating": None,
                        "negative_reviews": 0,
                    },
                }
            ],
        }
        with patch("pathlib.Path.read_text", return_value=json.dumps(snapshot)):
            store, products = load_snapshot(Path("snapshot.json"))
        self.assertEqual(store.slug, "globaltrade")
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].metrics.pv, 1000)


if __name__ == "__main__":
    unittest.main()
