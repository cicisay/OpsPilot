import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.catalog import CATEGORY_PROFILES, MARKETS, generate_catalog  # noqa: E402
from ops_agent.models import jsonable  # noqa: E402


class CatalogTests(unittest.TestCase):
    def test_generates_requested_count(self):
        skus = generate_catalog(50, seed=42)
        self.assertEqual(len(skus), 50)
        self.assertEqual(len({sku.sku_id for sku in skus}), 50)

    def test_covers_all_categories_and_markets(self):
        skus = generate_catalog(50, seed=42)
        self.assertEqual({sku.category for sku in skus}, set(CATEGORY_PROFILES))
        self.assertEqual({sku.market for sku in skus}, set(MARKETS))

    def test_is_deterministic_for_same_seed(self):
        first = [jsonable(sku) for sku in generate_catalog(50, seed=7)]
        second = [jsonable(sku) for sku in generate_catalog(50, seed=7)]
        self.assertEqual(first, second)

    def test_different_seed_changes_data(self):
        first = [jsonable(sku) for sku in generate_catalog(50, seed=7)]
        second = [jsonable(sku) for sku in generate_catalog(50, seed=8)]
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
