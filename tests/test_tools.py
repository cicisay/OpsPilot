import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.approvals import ApprovalService  # noqa: E402
from ops_agent.catalog import generate_catalog  # noqa: E402
from ops_agent.compliance import ComplianceChecker  # noqa: E402
from ops_agent.platform import MockPlatform  # noqa: E402
from ops_agent.simulator import simulate_days  # noqa: E402
from ops_agent.tools import ToolContext, build_registry  # noqa: E402

EXPECTED_TOOLS = {
    "get_shop_snapshot",
    "get_competitors",
    "get_trends",
    "price_calc",
    "generate_listing",
    "generate_assets",
    "compliance_check",
    "publish_listing",
    "rollback",
}


def make_context():
    skus = generate_catalog(10, seed=1)
    performance = {sku.sku_id: simulate_days(sku, 28, seed=3) for sku in skus}
    platform = MockPlatform(skus, performance)
    context = ToolContext(platform, ApprovalService(), ComplianceChecker())
    return context, build_registry()


class ToolRegistryTests(unittest.TestCase):
    def setUp(self):
        self.context, self.registry = make_context()

    def test_exposes_nine_tools(self):
        names = {spec["name"] for spec in self.registry.specs()}
        self.assertEqual(names, EXPECTED_TOOLS)

    def test_side_effect_labels(self):
        specs = {spec["name"]: spec for spec in self.registry.specs()}
        self.assertEqual(specs["get_shop_snapshot"]["side_effect"], "read")
        self.assertEqual(specs["generate_listing"]["side_effect"], "compute")
        self.assertEqual(specs["publish_listing"]["side_effect"], "write")
        self.assertEqual(specs["rollback"]["side_effect"], "write")

    def test_seasonality_matches_points(self):
        result = self.registry.call(
            "get_trends", {"keyword": "candle", "market": "US"}, self.context
        )
        first, last = result.data["points"][0], result.data["points"][-1]
        if last["index"] > first["index"]:
            self.assertEqual(result.data["seasonality"], "Q4 上升")
        elif last["index"] < first["index"]:
            self.assertEqual(result.data["seasonality"], "Q4 回落")
        else:
            self.assertEqual(result.data["seasonality"], "平稳")

    def test_unknown_tool_returns_error(self):
        result = self.registry.call("delete_everything", {}, self.context)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "unknown_tool")

    def test_missing_argument_is_rejected(self):
        result = self.registry.call("price_calc", {"sku_id": "SKU-1001"}, self.context)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_arguments")

    def test_unknown_argument_is_rejected(self):
        result = self.registry.call(
            "get_shop_snapshot",
            {"days": 7, "hack": True},
            self.context,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_arguments")

    def test_snapshot_returns_own_data(self):
        result = self.registry.call(
            "get_shop_snapshot", {"days": 7, "sku_id": "SKU-1001"}, self.context
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["license"], "own")
        item = result.data["items"][0]
        self.assertGreater(item["impressions"], 0)
        self.assertGreater(item["ctr"], 0)

    def test_competitors_are_licensed_not_scraped(self):
        result = self.registry.call(
            "get_competitors",
            {"category": "home-fragrance", "market": "US"},
            self.context,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["license"], "authorized")
        self.assertEqual(result.data["source"], "authorized_reference")
        self.assertTrue(result.data["items"])

    def test_trends_are_public(self):
        result = self.registry.call(
            "get_trends", {"keyword": "candle", "market": "US"}, self.context
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["license"], "public")
        self.assertEqual(len(result.data["points"]), 6)

    def test_price_calc_matches_margin(self):
        sku = self.context.platform.skus["SKU-1001"]
        result = self.registry.call(
            "price_calc", {"sku_id": "SKU-1001", "price": sku.price}, self.context
        )
        self.assertTrue(result.ok)
        self.assertAlmostEqual(result.data["margin"], sku.margin, places=3)
        self.assertFalse(result.data["below_floor"])

    def test_generate_listing_returns_two_variants(self):
        result = self.registry.call(
            "generate_listing",
            {
                "sku_id": "SKU-1001",
                "market": "US",
                "hypothesis": "scene_image",
                "variants": 2,
            },
            self.context,
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data["variants"]), 2)
        self.assertGreater(result.data["expected_lift"], 0)
        self.assertIn("image_brief", result.data["variants"][0])

    def test_generated_listing_passes_compliance(self):
        listing = self.registry.call(
            "generate_listing",
            {"sku_id": "SKU-1001", "market": "US", "hypothesis": "scene_image"},
            self.context,
        )
        variant = listing.data["variants"][0]
        text = " ".join([variant["title"], *variant["bullets"], variant["description"]])
        result = self.registry.call(
            "compliance_check",
            {
                "text": text,
                "image_brief": variant["image_brief"],
                "category": "home-fragrance",
                "market": "US",
            },
            self.context,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["passed"])

    def test_generate_assets_are_ai_generated(self):
        result = self.registry.call(
            "generate_assets",
            {"sku_id": "SKU-1001", "style": "scene", "count": 3},
            self.context,
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data["assets"]), 4)
        self.assertTrue(all(item["usage_rights"] == "ai_generated" for item in result.data["assets"]))

    def test_publish_requires_token_for_big_price_change(self):
        listing = self.registry.call(
            "generate_listing",
            {"sku_id": "SKU-1001", "market": "US", "hypothesis": "scene_image"},
            self.context,
        )
        variant = dict(listing.data["variants"][0])
        sku = self.context.platform.skus["SKU-1001"]
        variant["price"] = round(sku.price * 0.85, 2)

        blocked = self.registry.call(
            "publish_listing", {"sku_id": "SKU-1001", "variant": variant}, self.context
        )
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.code, "approval_required")

        token = self.context.approvals.issue("SKU-1001", "publish", "test price cut")
        published = self.registry.call(
            "publish_listing",
            {"sku_id": "SKU-1001", "variant": variant, "approval_token": token.token},
            self.context,
        )
        self.assertTrue(published.ok)
        self.assertEqual(published.data["version"], 2)
        self.assertEqual(published.data["approved_by"], "human")

        second = dict(variant)
        second["price"] = round(self.context.platform.current("SKU-1001").price * 0.85, 2)
        reused = self.registry.call(
            "publish_listing",
            {"sku_id": "SKU-1001", "variant": second, "approval_token": token.token},
            self.context,
        )
        self.assertFalse(reused.ok)
        self.assertEqual(reused.code, "invalid_token")

    def test_rollback_restores_previous_version(self):
        sku = self.context.platform.skus["SKU-1001"]
        original_price = sku.price
        listing = self.registry.call(
            "generate_listing",
            {"sku_id": "SKU-1001", "market": "US", "hypothesis": "scene_image"},
            self.context,
        )
        variant = dict(listing.data["variants"][0])
        variant["price"] = round(original_price * 0.9, 2)
        token = self.context.approvals.issue("SKU-1001", "publish", "test")
        self.registry.call(
            "publish_listing",
            {"sku_id": "SKU-1001", "variant": variant, "approval_token": token.token},
            self.context,
        )

        result = self.registry.call("rollback", {"sku_id": "SKU-1001"}, self.context)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["restored_version"], 1)
        self.assertAlmostEqual(result.data["price"], original_price)


if __name__ == "__main__":
    unittest.main()
