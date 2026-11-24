import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.config import DATA_DIR  # noqa: E402
from ops_agent.console.auth import UserStore  # noqa: E402
from ops_agent.console.connectors import ConnectorError, build_site  # noqa: E402
from ops_agent.console.db import ConsoleDB, SQLiteTenantStore, SQLiteUserStore  # noqa: E402
from ops_agent.console.demo_site import SnapshotSite  # noqa: E402
from ops_agent.console.tenants import Tenant  # noqa: E402
from ops_agent.site_platform import SiteAPIError  # noqa: E402


class ConsoleDBTests(unittest.TestCase):
    def setUp(self):
        self.db = ConsoleDB(":memory:")
        self.users = SQLiteUserStore(self.db, iterations=1000)
        self.tenants = SQLiteTenantStore(self.db, Path("."))

    def tearDown(self):
        self.db.close()

    def test_add_authenticate_and_memberships(self):
        self.users.add("admin", "password-1", "admin", ["a", "b"])
        self.users.add("operator", "password-2", "operator", ["a"])
        self.assertIsNotNone(self.users.authenticate("operator", "password-2"))
        self.assertIsNone(self.users.authenticate("operator", "wrong"))
        self.assertEqual(self.users.get("operator")["tenants"], ["a"])

    def test_disable_blocks_login(self):
        self.users.add("operator", "password-2", "operator", ["a"])
        self.users.update("operator", disabled=True)
        self.assertIsNone(self.users.authenticate("operator", "password-2"))
        self.users.update("operator", disabled=False)
        self.assertIsNotNone(self.users.authenticate("operator", "password-2"))

    def test_last_admin_guard(self):
        self.users.add("admin", "password-1", "admin", ["a"])
        with self.assertRaises(ValueError):
            self.users.update("admin", role="operator")
        with self.assertRaises(ValueError):
            self.users.update("admin", disabled=True)

    def test_set_password(self):
        self.users.add("operator", "password-2", "operator", ["a"])
        self.users.set_password("operator", "password-3")
        self.assertIsNone(self.users.authenticate("operator", "password-2"))
        self.assertIsNotNone(self.users.authenticate("operator", "password-3"))

    def test_tenant_membership_isolation(self):
        self.tenants.add(Tenant("a", "A", "", "", Path("data/a")))
        self.tenants.add(Tenant("b", "B", "", "", Path("data/b")))
        self.users.add("operator", "password-2", "operator", ["a"])
        operator = self.users.get("operator")
        names = [tenant.tenant_id for tenant in self.tenants.for_user(operator)]
        self.assertEqual(names, ["a"])
        admin = {"role": "admin", "tenants": []}
        self.assertEqual([t.tenant_id for t in self.tenants.for_user(admin)], ["a", "b"])

    def test_tenant_platform_default(self):
        self.tenants.add(Tenant("a", "A", "", "", Path("data/a")))
        self.assertEqual(self.tenants.get("a").platform, "selfhosted")

    def test_connector_rejects_unimplemented_platform(self):
        tenant = Tenant("shop", "Shop", "https://shop.example", "TOKEN", Path("data/shop"), platform="shopify")
        with self.assertRaises(ConnectorError):
            build_site(tenant)

    def test_connector_requires_token_for_selfhosted(self):
        tenant = Tenant("a", "A", "http://localhost:3000/api/agent", "MISSING_TOKEN_ENV", Path("data/a"))
        with self.assertRaises(ConnectorError):
            build_site(tenant)

    def test_snapshot_site_is_preview_only(self):
        site = SnapshotSite(DATA_DIR / "site_snapshot.json")
        products = site.all_products()
        self.assertEqual(len(products), 30)
        detail = site.product(products[0].product_id)
        variant_id = detail["variants"][0]["variantId"]
        preview = site.patch_variant(variant_id, price_cents=100, dry_run=True)
        self.assertIn("priceCents", preview["diff"])
        with self.assertRaises(SiteAPIError):
            site.patch_variant(variant_id, price_cents=100, dry_run=False)


if __name__ == "__main__":
    unittest.main()
