import http.client
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.config import DATA_DIR  # noqa: E402
_HAS_TENANT_DATA = (DATA_DIR / "tenants" / "globaltrade" / "task_orders.json").exists()
from ops_agent.console.app import ConsoleApp, agent_metadata, _snapshot_baseline  # noqa: E402
from ops_agent.console.audit import AuditDB  # noqa: E402
from ops_agent.console.auth import (  # noqa: E402
    LoginLimiter,
    SessionManager,
    UserStore,
    hash_password,
    verify_password,
)
from ops_agent.console.server import ConsoleServer  # noqa: E402
from ops_agent.console.tenants import Tenant, TenantStore  # noqa: E402


class FakeApprovals:
    def list_active(self):
        return []

    def issue(self, product_id, variant_key, issued_by, action="publish", ttl_seconds=86400):
        return SimpleNamespace(
            token="APR-TEST-1234",
            product_id=product_id,
            variant_key=variant_key,
            issued_by=issued_by,
            expires_at="2026-11-03T00:00:00+00:00",
        )


def make_app() -> ConsoleApp:
    users = UserStore(users={}, iterations=1000)
    users.add("admin", "admin-pass-1", "admin", ["globaltrade", "other"], "管理员")
    users.add("operator", "operator-pass-1", "operator", ["globaltrade"], "运营")
    tenants = TenantStore(Path("unused.json"), DATA_DIR)
    tenants.tenants = {
        "globaltrade": Tenant("globaltrade", "GlobalTrade", "", "", DATA_DIR),
        "other": Tenant("other", "Other Store", "", "", DATA_DIR),
    }
    audit = AuditDB(":memory:")
    return ConsoleApp(users, tenants, approval_factory=lambda tenant: FakeApprovals(), audit=audit)


class AgentMetadataTests(unittest.TestCase):
    def test_llm_provenance_strategy_and_critique(self):
        meta = agent_metadata(
            {
                "planner": "llm",
                "critique": {"score": 0.82, "verdict": "pass", "issues": ["初稿同质化"]},
                "revisions": 1,
                "task": {
                    "strategy": {
                        "problem": "high_negative",
                        "problem_label": "差评高",
                        "hypothesis": "色差导致不信任",
                        "action": "补自然光实拍",
                        "confidence": 0.86,
                        "angles": [
                            {"angle": "真实实拍", "focus": "自然光釉色", "price_index": 1}
                        ],
                        "evidence": ["评价原文：颜色和图片不一样"],
                    }
                },
            }
        )
        self.assertEqual(meta["source"], "model")
        self.assertEqual(meta["label"], "模型生成")
        self.assertEqual(meta["strategy"]["problem"], "high_negative")
        self.assertEqual(meta["strategy"]["confidence"], 0.86)
        self.assertEqual(meta["strategy"]["angles"][0]["price_index"], 1)
        self.assertEqual(meta["critique"]["score"], 0.82)
        self.assertEqual(meta["revisions"], 1)

    def test_fallback_is_distinguished_from_rule(self):
        meta = agent_metadata({"planner": "rule (fallback)"})
        self.assertEqual(meta["source"], "fallback")
        self.assertTrue(meta["recorded"])
        self.assertIn("兜底", meta["label"])

    def test_legacy_payload_does_not_claim_rule_provenance(self):
        meta = agent_metadata({"product_id": "p1", "variants": []})
        self.assertEqual(meta["source"], "legacy")
        self.assertFalse(meta["recorded"])
        self.assertEqual(meta["strategy"]["angles"], [])

    def test_malformed_fields_are_ignored(self):
        meta = agent_metadata(
            {
                "planner": "llm",
                "critique": {"score": "bad", "issues": "not-a-list"},
                "revisions": True,
                "strategy": {"angles": "not-a-list", "confidence": "high"},
            }
        )
        self.assertIsNone(meta["critique"]["score"])
        self.assertEqual(meta["critique"]["issues"], [])
        self.assertEqual(meta["strategy"]["angles"], [])
        self.assertEqual(meta["revisions"], 0)

    def test_overview_attaches_metadata_without_mutating_source_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            proposal = {
                "product_id": "p1",
                "title": "Test Product",
                "problem": "high_negative",
                "planner": "llm",
                "critique": {"score": 0.8, "verdict": "pass", "issues": []},
                "revisions": 2,
                "task": {
                    "strategy": {
                        "problem": "high_negative",
                        "problem_label": "差评高",
                        "angles": [{"angle": "实拍", "price_index": 1}],
                    }
                },
                "variants": [],
            }
            (directory / "workshop_proposals.json").write_text(
                json.dumps({"proposals": [proposal]}, ensure_ascii=False), encoding="utf-8"
            )
            tenants = TenantStore(Path("unused.json"), directory)
            tenants.tenants = {"t1": Tenant("t1", "Tenant One", "", "", directory)}
            audit = AuditDB(":memory:")
            app = ConsoleApp(UserStore(users={}, iterations=1000), tenants, audit=audit)
            try:
                payload = app.overview({"role": "admin"}, "t1")
            finally:
                audit.close()
            meta = payload["proposals"][0]["agent_meta"]
            self.assertEqual(meta["source"], "model")
            self.assertEqual(meta["revisions"], 2)
            self.assertEqual(meta["strategy"]["angles"][0]["angle"], "实拍")
            raw = json.loads((directory / "workshop_proposals.json").read_text(encoding="utf-8"))
            self.assertNotIn("agent_meta", raw["proposals"][0])


