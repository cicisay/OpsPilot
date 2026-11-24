import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.quota import MAX_VARIANTS, QuotaPolicy  # noqa: E402


class SkuBoundsTests(unittest.TestCase):
    def setUp(self):
        self.policy = QuotaPolicy()

    def test_empty_store_allows_nothing(self):
        self.assertEqual(self.policy.sku_bounds(0), (0, 0))

    def test_tiny_store_is_limited_by_catalogue(self):
        # The floor follows the weekly share down: a 3-product shop's usual week
        # is one, because three is its whole catalogue.
        self.assertEqual(self.policy.sku_bounds(3), (1, 3))
        self.assertEqual(self.policy.sku_bounds(1), (1, 1))

    def test_small_store_gets_a_floor_of_three(self):
        # 2 of 20 = the 10% share, so the usual week is below min_skus until the
        # catalogue is big enough to carry it. No "active < 20" branch needed.
        self.assertEqual(self.policy.sku_bounds(20), (2, 3))
        self.assertEqual(self.policy.sku_bounds(50), (3, 5))

    def test_mid_size_store_scales_with_catalogue(self):
        self.assertEqual(self.policy.sku_bounds(120), (3, 12))

    def test_large_store_is_capped_by_review_capacity(self):
        low, high = self.policy.sku_bounds(500)
        self.assertEqual(low, 3)
        self.assertEqual(high, 12)
        self.assertLessEqual(high * 2, self.policy.review_cap)

    def test_tight_review_cap_lowers_the_ceiling(self):
        policy = QuotaPolicy(review_cap=4)
        self.assertEqual(policy.sku_bounds(50), (2, 2))


class ClampTests(unittest.TestCase):
    def setUp(self):
        self.policy = QuotaPolicy()

    def test_requested_inside_bounds_is_untouched(self):
        decision = self.policy.clamp_skus(4, 50)
        self.assertEqual(decision.value, 4)
        self.assertFalse(decision.guarded)

    def test_requested_above_bounds_is_capped(self):
        decision = self.policy.clamp_skus(12, 50)
        self.assertEqual(decision.value, 5)
        self.assertTrue(decision.guarded)
        self.assertEqual(decision.requested, 12)
        self.assertIn("capped", decision.reason)

    def test_requested_below_bounds_is_accepted_not_raised(self):
        # A quiet week is a legitimate finding. Raising it would force the model
        # to pad the list, so the number is kept and only explained.
        decision = self.policy.clamp_skus(1, 50)
        self.assertEqual(decision.value, 1)
        self.assertFalse(decision.guarded)
        self.assertEqual(decision.low, 3)
        self.assertIn("below usual floor", decision.reason)

    def test_missing_request_is_not_turned_into_work(self):
        decision = self.policy.clamp_skus(0, 50)
        self.assertEqual(decision.value, 0)
        self.assertFalse(decision.guarded)
        self.assertEqual(decision.reason, "no selection")

    def test_decision_serialises_for_the_console(self):
        payload = self.policy.clamp_skus(12, 50).as_dict()
        self.assertEqual(
            set(payload),
            {"value", "low", "high", "requested", "guarded", "reason"},
        )


class VariantBoundsTests(unittest.TestCase):
    def setUp(self):
        self.policy = QuotaPolicy()

    def test_roomy_budget_allows_the_maximum(self):
        self.assertEqual(self.policy.variant_bounds(3), (2, MAX_VARIANTS))

    def test_budget_per_sku_shrinks_on_large_batches(self):
        self.assertEqual(self.policy.variant_bounds(5), (2, 4))
        self.assertEqual(self.policy.variant_bounds(12), (2, 2))

    def test_zero_skus_falls_back_to_the_minimum(self):
        self.assertEqual(self.policy.variant_bounds(0), (2, 2))

    def test_requested_variants_are_clamped(self):
        self.assertEqual(self.policy.clamp_variants(5, 12).value, 2)
        self.assertEqual(self.policy.clamp_variants(1, 5).value, 2)
        self.assertEqual(self.policy.clamp_variants(3, 5).value, 3)

    def test_total_versions_is_sku_times_variants(self):
        self.assertEqual(self.policy.total_versions(5, 4), 20)


class EnvTests(unittest.TestCase):
    def test_policy_reads_environment_overrides(self):
        keys = ("OPS_QUOTA_RATIO", "OPS_QUOTA_MIN_SKUS", "OPS_QUOTA_MAX_SKUS", "OPS_REVIEW_CAP")
        saved = {key: os.environ.get(key) for key in keys}
        try:
            os.environ.update(
                {
                    "OPS_QUOTA_RATIO": "0.2",
                    "OPS_QUOTA_MIN_SKUS": "2",
                    "OPS_QUOTA_MAX_SKUS": "8",
                    "OPS_REVIEW_CAP": "10",
                }
            )
            policy = QuotaPolicy.from_env()
            self.assertEqual(policy.ratio, 0.2)
            self.assertEqual(policy.min_skus, 2)
            self.assertEqual(policy.max_skus, 8)
            self.assertEqual(policy.review_cap, 10)
            self.assertEqual(policy.sku_bounds(100), (2, 5))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_nonsense_ratio_falls_back_to_default(self):
        saved = os.environ.get("OPS_QUOTA_RATIO")
        try:
            os.environ["OPS_QUOTA_RATIO"] = "abc"
            self.assertEqual(QuotaPolicy.from_env().ratio, 0.10)
        finally:
            if saved is None:
                os.environ.pop("OPS_QUOTA_RATIO", None)
            else:
                os.environ["OPS_QUOTA_RATIO"] = saved


if __name__ == "__main__":
    unittest.main()
