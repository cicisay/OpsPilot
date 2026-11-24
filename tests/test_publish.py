import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.approvals import ApprovalError  # noqa: E402
from ops_agent.publish import PublishBusy, PublishOrchestrator  # noqa: E402
from ops_agent.site_platform import SiteAPIError, SiteMetrics, SiteProduct, SiteVariant  # noqa: E402

DETAIL = {
    "productId": "p1",
    "i18n": {"En": {"title": "Ceramic Mug", "bullets": ["old bullet"]}},
    "variants": [
        {"variantId": "v1", "priceCents": 620, "costCents": 400, "stock": 1000},
        {"variantId": "v2", "priceCents": 700, "costCents": 400, "stock": 500},
    ],
}


def make_product() -> SiteProduct:
    variants = (
        SiteVariant("v1", "P1-01", 620, 400, 1000, {}),
        SiteVariant("v2", "P1-02", 700, 400, 500, {}),
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
        variants=variants,
        metrics=metrics,
    )


def make_proposal(price: int = 500, title: str = "Ceramic Mug — Bulk Pricing Available") -> dict:
    return {
        "product_id": "p1",
        "title": "Ceramic Mug",
        "problem": "low_order",
        "variants": [
            {
                "variant_key": "p1-tier",
                "angle": "阶梯价",
                "title": title,
                "bullets": ["Tier pricing from 100 pcs"],
                "description": "Built for wholesale buyers. QC documents available.",
                "price_cents": price,
                "image_brief": "Scene for bulk pricing, product in use, no visible logos",
            }
        ],
    }


class FakeSite:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_variant = False

    def product(self, product_id: str) -> dict:
        return DETAIL

    def patch_product(self, product_id, fields, reason=None, idempotency_key=None, dry_run=False):
        self.calls.append(
            {"kind": "product", "dry_run": dry_run, "fields": fields, "key": idempotency_key}
        )
        if dry_run:
            return {
                "ok": True,
                "dryRun": True,
                "diff": {"titleEn": {"before": "Ceramic Mug", "after": fields["titleEn"]}},
            }
        return {"ok": True, "productId": product_id, "versionId": "pv1", "changed": list(fields)}

    def patch_variant(self, variant_id, price_cents=None, stock=None, reason=None, idempotency_key=None, dry_run=False):
        self.calls.append(
            {"kind": "variant", "dry_run": dry_run, "price": price_cents, "key": idempotency_key}
        )
        if dry_run:
            return {"ok": True, "dryRun": True, "diff": {"priceCents": {"before": 620, "after": price_cents}}}
        if self.fail_variant:
            raise SiteAPIError(500, "boom", "variant failed")
        return {"ok": True, "variantId": variant_id, "versionId": "vv1", "priceCents": price_cents}

    def rollback(self, product_id, version_id, reason=None):
        self.calls.append({"kind": "rollback", "version": version_id})
        return {"ok": True, "productId": product_id, "restoredFrom": version_id}


