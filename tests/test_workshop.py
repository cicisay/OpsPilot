import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.site_platform import SiteMetrics, SiteProduct, SiteVariant  # noqa: E402
from ops_agent.workshop import build_proposals, proposals_to_dict  # noqa: E402


def make_product(product_id: str = "p1", price: int = 600, cost: int = 400) -> SiteProduct:
    metrics = SiteMetrics(
        days=30,
        pv=1000,
        uv=700,
        inquiries=5,
        orders=2,
        units=60,
        revenue_cents=1200,
        reviews=0,
        avg_rating=None,
        negative_reviews=0,
    )
    variant = SiteVariant(
        variant_id="v1",
        sku_code="P1-01",
        price_cents=price,
        cost_cents=cost,
        stock=1000,
        attrs={},
    )
    return SiteProduct(
        product_id=product_id,
        slug=product_id,
        title_en="Ceramic Mug Set",
        status="active",
        category_id="c1",
        category_slug="home-kitchen",
        moq=100,
        lead_time_days=15,
        incoterm="FOB",
        variants=(variant,),
        metrics=metrics,
    )


def make_task(problem: str = "low_order", band=(500, 700), margin_floor: float = 0.16) -> dict:
    return {
        "product_id": "p1",
        "title": "Ceramic Mug Set",
        "problem": problem,
        "price_band_cents": list(band),
        "margin_floor": margin_floor,
        "budget_tokens": 60000,
    }


class WorkshopTests(unittest.TestCase):
    def test_generates_three_variants_for_low_order(self):
        proposals = build_proposals([make_task()], {"p1": make_product()})
        self.assertEqual(len(proposals), 1)
        self.assertEqual(len(proposals[0].variants), 3)

    def test_all_variants_pass_compliance(self):
        proposals = build_proposals([make_task()], {"p1": make_product()})
        for variant in proposals[0].variants:
            self.assertTrue(variant.compliance_passed, variant.violations)

    def test_prices_stay_in_band_and_above_floor(self):
        proposals = build_proposals(
            [make_task(band=(500, 700), margin_floor=0.16)],
            {"p1": make_product(cost=500)},
        )
        floor = 596  # ceil(500 / 0.84)
        for variant in proposals[0].variants:
            self.assertGreaterEqual(variant.price_cents, floor)
            self.assertLessEqual(variant.price_cents, 700)
            self.assertGreaterEqual(variant.price_cents, 500)

    def test_low_order_has_tier_pricing_angle(self):
        proposals = build_proposals([make_task("low_order")], {"p1": make_product()})
        bullets = [bullet for variant in proposals[0].variants for bullet in variant.bullets]
        self.assertTrue(any("Tier pricing" in bullet for bullet in bullets))

    def test_overstock_has_two_variants(self):
        proposals = build_proposals([make_task("overstock")], {"p1": make_product()})
        self.assertEqual(len(proposals[0].variants), 2)

    def test_missing_product_is_skipped(self):
        proposals = build_proposals([make_task()], {})
        self.assertEqual(proposals, [])

    def test_preview_has_before_and_after(self):
        details = {"p1": {"i18n": {"En": {"title": "Old Title", "bullets": ["old bullet"]}}}}
        proposals = build_proposals([make_task()], {"p1": make_product()}, details)
        preview = proposals[0].variants[0].preview
        self.assertEqual(preview["title"]["before"], "Old Title")
        self.assertIn("Old Title", preview["title"]["after"])
        self.assertEqual(preview["bulletsEn"]["before"], ["old bullet"])
        self.assertEqual(preview["priceCents"]["before"], 600)

    def test_expected_lift_is_positive(self):
        proposals = build_proposals([make_task()], {"p1": make_product()})
        self.assertTrue(all(variant.expected_lift > 0 for variant in proposals[0].variants))

    def test_payload_serializes(self):
        proposals = build_proposals([make_task()], {"p1": make_product()})
        payload = proposals_to_dict("globaltrade", proposals)
        self.assertEqual(payload["store"], "globaltrade")
        self.assertEqual(len(payload["proposals"]), 1)
        self.assertEqual(len(payload["proposals"][0]["variants"]), 3)
        self.assertEqual(payload["proposals"][0]["target_variant_id"], "v1")

    def test_reversed_band_is_sorted(self):
        proposals = build_proposals(
            [make_task(band=(700, 500))],
            {"p1": make_product(cost=400)},
        )
        for variant in proposals[0].variants:
            self.assertGreaterEqual(variant.price_cents, 500)
            self.assertLessEqual(variant.price_cents, 700)

    def test_invalid_margin_floor_does_not_crash(self):
        proposals = build_proposals(
            [make_task(margin_floor=1.0)],
            {"p1": make_product(cost=400)},
        )
        self.assertEqual(len(proposals[0].variants), 3)


if __name__ == "__main__":
    unittest.main()
