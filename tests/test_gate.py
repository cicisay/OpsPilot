import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.cli import cmd_approve_issue  # noqa: E402
from ops_agent.publish import (  # noqa: E402
    PublishOrchestrator,
    load_verdicts,
    verdict_for,
)
from ops_agent.site_platform import SiteProduct, SiteVariant  # noqa: E402

DETAIL = {
    "productId": "p1",
    "variants": [{"variantId": "v1", "priceCents": 620, "costCents": 400, "stock": 800}],
}


def make_product() -> SiteProduct:
    variant = SiteVariant("v1", "P1-01", 620, 400, 800, {})
    return SiteProduct(
        product_id="p1",
        slug="p1",
        title_en="Ceramic Mug Set",
        status="active",
        category_id="c1",
        category_slug="home-kitchen",
        moq=100,
        lead_time_days=18,
        incoterm="FOB",
        variants=(variant,),
        metrics=None,
    )


def make_proposal() -> dict:
    return {
        "product_id": "p1",
        "title": "Ceramic Mug Set",
        "problem": "low_order",
        "variants": [
            {
                "variant_key": "p1-v1",
                "angle": "阶梯价",
                "title": "Ceramic Mug Set - Tier Pricing",
                "bullets": ["MOQ 100 pcs"],
                "description": "Tier pricing for cafes that reorder monthly.",
                "price_cents": 620,
                "material_brief": ["主体：四只装"],
            }
        ],
    }


class FakeSite:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def product(self, product_id: str) -> dict:
        return DETAIL

    def patch_product(self, product_id, fields, reason=None, idempotency_key=None, dry_run=False):
        self.calls.append({"kind": "product", "dry_run": dry_run})
        if dry_run:
            return {"ok": True, "diff": {}}
        return {"ok": True, "versionId": "pv1"}

    def patch_variant(self, variant_id, price_cents=None, stock=None, reason=None,
                      idempotency_key=None, dry_run=False):
        self.calls.append({"kind": "variant", "dry_run": dry_run})
        if dry_run:
            return {"ok": True, "diff": {}}
        return {"ok": True, "versionId": "vv1"}


class FakeApprovals:
    def consume(self, token, product_id, variant_key, action="publish"):
        return SimpleNamespace(issued_by="tester")


def write_review(path: Path, results: list[dict]) -> None:
    path.write_text(json.dumps({"results": results}, ensure_ascii=False), encoding="utf-8")


class VerdictIndexTests(unittest.TestCase):
    def test_new_format_is_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.json"
            write_review(path, [{"product_id": "p1", "variant_key": "p1-v1", "severity": "block"}])
            index = load_verdicts(path)
        self.assertEqual(index[("p1", "p1-v1")]["severity"], "block")

    def test_legacy_passed_false_becomes_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.json"
            write_review(path, [{"product_id": "p1", "variant_key": "p1-v1", "passed": False}])
            index = load_verdicts(path)
        self.assertEqual(index[("p1", "p1-v1")]["severity"], "block")

    def test_legacy_passed_true_is_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.json"
            write_review(path, [{"product_id": "p1", "variant_key": "p1-v1", "passed": True}])
            index = load_verdicts(path)
        self.assertEqual(index[("p1", "p1-v1")]["severity"], "pass")

    def test_missing_file_is_empty(self):
        self.assertEqual(load_verdicts(Path("nope.json")), {})

    def test_unknown_variant_is_empty(self):
        self.assertEqual(verdict_for({}, "p1", "missing"), {})


class PublishGateTests(unittest.TestCase):
    def setUp(self):
        self.site = FakeSite()
        self.product = make_product()
        self.tmp = tempfile.TemporaryDirectory()
        self.review = Path(self.tmp.name) / "review.json"
        write_review(
            self.review,
            [
                {
                    "product_id": "p1",
                    "variant_key": "p1-v1",
                    "severity": "block",
                    "findings": [{"category": "c1_absolute", "quote": "100%", "rule": "绝对化承诺", "suggestion": "删除"}],
                }
            ],
        )
        self.orchestrator = PublishOrchestrator(
            self.site,
            approvals=FakeApprovals(),
            store_slug="shop1",
            verdict_path=self.review,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_preview_marks_a_blocked_variant(self):
        preview = self.orchestrator.preview(make_proposal(), self.product)
        self.assertTrue(preview.blocked)
        self.assertEqual(preview.verdict["severity"], "block")

    def test_blocked_variant_never_publishes_even_with_a_token(self):
        result = self.orchestrator.publish(
            make_proposal(), self.product, approval_token="valid"
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("合规否决", result.message)
        writes = [call for call in self.site.calls if call["dry_run"] is False]
        self.assertEqual(writes, [])

    def test_warn_variant_is_approvable(self):
        write_review(
            self.review,
            [{"product_id": "p1", "variant_key": "p1-v1", "severity": "warn"}],
        )
        orchestrator = PublishOrchestrator(
            self.site, approvals=FakeApprovals(), store_slug="shop1", verdict_path=self.review
        )
        result = orchestrator.publish(make_proposal(), self.product, approval_token="valid")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.status, "published")
        self.assertFalse(result.preview.blocked)

    def test_no_review_file_keeps_old_behaviour(self):
        orchestrator = PublishOrchestrator(
            self.site, approvals=FakeApprovals(), store_slug="shop1"
        )
        preview = orchestrator.preview(make_proposal(), self.product)
        self.assertFalse(preview.blocked)


class ApproveGateTests(unittest.TestCase):
    def test_approve_refuses_a_blocked_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.json"
            write_review(
                path,
                [
                    {
                        "product_id": "p1",
                        "variant_key": "p1-v1",
                        "severity": "block",
                        "findings": [{"rule": "未授权品牌词", "quote": "Nike"}],
                    }
                ],
            )
            args = argparse.Namespace(
                product_id="p1",
                variant_key="p1-v1",
                operator="alice",
                ttl=3600,
                review=str(path),
            )
            with patch.dict(os.environ, {"OPS_APPROVER_TOKEN": "secret"}):
                code = cmd_approve_issue(args)
        self.assertEqual(code, 1)

    def test_approve_allows_a_warn_variant_to_reach_the_store(self):
        """Only the publish gate is exercised here; issuing writes a real token store."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.json"
            write_review(path, [{"product_id": "p1", "variant_key": "p1-v1", "severity": "warn"}])
            index = load_verdicts(path)
        self.assertEqual(verdict_for(index, "p1", "p1-v1")["severity"], "warn")


if __name__ == "__main__":
    unittest.main()