class FakeApprovals:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def consume(self, token, product_id, variant_key, action="publish"):
        self.calls.append((token, product_id, variant_key, action))
        if token != "valid":
            raise ApprovalError("bad token")
        return SimpleNamespace(issued_by="tester")


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.site = FakeSite()
        self.approvals = FakeApprovals()
        self.orchestrator = PublishOrchestrator(
            self.site, approvals=self.approvals, store_slug="shop1"
        )
        self.product = make_product()

    def test_big_price_change_requires_approval(self):
        preview = self.orchestrator.preview(make_proposal(price=500), self.product)
        self.assertTrue(preview.requires_approval)
        self.assertTrue(preview.review.passed)
        self.assertEqual(preview.variant_id, "v1")

    def test_small_price_change_does_not_require_approval(self):
        preview = self.orchestrator.preview(make_proposal(price=600), self.product)
        self.assertFalse(preview.requires_approval)

    def test_publish_without_approval_is_blocked(self):
        result = self.orchestrator.publish(make_proposal(price=500), self.product)
        self.assertEqual(result.status, "approval_required")
        self.assertFalse(any(not call["dry_run"] for call in self.site.calls))

    def test_publish_with_invalid_approval_is_blocked(self):
        result = self.orchestrator.publish(
            make_proposal(price=500), self.product, approval_token="bad"
        )
        self.assertEqual(result.status, "invalid_approval")
        self.assertFalse(any(not call["dry_run"] for call in self.site.calls))

    def test_publish_with_approval_writes(self):
        result = self.orchestrator.publish(
            make_proposal(price=500), self.product, approval_token="valid"
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "published")
        self.assertEqual(result.product_version_id, "pv1")
        self.assertEqual(result.variant_version_id, "vv1")
        self.assertEqual(result.approved_by, "tester")
        writes = [call for call in self.site.calls if call["kind"] in ("product", "variant") and not call["dry_run"]]
        self.assertEqual(len(writes), 2)
        self.assertTrue(all(call["key"] for call in writes))
        self.assertTrue(all(call["key"].startswith("pub-shop1-") for call in writes))

    def test_compliance_failure_blocks(self):
        result = self.orchestrator.publish(
            make_proposal(price=600, title="100% guaranteed mug"),
            self.product,
            approval_token="valid",
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.approvals.calls, [])
        self.assertFalse(any(not call["dry_run"] for call in self.site.calls))

    def test_variant_failure_rolls_back_product(self):
        self.site.fail_variant = True
        result = self.orchestrator.publish(
            make_proposal(price=500), self.product, approval_token="valid"
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.product_version_id, "pv1")
        self.assertTrue(any(call["kind"] == "rollback" for call in self.site.calls))

    def test_variant_index_out_of_range(self):
        with self.assertRaises(ValueError):
            self.orchestrator.preview(make_proposal(), self.product, variant_index=3)

    def test_target_variant_is_used(self):
        proposal = make_proposal(price=650)
        proposal["target_variant_id"] = "v2"
        preview = self.orchestrator.preview(proposal, self.product)
        self.assertEqual(preview.variant_id, "v2")

    def test_publish_lock_busy_returns_busy(self):
        with patch.object(self.orchestrator, "_acquire_lock", side_effect=PublishBusy("busy")):
            result = self.orchestrator.publish(
                make_proposal(price=500), self.product, approval_token="valid"
            )
        self.assertEqual(result.status, "busy")
        self.assertFalse(any(not call["dry_run"] for call in self.site.calls))


class PublishExperimentHookTests(unittest.TestCase):
    """发布成功才登记实验；登记失败不能让已经上线的发布回滚。"""

    def setUp(self):
        self.site = FakeSite()
        self.product = make_product()

    def build(self, hook):
        return PublishOrchestrator(
            self.site, approvals=FakeApprovals(), store_slug="shop1", on_published=hook
        )

    def test_hook_sees_the_published_variant(self):
        seen = []

        def hook(proposal, preview):
            seen.append((proposal["product_id"], preview.variant_key))
            return "p1:p1-tier:2026-11-03"

        result = self.build(hook).publish(
            make_proposal(price=500), self.product, approval_token="valid"
        )
        self.assertEqual(seen, [("p1", "p1-tier")])
        self.assertEqual(result.experiment_id, "p1:p1-tier:2026-11-03")
        self.assertEqual(result.warnings, [])

    def test_hook_not_called_when_blocked(self):
        seen = []
        result = self.build(lambda *a: seen.append(a)).publish(
            make_proposal(price=600, title="100% guaranteed mug"),
            self.product,
            approval_token="valid",
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(seen, [])

    def test_hook_failure_keeps_the_publish(self):
        def hook(proposal, preview):
            raise RuntimeError("ledger is read-only")

        result = self.build(hook).publish(
            make_proposal(price=500), self.product, approval_token="valid"
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "published")
        self.assertEqual(result.experiment_id, "")
        self.assertIn("ledger is read-only", result.warnings[0])


if __name__ == "__main__":
    unittest.main()
