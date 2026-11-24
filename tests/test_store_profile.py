import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.store_metrics import MARGIN_FLOOR  # noqa: E402
from ops_agent.store_profile import PROFILE_FILE, load_store_profile  # noqa: E402

KEYS = ("OPS_QUOTA_RATIO", "OPS_QUOTA_MIN_SKUS", "OPS_QUOTA_MAX_SKUS", "OPS_REVIEW_CAP", "OPS_VARIANT_MIN")


class StoreProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = SimpleNamespace(slug="globaltrade")
        # A developer's own .env must not decide what these tests assert.
        saved = {key: os.environ.pop(key, None) for key in KEYS}
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in KEYS])
        self.addCleanup(lambda: [os.environ.__setitem__(k, v) for k, v in saved.items() if v])

    def write(self, payload: dict) -> None:
        (self.dir / PROFILE_FILE).write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_file_falls_back_to_env(self):
        profile = load_store_profile(self.store, data_dir=self.dir)
        self.assertFalse(profile.from_file)
        self.assertEqual(profile.source, "env/default")
        self.assertEqual(profile.margin_floor, MARGIN_FLOOR)
        self.assertEqual(profile.window_max_days, 56)

    def test_file_overrides_quota_and_business_numbers(self):
        self.write(
            {
                "store": "globaltrade",
                "quota": {"ratio": 0.2, "min_skus": 2, "max_skus": 30, "review_cap": 40, "variant_min": 3},
                "pricing": {"margin_floor": 0.08, "band_floor_ratio": 0.4, "band_ceil_ratio": 4.0},
                "experiment": {"window_max_days": 90},
            }
        )
        profile = load_store_profile(self.store, data_dir=self.dir)
        self.assertTrue(profile.from_file)
        self.assertEqual(profile.quota.ratio, 0.2)
        self.assertEqual(profile.quota.variant_min, 3)
        self.assertEqual(profile.quota.variant_bounds(2)[0], 3)
        # 40 review slots at 3 variants each = 13 products, not 20.
        self.assertEqual(profile.quota.sku_bounds(100), (2, 13))
        self.assertEqual(profile.margin_floor, 0.08)
        self.assertEqual(profile.window_max_days, 90)
        self.assertEqual((profile.band_floor_ratio, profile.band_ceil_ratio), (0.4, 4.0))
        self.assertEqual(profile.notes, ())

    def test_out_of_range_values_fall_back_and_say_so(self):
        self.write(
            {
                "quota": {"ratio": 3.0, "min_skus": 0, "max_skus": "many"},
                "pricing": {"margin_floor": 1.5},
                "experiment": {"window_max_days": 4000},
            }
        )
        profile = load_store_profile(self.store, data_dir=self.dir)
        self.assertEqual(profile.quota.ratio, 0.10)
        self.assertEqual(profile.quota.min_skus, 3)
        self.assertEqual(profile.margin_floor, MARGIN_FLOOR)
        self.assertEqual(profile.window_max_days, 56)
        joined = " ".join(profile.notes)
        self.assertIn("ratio", joined)
        self.assertIn("min_skus", joined)
        self.assertIn("max_skus", joined)
        self.assertIn("window_max_days", joined)

    def test_inverted_floor_and_ceil_are_swapped_not_ignored(self):
        self.write({"quota": {"min_skus": 9, "max_skus": 4}})
        profile = load_store_profile(self.store, data_dir=self.dir)
        self.assertEqual((profile.quota.min_skus, profile.quota.max_skus), (4, 9))
        self.assertIn("对调", " ".join(profile.notes))

    def test_inverted_price_band_returns_to_the_default(self):
        # Each value is inside its own range, but the pair is impossible: a band
        # that cannot go anywhere is a typo, not a strategy.
        self.write({"pricing": {"band_floor_ratio": 1.0, "band_ceil_ratio": 1.0}})
        profile = load_store_profile(self.store, data_dir=self.dir)
        self.assertEqual((profile.band_floor_ratio, profile.band_ceil_ratio), (0.5, 3.0))
        self.assertTrue(any("pricing" in note for note in profile.notes))

    def test_unknown_key_is_reported_not_silently_kept(self):
        self.write({"quota": {"ratio": 0.12, "min_sku": 2}})
        profile = load_store_profile(self.store, data_dir=self.dir)
        self.assertEqual(profile.quota.ratio, 0.12)
        self.assertIn("min_sku", " ".join(profile.notes))

    def test_a_profile_copied_from_another_store_is_flagged(self):
        self.write({"store": "other-shop", "quota": {"max_skus": 8}})
        profile = load_store_profile(self.store, data_dir=self.dir)
        self.assertIn("other-shop", " ".join(profile.notes))
        # Still applied: a warning is not permission to ignore the operator.
        self.assertEqual(profile.quota.max_skus, 8)

    def test_broken_json_is_a_default_not_a_crash(self):
        (self.dir / PROFILE_FILE).write_text("{not json", encoding="utf-8")
        profile = load_store_profile(self.store, data_dir=self.dir)
        self.assertFalse(profile.from_file)
        self.assertTrue(profile.notes)

    def test_describe_names_the_source(self):
        self.write({"quota": {"max_skus": 8}})
        profile = load_store_profile(self.store, data_dir=self.dir)
        self.assertIn("profile:", profile.describe())
        self.assertIn("margin", profile.describe())


if __name__ == "__main__":
    unittest.main()
