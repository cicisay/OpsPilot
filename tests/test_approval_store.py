import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.approval_store import ApprovalStore  # noqa: E402
from ops_agent.approvals import ApprovalError  # noqa: E402


class ApprovalStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = ApprovalStore(Path("unused.json"))
        self.data = {"tokens": []}
        self.store._load = lambda: self.data
        self.store._save = lambda payload: None

    def test_issue_and_consume_once(self):
        record = self.store.issue("p1", "v1", "alice", ttl_seconds=60)
        self.assertTrue(record.token.startswith("APR-"))
        consumed = self.store.consume(record.token, "p1", "v1", "publish")
        self.assertEqual(consumed.issued_by, "alice")
        with self.assertRaises(ApprovalError):
            self.store.consume(record.token, "p1", "v1", "publish")

    def test_token_is_bound_to_product(self):
        record = self.store.issue("p1", "v1", "alice", ttl_seconds=60)
        with self.assertRaises(ApprovalError):
            self.store.consume(record.token, "p2", "v1", "publish")

    def test_token_is_bound_to_variant(self):
        record = self.store.issue("p1", "v1", "alice", ttl_seconds=60)
        with self.assertRaises(ApprovalError):
            self.store.consume(record.token, "p1", "v2", "publish")

    def test_invalid_ttl_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.issue("p1", "v1", "alice", ttl_seconds=0)

    def test_list_active_skips_used(self):
        record = self.store.issue("p1", "v1", "alice", ttl_seconds=60)
        self.assertEqual(len(self.store.list_active()), 1)
        self.store.consume(record.token, "p1", "v1", "publish")
        self.assertEqual(self.store.list_active(), [])


if __name__ == "__main__":
    unittest.main()
