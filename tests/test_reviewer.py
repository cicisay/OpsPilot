import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.reviewer import ComplianceReviewer  # noqa: E402
from ops_agent.site_platform import SiteMetrics, SiteProduct, SiteVariant  # noqa: E402


def make_product(price: int = 620, cost: int = 400) -> SiteProduct:
    variant = SiteVariant(
        variant_id="v1",
        sku_code="P1-01",
        price_cents=price,
        cost_cents=cost,
        stock=1000,
        attrs={},
    )
    metrics = SiteMetrics(30, 1000, 700, 5, 2, 60, 1200, 0, None, 0)
    return SiteProduct(
        product_id="p1",
        slug="p1",
        title_en="Ceramic Mug",
        status="active",
        category_id="c1",
        category_slug="home-kitchen",
        moq=100,
        lead_time_days=15,
        incoterm="FOB",
        variants=(variant,),
        metrics=metrics,
    )


def make_variant(**overrides) -> dict:
    data = {
        "variant_key": "p1-tier",
        "angle": "阶梯价",
        "title": "Ceramic Mug — Bulk Pricing Available",
        "bullets": ["Tier pricing from 100 pcs", "Free sample for qualified buyers"],
        "description": "Built for wholesale buyers. QC documents available on request.",
        "price_cents": 620,
        "image_brief": "Scene for bulk pricing, product in use, no visible logos",
    }
    data.update(overrides)
    return data


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        self.reviewer = ComplianceReviewer()
        self.product = make_product()

    def test_compliant_variant_passes(self):
        decision = self.reviewer.review(make_variant(), self.product)
        self.assertTrue(decision.passed, decision.violations)

    def test_absolute_term_blocks(self):
        decision = self.reviewer.review(
            make_variant(title="100% guaranteed ceramic mug"), self.product
        )
        self.assertFalse(decision.passed)
        self.assertTrue(any("100%" in item for item in decision.violations))

    def test_price_below_floor_blocks(self):
        decision = self.reviewer.review(make_variant(price_cents=450), self.product)
        self.assertFalse(decision.passed)
        self.assertTrue(any("毛利底线" in item for item in decision.violations))

    def test_long_title_blocks(self):
        decision = self.reviewer.review(make_variant(title="x" * 301), self.product)
        self.assertFalse(decision.passed)
        self.assertTrue(any("标题长度" in item for item in decision.violations))

    def test_empty_bullets_blocks(self):
        decision = self.reviewer.review(make_variant(bullets=[]), self.product)
        self.assertFalse(decision.passed)
        self.assertTrue(any("卖点数量" in item for item in decision.violations))

    def test_zero_price_blocks(self):
        decision = self.reviewer.review(make_variant(price_cents=0), self.product)
        self.assertFalse(decision.passed)
        self.assertTrue(any("价格必须大于 0" in item for item in decision.violations))

    def test_market_specific_term_blocks(self):
        decision = self.reviewer.review(
            make_variant(title="Klimaneutral ceramic mug"), self.product, market="DE"
        )
        self.assertFalse(decision.passed)
        self.assertTrue(any("气候中性" in item for item in decision.violations))


if __name__ == "__main__":
    unittest.main()
