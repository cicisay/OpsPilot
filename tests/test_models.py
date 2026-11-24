import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.models import (  # noqa: E402
    Market,
    Performance,
    ReviewResult,
    SKU,
    Verdict,
    jsonable,
)


def make_sku(**overrides):
    data = dict(
        sku_id="SKU-1",
        title="Test Product",
        category="home-fragrance",
        market=Market.US,
        cost=10.0,
        price=20.0,
        stock_days=30,
        image_score=0.6,
        title_score=0.6,
        detail_score=0.6,
        review_score=0.6,
        shipping_days=7,
        created_at=date(2026, 1, 1),
    )
    data.update(overrides)
    return SKU(**data)


class ModelTests(unittest.TestCase):
    def test_margin(self):
        self.assertAlmostEqual(make_sku().margin, 0.5)

    def test_price_below_cost_is_rejected(self):
        with self.assertRaises(ValueError):
            make_sku(price=5.0)

    def test_score_out_of_range_is_rejected(self):
        with self.assertRaises(ValueError):
            make_sku(image_score=1.2)

    def test_performance_ratios(self):
        row = Performance("SKU-1", date(2026, 10, 1), 1000, 40, 2, 1)
        self.assertAlmostEqual(row.ctr, 0.04)
        self.assertAlmostEqual(row.cvr, 0.05)
        self.assertAlmostEqual(row.return_rate, 0.5)

    def test_performance_rejects_impossible_counts(self):
        with self.assertRaises(ValueError):
            Performance("SKU-1", date(2026, 10, 1), 100, 101, 0, 0)

    def test_failed_review_requires_violation(self):
        with self.assertRaises(ValueError):
            ReviewResult("v1", Verdict.FAIL, [], [])

    def test_jsonable_handles_enum_and_date(self):
        payload = jsonable(make_sku())
        self.assertEqual(payload["market"], "US")
        self.assertEqual(payload["created_at"], "2026-01-01")


if __name__ == "__main__":
    unittest.main()
