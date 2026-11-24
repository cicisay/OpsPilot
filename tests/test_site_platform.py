import json
import unittest
import urllib.error
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.site_platform import (  # noqa: E402
    SiteAPIError,
    SitePlatform,
    SiteProduct,
)


def response(payload: dict, status: int = 200) -> tuple[int, dict, bytes]:
    return status, {}, json.dumps(payload).encode("utf-8")


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[SimpleNamespace] = []

    def __call__(self, method: str, url: str, headers: dict, data: bytes | None):
        body = json.loads(data.decode("utf-8")) if data else None
        self.calls.append(SimpleNamespace(method=method, url=url, headers=headers, body=body))
        return self.handler(method, url, headers, data)


def product_payload(product_id: str = "p1") -> dict:
    return {
        "productId": product_id,
        "slug": "anc-wireless-earbuds",
        "titleEn": "ANC Wireless Earbuds",
        "status": "active",
        "categoryId": "c1",
        "categorySlug": "audio",
        "moq": 100,
        "leadTimeDays": 15,
        "incoterm": "FOB",
        "variants": [
            {
                "variantId": "v1",
                "skuCode": "ANC-01",
                "priceCents": 1290,
                "costCents": 774,
                "stock": 4200,
                "attrs": {"color": "Black"},
            }
        ],
        "metrics": {
            "days": 30,
            "pv": 1000,
            "uv": 700,
            "inquiries": 5,
            "orders": 10,
            "units": 120,
            "revenueCents": 154800,
            "reviews": 4,
            "avgRating": 4.5,
            "negativeReviews": 1,
        },
    }


class SitePlatformTests(unittest.TestCase):
    def make_site(self, handler) -> tuple[SitePlatform, FakeTransport]:
        transport = FakeTransport(handler)
        site = SitePlatform("http://example.test/api/agent", "token", transport=transport)
        return site, transport

    def test_health_parses_capabilities(self):
        site, _ = self.make_site(
            lambda method, url, headers, data: response(
                {
                    "ok": True,
                    "version": "1.1.0",
                    "apiVersion": "1.1.0",
                    "connectorVersion": "1.1.0",
                    "productCount": 30,
                    "variantCount": 46,
                    "capabilities": ["read", "write", "dryRun"],
                    "dbWritable": True,
                }
            )
        )
        health = site.health()
        self.assertEqual(health["capabilities"], ["read", "write", "dryRun"])
        self.assertTrue(health["dbWritable"])

    def test_store_parses_fields(self):
        site, _ = self.make_site(
            lambda method, url, headers, data: response(
                {
                    "ok": True,
                    "store": {
                        "storeId": "s1",
                        "slug": "globaltrade",
                        "name": "GlobalTrade",
                        "country": "CN",
                        "currency": "USD",
                        "timezone": "Asia/Shanghai",
                    },
                }
            )
        )
        store = site.store()
        self.assertEqual(store.slug, "globaltrade")
        self.assertEqual(store.currency, "USD")

    def test_products_parse_variants_and_metrics(self):
        site, _ = self.make_site(
            lambda method, url, headers, data: response(
                {"ok": True, "total": 1, "items": [product_payload()]}
            )
        )
        products = site.products()
        self.assertEqual(len(products), 1)
        product = products[0]
        self.assertIsInstance(product, SiteProduct)
        self.assertAlmostEqual(product.variants[0].margin, (1290 - 774) / 1290)
        self.assertEqual(product.metrics.inquiries, 5)
        self.assertAlmostEqual(product.metrics.inquiry_rate, 0.005)

    def test_metrics_parses_items(self):
        site, _ = self.make_site(
            lambda method, url, headers, data: response(
                {"ok": True, "days": 7, "items": [{"productId": "p1", "pv": 100, "orders": 3}]}
            )
        )
        items = site.metrics(days=7)
        self.assertEqual(items[0]["pv"], 100)

    def test_patch_variant_dry_run_sends_query_and_header(self):
        site, transport = self.make_site(
            lambda method, url, headers, data: response({"ok": True, "dryRun": True, "diff": {}})
        )
        result = site.patch_variant(
            "v1",
            price_cents=1280,
            reason="test",
            idempotency_key="key-123",
            dry_run=True,
        )
        self.assertTrue(result["dryRun"])
        call = transport.calls[-1]
        parsed = urlparse(call.url)
        self.assertEqual(parsed.path, "/api/agent/variants/v1")
        self.assertEqual(parse_qs(parsed.query).get("dryRun"), ["true"])
        self.assertEqual(call.headers["Idempotency-Key"], "key-123")
        self.assertEqual(call.body, {"priceCents": 1280, "reason": "test"})

    def test_patch_variant_requires_a_change(self):
        site, _ = self.make_site(lambda *args: response({"ok": True}))
        with self.assertRaises(ValueError):
            site.patch_variant("v1")

    def test_api_error_maps_status_and_code(self):
        site, _ = self.make_site(
            lambda method, url, headers, data: response(
                {"message": "unauthorized", "code": "unauthorized"}, status=401
            )
        )
        with self.assertRaises(SiteAPIError) as ctx:
            site.health()
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.code, "unauthorized")

    def test_rollback_sends_version(self):
        site, transport = self.make_site(
            lambda method, url, headers, data: response({"ok": True, "restoredFrom": "v1"})
        )
        site.rollback("p1", "v1", reason="test")
        call = transport.calls[-1]
        self.assertEqual(call.method, "POST")
        self.assertEqual(call.body, {"versionId": "v1", "reason": "test"})

    def test_all_products_paginates_until_short_page(self):
        def handler(method, url, headers, data):
            query = parse_qs(urlparse(url).query)
            offset = int(query.get("offset", ["0"])[0])
            items = [product_payload(f"p{i}") for i in range(100 if offset == 0 else 1)]
            return response({"ok": True, "total": 101, "items": items})

        site, _ = self.make_site(handler)
        products = site.all_products()
        self.assertEqual(len(products), 101)

    def test_network_error_is_wrapped(self):
        def handler(method, url, headers, data):
            raise urllib.error.URLError("connection refused")

        site, _ = self.make_site(handler)
        with self.assertRaises(SiteAPIError) as ctx:
            site.health()
        self.assertEqual(ctx.exception.status, 0)
        self.assertEqual(ctx.exception.code, "network_error")

    def test_path_traversal_is_rejected(self):
        site, _ = self.make_site(lambda *args: response({"ok": True}))
        with self.assertRaises(ValueError):
            site.product("../health")

    def test_non_http_base_url_is_rejected(self):
        with self.assertRaises(ValueError):
            SitePlatform("file:///tmp/store", "token")

    def test_token_provider_rotates_per_request(self):
        counter = {"n": 0}

        def provider() -> str:
            value = f"token-{counter['n']}"
            counter["n"] += 1
            return value

        transport = FakeTransport(lambda *args: response({"ok": True}))
        site = SitePlatform(
            "http://example.test/api/agent",
            token_provider=provider,
            transport=transport,
        )
        site.health()
        site.health()
        self.assertEqual(transport.calls[0].headers["Authorization"], "Bearer token-1")
        self.assertEqual(transport.calls[1].headers["Authorization"], "Bearer token-2")

    def test_missing_token_provider_raises(self):
        with self.assertRaises(ValueError):
            SitePlatform("http://example.test/api/agent", token_provider=lambda: "")


