import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.catalog import generate_catalog  # noqa: E402
from ops_agent.models import Market, SKU  # noqa: E402
from ops_agent.simulator import (  # noqa: E402
    apply_change,
    category_benchmarks,
    expected_ctr,
    expected_cvr,
    expected_return_rate,
    opportunity_score,
    simulate_days,
)


def make_sku(**overrides):
    data = dict(
        sku_id="SKU-9001",
        title="Test Product",
        category="home-fragrance",
        market=Market.US,
        cost=10.0,
        price=20.0,
        stock_days=30,
        image_score=0.5,
        title_score=0.5,
        detail_score=0.5,
        review_score=0.5,
        shipping_days=7,
        created_at=date(2026, 1, 1),
    )
    data.update(overrides)
    return SKU(**data)


class SimulatorTests(unittest.TestCase):
    def test_expected_rates_are_in_range(self):
        for sku in generate_catalog(50, seed=42):
            self.assertGreater(expected_ctr(sku), 0)
            self.assertLess(expected_ctr(sku), 0.25)
            self.assertGreater(expected_cvr(sku), 0)
            self.assertLess(expected_cvr(sku), 0.20)
            self.assertGreaterEqual(expected_return_rate(sku), 0.02)
            self.assertLessEqual(expected_return_rate(sku), 0.18)

    def test_scene_image_increases_expected_ctr(self):
        sku = make_sku(image_score=0.4)
        improved = apply_change(sku, "scene_image")
        self.assertGreater(expected_ctr(improved), expected_ctr(sku))

    def test_price_cut_increases_cvr_but_reduces_margin(self):
        sku = make_sku()
        cheaper = apply_change(sku, "price_cut", 0.08)
        self.assertGreater(expected_cvr(cheaper), expected_cvr(sku))
        self.assertLess(cheaper.margin, sku.margin)

    def test_price_cut_below_cost_is_rejected(self):
        sku = make_sku(cost=19.0, price=20.0)
        with self.assertRaises(ValueError):
            apply_change(sku, "price_cut", 0.10)

    def test_unknown_change_is_rejected(self):
        with self.assertRaises(ValueError):
            apply_change(make_sku(), "change_the_world")

    def test_simulation_produces_valid_rows(self):
        rows = simulate_days(make_sku(), days=28, seed=3)
        self.assertEqual(len(rows), 28)
        for row in rows:
            self.assertLessEqual(row.clicks, row.impressions)
            self.assertLessEqual(row.orders, row.clicks)
            self.assertLessEqual(row.returns, row.orders)
        self.assertGreater(sum(row.impressions for row in rows), 0)

    def test_opportunity_score_is_non_negative(self):
        skus = generate_catalog(50, seed=42)
        benchmarks = category_benchmarks(skus)
        for sku in skus:
            self.assertGreaterEqual(opportunity_score(sku, benchmarks), 0)

    def test_some_skus_fall_below_benchmark(self):
        skus = generate_catalog(50, seed=42)
        benchmarks = category_benchmarks(skus)
        self.assertTrue(any(opportunity_score(sku, benchmarks) > 0 for sku in skus))


if __name__ == "__main__":
    unittest.main()
