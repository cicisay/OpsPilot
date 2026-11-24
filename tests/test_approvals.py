import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.approvals import ApprovalError, ApprovalService  # noqa: E402


class ApprovalTests(unittest.TestCase):
    def test_issue_and_consume(self):
        service = ApprovalService()
        token = service.issue("SKU-1", "publish", "price cut")
        record = service.consume(token.token, "SKU-1", "publish")
        self.assertTrue(record.used)

    def test_reuse_is_rejected(self):
        service = ApprovalService()
        token = service.issue("SKU-1", "publish", "price cut")
        service.consume(token.token, "SKU-1", "publish")
        with self.assertRaises(ApprovalError):
            service.consume(token.token, "SKU-1", "publish")

    def test_wrong_sku_is_rejected(self):
        service = ApprovalService()
        token = service.issue("SKU-1", "publish", "price cut")
        with self.assertRaises(ApprovalError):
            service.validate(token.token, "SKU-2", "publish")

    def test_expired_token_is_rejected(self):
        service = ApprovalService(ttl_seconds=-1)
        token = service.issue("SKU-1", "publish", "price cut")
        with self.assertRaises(ApprovalError):
            service.validate(token.token, "SKU-1", "publish")


if __name__ == "__main__":
    unittest.main()