class WindowTotalsTests(unittest.TestCase):
    """The API only offers "last N days"; a closed window is a subtraction."""

    @staticmethod
    def metrics_handler(table):
        def handler(method, url, headers, data):
            days = int(parse_qs(urlparse(url).query)["days"][0])
            pv, inquiry, orders = table[days]
            return response(
                {
                    "days": days,
                    "items": [
                        {
                            "productId": "p1",
                            "pv": pv,
                            "uv": pv // 2,
                            "inquiries": inquiry,
                            "orders": orders,
                            "units": orders * 2,
                            "revenueCents": 0,
                            "reviews": 0,
                            "avgRating": 0,
                            "negativeReviews": 0,
                        }
                    ],
                }
            )

        return FakeTransport(handler)

    def test_two_reads_leave_the_slice(self):
        transport = self.metrics_handler({28: (2800, 28, 14), 14: (1400, 14, 7)})
        site = SitePlatform("http://example.test/api/agent", "token", transport=transport)
        totals = site.window_totals("p1", days_total=28, days_cut=14)
        self.assertEqual(totals["impressions"], 1400)
        self.assertEqual(totals["inquiries"], 14)
        self.assertEqual(totals["orders"], 7)
        self.assertEqual(totals["days"], 14)

    def test_open_window_costs_one_read(self):
        transport = self.metrics_handler({14: (1400, 14, 7)})
        site = SitePlatform("http://example.test/api/agent", "token", transport=transport)
        totals = site.window_totals("p1", days_total=14)
        self.assertEqual(totals["impressions"], 1400)
        self.assertEqual(len(transport.calls), 1)

    def test_unknown_product_is_empty_not_an_error(self):
        transport = self.metrics_handler({14: (1400, 14, 7)})
        site = SitePlatform("http://example.test/api/agent", "token", transport=transport)
        self.assertEqual(site.window_totals("nope", days_total=14), {})

    def test_clicks_are_zero_not_derived_from_pv(self):
        transport = self.metrics_handler({14: (1400, 14, 7)})
        site = SitePlatform("http://example.test/api/agent", "token", transport=transport)
        self.assertEqual(site.window_totals("p1", days_total=14)["clicks"], 0)

    def test_data_gap_is_empty_not_the_whole_period(self):
        # 近 16 天没数据、30 天有 1399：减法会把 1399 当成"后 14 天"，
        # 断档时宁可说测不到，也不能把老流量当新窗口。
        transport = self.metrics_handler({30: (1399, 2, 4), 16: (0, 0, 0)})
        site = SitePlatform("http://example.test/api/agent", "token", transport=transport)
        self.assertEqual(site.window_totals("p1", days_total=30, days_cut=16), {})


if __name__ == "__main__":
    unittest.main()