class SnapshotBaselineTests(unittest.TestCase):
    """站点基线只能取它真有的指标。"""

    def test_uv_is_not_treated_as_clicks(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "site_snapshot.json").write_text(
                json.dumps(
                    {
                        "products": [
                            {
                                "product_id": "p1",
                                "metrics": {"days": 30, "pv": 1804, "uv": 1266,
                                            "inquiries": 2, "orders": 4},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            baseline = _snapshot_baseline(directory, "p1")
            # uv 是独立访客，不是点击；把它当 clicks 会算出 70% 的假点击率。
            self.assertEqual(baseline["clicks"], 0)
            self.assertEqual(baseline["click_rate"], 0.0)
            self.assertAlmostEqual(baseline["order_rate"], 0.002217, places=6)
            self.assertIn("无点击计数", baseline["note"])


class ConsoleAuthTests(unittest.TestCase):
    def test_password_hash_and_verify(self):
        encoded = hash_password("correct horse", iterations=1000)
        self.assertTrue(verify_password("correct horse", encoded))
        self.assertFalse(verify_password("wrong", encoded))
        self.assertFalse(verify_password("correct horse", "garbage"))

    def test_session_expiry(self):
        sessions = SessionManager(ttl_seconds=-1)
        session = sessions.create("admin")
        self.assertIsNone(sessions.get(session.token))

    def test_login_limiter(self):
        limiter = LoginLimiter(max_failures=2, window_seconds=60)
        limiter.fail("ip:admin")
        self.assertFalse(limiter.blocked("ip:admin"))
        limiter.fail("ip:admin")
        self.assertTrue(limiter.blocked("ip:admin"))
        limiter.reset("ip:admin")
        self.assertFalse(limiter.blocked("ip:admin"))

    def test_user_store_rejects_duplicate(self):
        users = UserStore(users={}, iterations=1000)
        users.add("admin", "password-1", "admin", ["globaltrade"])
        with self.assertRaises(ValueError):
            users.add("admin", "password-2", "admin", ["globaltrade"])

    def test_user_store_rejects_short_password(self):
        users = UserStore(users={}, iterations=1000)
        with self.assertRaises(ValueError):
            users.add("admin", "short", "admin", ["globaltrade"])

    def test_user_update_disable_and_password(self):
        users = UserStore(users={}, iterations=1000)
        users.add("admin", "password-1", "admin", ["globaltrade"])
        users.add("operator", "password-2", "operator", ["globaltrade"])
        users.update("operator", disabled=True)
        self.assertIsNone(users.authenticate("operator", "password-2"))
        users.update("operator", disabled=False)
        self.assertIsNotNone(users.authenticate("operator", "password-2"))
        users.set_password("operator", "password-3")
        self.assertIsNone(users.authenticate("operator", "password-2"))
        self.assertIsNotNone(users.authenticate("operator", "password-3"))

    def test_last_admin_cannot_be_demoted(self):
        users = UserStore(users={}, iterations=1000)
        users.add("admin", "password-1", "admin", ["globaltrade"])
        with self.assertRaises(ValueError):
            users.update("admin", role="operator")
        with self.assertRaises(ValueError):
            users.update("admin", disabled=True)

    def test_tls_requires_cert_and_key_together(self):
        with self.assertRaises(ValueError):
            ConsoleServer(make_app(), port=0, tls_cert="cert.pem")


class ConsoleHTTPTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.server = ConsoleServer(self.app, "127.0.0.1", 0)
        import threading

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.close()
        self.thread.join(timeout=2)
        self.app.audit.close()

    def request(self, method, path, body=None, headers=None, cookie=True):
        conn = http.client.HTTPConnection(self.server.host, self.server.port, timeout=5)
        request_headers = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if cookie and self.cookie:
            request_headers["Cookie"] = self.cookie
        conn.request(method, path, body=payload, headers=request_headers)
        response = conn.getresponse()
        raw = response.read()
        set_cookie = response.getheader("Set-Cookie")
        if set_cookie and set_cookie.startswith("opspilot_session="):
            self.cookie = set_cookie.split(";", 1)[0]
        conn.close()
        return response.status, json.loads(raw.decode("utf-8"))

    def request_raw(self, method, path):
        conn = http.client.HTTPConnection(self.server.host, self.server.port, timeout=5)
        headers = {"Cookie": self.cookie} if self.cookie else {}
        conn.request(method, path, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        result_headers = dict(response.getheaders())
        conn.close()
        return response.status, raw, result_headers

    def login(self, username, password):
        status, payload = self.request(
            "POST",
            "/api/login",
            {"username": username, "password": password},
            cookie=False,
        )
        self.assertEqual(status, 200, payload)
        return payload["csrf"]

    @unittest.skipUnless(_HAS_TENANT_DATA, "demo tenant data not generated (run: python run.py seed)")
    def test_login_me_and_overview(self):
        csrf = self.login("admin", "admin-pass-1")
        status, payload = self.request("GET", "/api/me")
        self.assertEqual(status, 200)
        self.assertEqual(payload["user"]["username"], "admin")

        status, payload = self.request("GET", "/api/tenants/globaltrade/overview")
        self.assertEqual(status, 200)
        self.assertIn("task_orders", payload)
        self.assertIn("proposals", payload)
        self.assertGreater(len(payload["task_orders"]), 0)
        self.assertIn("agent_meta", payload["task_orders"][0])
        if payload["proposals"]:
            self.assertIn("agent_meta", payload["proposals"][0])

    def test_overview_carries_the_console_panels(self):
        self.login("admin", "admin-pass-1")
        status, payload = self.request("GET", "/api/tenants/globaltrade/overview")
        self.assertEqual(status, 200)
        for key in ("checkup", "metrics", "experiment_board", "memory"):
            self.assertIn(key, payload)
        self.assertIn("available", payload["checkup"])
        self.assertEqual(len(payload["metrics"]["metrics"]), 6)
        for key in ("observing", "judged", "summary"):
            self.assertIn(key, payload["experiment_board"])
        for key in ("profile", "decisions", "lessons", "snapshots", "health"):
            self.assertIn(key, payload["memory"])
        if payload["proposals"]:
            proposal = payload["proposals"][0]
            self.assertIn("compliance_summary", proposal)
            for variant in proposal["variants"]:
                self.assertIn("compliance", variant)
                self.assertIn("approvable", variant)
                self.assertEqual(set(variant["compliance"]["checks"]), {
                    "c1_absolute", "c2_ip", "c3_category", "c4_market"
                })

    def test_memory_replay_returns_the_snapshot(self):
        self.login("admin", "admin-pass-1")
        snapshot = {
            "run_id": "20261103-110000-abcd",
            "saved_at": "2026-11-03T11:00:00",
            "store_profile": {"store_slug": "globaltrade"},
            "experiments": [],
            "decisions": [{"decision_id": "d1"}],
            "lessons": [],
        }
        with patch("ops_agent.console.app.replay_snapshot", return_value=snapshot):
            status, payload = self.request("GET", "/api/tenants/globaltrade/memory/20261103-110000-abcd")
        self.assertEqual(status, 200)
        self.assertEqual(payload["run_id"], "20261103-110000-abcd")
        self.assertEqual(len(payload["decisions"]), 1)

    def test_memory_replay_missing_snapshot_is_404(self):
        self.login("admin", "admin-pass-1")
        status, payload = self.request("GET", "/api/tenants/globaltrade/memory/20990101-000000-0000")
        self.assertEqual(status, 404)
        self.assertIn("快照", payload["error"])

    def test_memory_replay_rejects_traversal(self):
        self.login("admin", "admin-pass-1")
        # The route pattern only allows recorder-style ids, so a traversal never
        # reaches the handler and reads as "not found" rather than leaking a path.
        for path in ("/api/tenants/globaltrade/memory/..", "/api/tenants/globaltrade/memory/..%2f..%2fetc"):
            with self.subTest(path=path):
                status, _ = self.request("GET", path)
                self.assertEqual(status, 404)

    def test_wrong_password_is_401(self):
        status, payload = self.request(
            "POST",
            "/api/login",
            {"username": "admin", "password": "wrong"},
            cookie=False,
        )
        self.assertEqual(status, 401)
        self.assertIn("用户名或密码错误", payload["error"])

    def test_csrf_required_for_post(self):
        self.login("admin", "admin-pass-1")
        status, payload = self.request("POST", "/api/logout", {})
        self.assertEqual(status, 403)
        self.assertIn("CSRF", payload["error"])

    def test_operator_cannot_approve_or_cross_tenant(self):
        csrf = self.login("operator", "operator-pass-1")
        status, payload = self.request(
            "POST",
            "/api/tenants/globaltrade/approve",
            {"product_id": "p1", "variant_key": "v1"},
            {"X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 403)
        self.assertIn("审批人", payload["error"])

        status, payload = self.request("GET", "/api/tenants/other/overview")
        self.assertEqual(status, 403)

    def test_admin_can_issue_token(self):
        csrf = self.login("admin", "admin-pass-1")
        # The approval gate requires a compliance verdict, so the test supplies
        # one instead of relying on whatever the data directory happens to hold.
        with patch(
            "ops_agent.console.app.load_verdicts",
            return_value={("p1", "v1"): {"severity": "pass", "findings": []}},
        ):
            status, payload = self.request(
                "POST",
                "/api/tenants/globaltrade/approve",
                {"product_id": "p1", "variant_key": "v1"},
                {"X-CSRF-Token": csrf},
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["token"].startswith("APR-"))

    def test_unreviewed_variant_cannot_be_approved(self):
        csrf = self.login("admin", "admin-pass-1")
        with patch("ops_agent.console.app.load_verdicts", return_value={}):
            status, payload = self.request(
                "POST",
                "/api/tenants/globaltrade/approve",
                {"product_id": "p1", "variant_key": "v1"},
                {"X-CSRF-Token": csrf},
            )
        self.assertEqual(status, 409)
        self.assertIn("没有合规结论", payload["error"])

    def test_blocked_variant_cannot_be_approved(self):
        csrf = self.login("admin", "admin-pass-1")
        with patch(
            "ops_agent.console.app.load_verdicts",
            return_value={("p1", "v1"): {"severity": "block", "findings": []}},
        ):
            status, payload = self.request(
                "POST",
                "/api/tenants/globaltrade/approve",
                {"product_id": "p1", "variant_key": "v1"},
                {"X-CSRF-Token": csrf},
            )
        self.assertEqual(status, 409)
        self.assertIn("否决", payload["error"])

    def test_admin_lists_users_operator_forbidden(self):
        csrf = self.login("admin", "admin-pass-1")
        status, payload = self.request("GET", "/api/users")
        self.assertEqual(status, 200)
        usernames = {item["username"] for item in payload["users"]}
        self.assertEqual(usernames, {"admin", "operator"})

        csrf = self.login("operator", "operator-pass-1")
        status, payload = self.request("GET", "/api/users")
        self.assertEqual(status, 403)
        self.assertIn("管理员", payload["error"])

    def test_admin_can_create_user(self):
        csrf = self.login("admin", "admin-pass-1")
        status, payload = self.request(
            "POST",
            "/api/users",
            {
                "username": "newuser",
                "password": "newuser-pass",
                "role": "operator",
                "tenants": ["globaltrade"],
            },
            {"X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["user"]["username"], "newuser")
        self.assertIsNotNone(self.app.users.authenticate("newuser", "newuser-pass"))

    def test_login_is_written_to_audit(self):
        self.login("admin", "admin-pass-1")
        status, payload = self.request("GET", "/api/audit?limit=20")
        self.assertEqual(status, 200)
        self.assertTrue(any(item["action"] == "login" and item["actor"] == "admin" for item in payload["logs"]))

    def test_non_admin_audit_requires_tenant(self):
        self.login("operator", "operator-pass-1")
        status, payload = self.request("GET", "/api/audit")
        self.assertEqual(status, 400)
        status, payload = self.request("GET", "/api/audit?tenant=globaltrade")
        self.assertEqual(status, 200)
        status, payload = self.request("GET", "/api/audit?tenant=other")
        self.assertEqual(status, 403)

    def test_audit_export_csv(self):
        self.login("admin", "admin-pass-1")
        status, raw, headers = self.request_raw("GET", "/api/audit/export")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers.get("Content-Type", ""))
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn("login", raw.decode("utf-8-sig"))

    def test_path_traversal_tenant_is_not_found(self):
        self.login("admin", "admin-pass-1")
        status, _ = self.request("GET", "/api/tenants/../overview")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
