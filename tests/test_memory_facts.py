"""作业记忆（Step 6）：facts 的存储、检索、冲突与撤回。

五件必须钉死的事（PRD 通过标准）：
1. 冲突不静默覆盖——旧记录标 superseded，但历史行一个字都不改；
2. 软删后不可召回，同时旧行仍在盘上；
3. 过期不召回，且不依赖"恰好跑过清理任务"；
4. 来源白名单——不在表里的来源一律拒绝；
5. 注入预算截断。

额外钉一条边界：委托原文（intake）永远不进 facts。
"""

import http.client
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.console.app import ConsoleApp, ConsoleError  # noqa: E402
from ops_agent.console.audit import AuditDB  # noqa: E402
from ops_agent.console.auth import LoginLimiter, SessionManager, UserStore  # noqa: E402
from ops_agent.console.server import ConsoleServer  # noqa: E402
from ops_agent.console.tenants import Tenant, TenantStore  # noqa: E402
from ops_agent.memory.facts import (  # noqa: E402
    ACTIVE,
    EXPIRED,
    MAX_CONTENT,
    REVOKED,
    SOURCE_CONFIDENCE,
    SUPERSEDED,
    FactsStore,
    estimate_tokens,
    format_injection,
)

NOW = datetime(2026, 11, 5, 10, 0, 0)
OLD = NOW - timedelta(days=200)


def add_shop_rule(store, content, **kwargs):
    kwargs.setdefault("scope", "shop")
    kwargs.setdefault("fact_type", "rule")
    kwargs.setdefault("source", "user_instruction")
    return store.add(content=content, **kwargs)


class FactsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = FactsStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------------------------------------------------- 形状与落盘

    def test_record_has_the_documented_shape(self):
        result = add_shop_rule(self.store, "不做宠物类目", importance=0.9, now=NOW)
        fact = result["fact"]
        self.assertEqual("M-0001", fact["memory_id"])
        self.assertEqual("shop", fact["scope"])
        self.assertEqual("rule", fact["type"])
        self.assertEqual("user_instruction", fact["source"])
        self.assertEqual(1.0, fact["confidence"])
        self.assertEqual(0.9, fact["importance"])
        self.assertEqual(ACTIVE, fact["status"])
        self.assertEqual(NOW.replace(microsecond=0).isoformat(), fact["created_at"])
        self.assertIsNone(fact["last_used_at"])
        self.assertIsNone(fact["supersedes"])
        self.assertFalse(result["duplicate"])
        self.assertEqual([], result["superseded"])

    def test_facts_file_sits_in_the_memory_directory(self):
        add_shop_rule(self.store, "不做宠物类目")
        self.assertEqual(self.root / "facts.jsonl", self.store.path)
        self.assertTrue(self.store.path.exists())

    def test_other_memory_files_are_not_created(self):
        add_shop_rule(self.store, "不做宠物类目")
        names = {path.name for path in self.root.iterdir()}
        self.assertEqual({"facts.jsonl"}, names)

    # -------------------------------------------------------------- 白名单

    def test_every_whitelisted_source_is_accepted(self):
        for index, source in enumerate(SOURCE_CONFIDENCE):
            result = add_shop_rule(self.store, f"第 {index} 条规则", source=source)
            self.assertEqual(source, result["fact"]["source"])

    def test_unknown_sources_are_rejected(self):
        for source in ("model", "intake", "system", "prompt", "assistant", ""):
            with self.assertRaises(ValueError) as raised:
                add_shop_rule(self.store, "不做宠物类目", source=source)
            self.assertIn("白名单", str(raised.exception))
        self.assertFalse(self.store.path.exists())

    def test_confidence_cannot_exceed_its_source(self):
        result = add_shop_rule(
            self.store, "客户偏好在周五下单", source="pattern", confidence=1.0
        )
        self.assertEqual(0.7, result["fact"]["confidence"])

    def test_confidence_can_be_lowered(self):
        result = add_shop_rule(self.store, "不做宠物类目", confidence=0.4)
        self.assertEqual(0.4, result["fact"]["confidence"])

    def test_importance_is_clamped(self):
        high = add_shop_rule(self.store, "不做宠物类目", importance=9)
        low = add_shop_rule(self.store, "不做玻璃制品", importance=-3)
        self.assertEqual(1.0, high["fact"]["importance"])
        self.assertEqual(0.0, low["fact"]["importance"])

    def test_bad_scope_type_and_content_are_rejected(self):
        cases = (
            {"scope": "galaxy", "fact_type": "rule", "content": "不做宠物类目"},
            {"scope": "shop", "fact_type": "vibe", "content": "不做宠物类目"},
            {"scope": "shop", "fact_type": "rule", "content": "   "},
            {"scope": "shop", "fact_type": "rule", "content": "长" * (MAX_CONTENT + 1)},
        )
        for case in cases:
            with self.assertRaises(ValueError):
                self.store.add(source="user_instruction", **case)

    # ---------------------------------------------------------------- 冲突

    def test_duplicate_content_is_not_written_twice(self):
        first = add_shop_rule(self.store, "不做宠物类目")
        second = add_shop_rule(self.store, " 不做宠物类目 ")
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["fact"]["memory_id"], second["fact"]["memory_id"])
        self.assertEqual(1, len(self.store.path.read_text(encoding="utf-8").splitlines()))

    def test_conflicting_content_supersedes_instead_of_overwriting(self):
        first = add_shop_rule(self.store, "不做宠物类目")
        second = add_shop_rule(self.store, "可以做宠物类目", now=NOW)
        self.assertEqual([first["fact"]["memory_id"]], second["superseded"])
        self.assertEqual(first["fact"]["memory_id"], second["fact"]["supersedes"])
        old = self.store.get(first["fact"]["memory_id"])
        self.assertEqual(SUPERSEDED, old["status"])
        self.assertEqual(second["fact"]["memory_id"], old["superseded_by"])

    def test_superseded_fact_is_not_recalled(self):
        add_shop_rule(self.store, "不做宠物类目", importance=0.9)
        add_shop_rule(self.store, "可以做宠物类目", now=NOW)
        hits = self.store.search(query="宠物类目", now=NOW)["facts"]
        self.assertEqual(["可以做宠物类目"], [item["content"] for item in hits])
        active = self.store.list(now=NOW)
        self.assertEqual(["可以做宠物类目"], [item["content"] for item in active])

    def test_superseding_keeps_every_history_line(self):
        add_shop_rule(self.store, "不做宠物类目")
        before = self.store.path.read_bytes()
        add_shop_rule(self.store, "可以做宠物类目", now=NOW)
        after = self.store.path.read_bytes()
        # append-only：旧字节是新的前缀，一个字符都没被改写。
        self.assertTrue(after.startswith(before))
        content = after.decode("utf-8")
        self.assertIn("不做宠物类目", content)
        self.assertIn('"status": "superseded"', content)

    def test_non_conflicting_content_stays_active(self):
        add_shop_rule(self.store, "不做宠物类目")
        add_shop_rule(self.store, "不做 3C 数码类目", now=NOW)
        self.assertEqual(2, len(self.store.list(now=NOW)))

    def test_conflict_survives_extra_clauses(self):
        # 真实写法几乎不会只写四个字；附加从句不能把冲突判定稀释掉。
        add_shop_rule(self.store, "不做宠物类目：利润太薄且退货率高", importance=0.9)
        second = add_shop_rule(self.store, "可以做宠物类目：找到稳定供应商了", now=NOW)
        self.assertEqual(1, len(second["superseded"]))

    def test_different_subject_is_not_a_conflict(self):
        add_shop_rule(self.store, "不做宠物类目")
        second = add_shop_rule(self.store, "可以做宠物玩具", now=NOW)
        self.assertEqual([], second["superseded"])

    def test_explicit_supersedes_is_honoured(self):
        first = add_shop_rule(self.store, "主推夏季品类")
        second = add_shop_rule(
            self.store, "主推冬季品类", supersedes=first["fact"]["memory_id"], now=NOW
        )
        self.assertEqual([first["fact"]["memory_id"]], second["superseded"])

    def test_superseding_an_unknown_fact_is_rejected(self):
        with self.assertRaises(ValueError):
            add_shop_rule(self.store, "主推冬季品类", supersedes="M-9999")

    def test_superseding_across_scopes_is_rejected(self):
        first = add_shop_rule(self.store, "不做宠物类目")
        with self.assertRaises(ValueError) as raised:
            self.store.add(
                scope="product",
                fact_type="rule",
                content="可以做宠物类目",
                source="user_instruction",
                supersedes=first["fact"]["memory_id"],
            )
        self.assertIn("scope", str(raised.exception))

    # ---------------------------------------------------------------- 撤回

    def test_revoked_fact_disappears_from_list_and_search(self):
        fact = add_shop_rule(self.store, "不做宠物类目")["fact"]
        self.assertTrue(self.store.revoke(fact["memory_id"])["changed"])
        self.assertEqual([], self.store.list(now=NOW))
        self.assertEqual([], self.store.search(query="宠物", now=NOW)["facts"])
        stored = self.store.get(fact["memory_id"])
        self.assertEqual(REVOKED, stored["status"])
        self.assertEqual(REVOKED, stored["effective_status"])

    def test_revoking_does_not_delete_the_line(self):
        fact = add_shop_rule(self.store, "不做宠物类目")["fact"]
        before = self.store.path.read_bytes()
        self.store.revoke(fact["memory_id"], reason="客户改口")
        after = self.store.path.read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertGreater(len(after), len(before))
        self.assertIn("不做宠物类目", after.decode("utf-8"))

    def test_revoke_is_idempotent(self):
        fact = add_shop_rule(self.store, "不做宠物类目")["fact"]
        self.assertTrue(self.store.revoke(fact["memory_id"])["changed"])
        second = self.store.revoke(fact["memory_id"])
        self.assertFalse(second["changed"])

    def test_revoking_an_unknown_fact_raises(self):
        with self.assertRaises(KeyError):
            self.store.revoke("M-9999")

    def test_all_status_view_still_shows_revoked(self):
        fact = add_shop_rule(self.store, "不做宠物类目")["fact"]
        self.store.revoke(fact["memory_id"])
        everything = self.store.list(status="all", now=NOW)
        self.assertEqual(1, len(everything))
        self.assertEqual(REVOKED, everything[0]["effective_status"])

    # ---------------------------------------------------------------- 过期

    def test_stale_low_importance_fact_is_not_recalled(self):
        add_shop_rule(self.store, "包邮门槛 30 美元", importance=0.3, now=OLD)
        # 没跑过清理任务也必须过期：否则"过期不召回"就依赖后台定时器。
        self.assertEqual([], self.store.search(query="包邮", now=NOW)["facts"])
        self.assertEqual([], self.store.list(now=NOW))

    def test_stale_but_important_fact_is_still_recalled(self):
        add_shop_rule(self.store, "不做宠物类目", importance=0.9, now=OLD)
        self.assertEqual(1, len(self.store.search(query="宠物", now=NOW)["facts"]))

    def test_recent_low_importance_fact_is_still_active(self):
        add_shop_rule(self.store, "包邮门槛 30 美元", importance=0.3, now=NOW)
        self.assertEqual(1, len(self.store.list(now=NOW)))

    def test_expire_stale_marks_records_once(self):
        fact = add_shop_rule(self.store, "包邮门槛 30 美元", importance=0.3, now=OLD)["fact"]
        self.assertEqual([fact["memory_id"]], self.store.expire_stale(now=NOW))
        self.assertEqual([], self.store.expire_stale(now=NOW))
        self.assertEqual(EXPIRED, self.store.get(fact["memory_id"])["status"])

    def test_touch_keeps_a_fact_from_expiring(self):
        fact = add_shop_rule(self.store, "包邮门槛 30 美元", importance=0.3, now=OLD)["fact"]
        self.assertTrue(self.store.touch(fact["memory_id"], now=NOW - timedelta(days=10)))
        self.assertEqual(1, len(self.store.list(now=NOW)))

    # ---------------------------------------------------------------- 检索

    def test_search_filters_by_scope(self):
        add_shop_rule(self.store, "不做宠物类目")
        self.store.add(
            scope="product",
            fact_type="rule",
            content="宠物窝必须带图",
            source="manual_edit",
        )
        hits = self.store.search(scope="product", query="宠物", now=NOW)["facts"]
        self.assertEqual(["宠物窝必须带图"], [item["content"] for item in hits])

    def test_search_ranks_by_importance(self):
        add_shop_rule(self.store, "宠物类目开价 20 美元", importance=0.2)
        add_shop_rule(self.store, "宠物类目要过认证", importance=0.9)
        hits = self.store.search(query="宠物类目", now=NOW)["facts"]
        self.assertEqual("宠物类目要过认证", hits[0]["content"])
        self.assertGreater(hits[0]["score"], hits[1]["score"])

    def test_search_without_query_returns_everything_active(self):
        add_shop_rule(self.store, "不做宠物类目")
        add_shop_rule(self.store, "主推夏季品类")
        self.assertEqual(2, self.store.search(now=NOW)["count"])

    def test_search_ignores_unrelated_facts(self):
        add_shop_rule(self.store, "不做宠物类目")
        add_shop_rule(self.store, "主推夏季品类")
        hits = self.store.search(query="宠物", now=NOW)["facts"]
        self.assertEqual(["不做宠物类目"], [item["content"] for item in hits])

    def test_budget_truncates_the_injection(self):
        for index in range(6):
            add_shop_rule(self.store, f"宠物类目规则{index}要备货")
        full = self.store.search(query="宠物类目", budget_tokens=10_000, now=NOW)
        tight = self.store.search(query="宠物类目", budget_tokens=20, now=NOW)
        self.assertEqual(6, full["count"])
        self.assertFalse(full["truncated"])
        self.assertLess(tight["count"], full["count"])
        self.assertTrue(tight["truncated"])
        self.assertLessEqual(tight["tokens"], 20)

    def test_budget_always_keeps_at_least_one_fact(self):
        add_shop_rule(self.store, "宠物类目" + "规则" * 50)
        result = self.store.search(query="宠物类目", budget_tokens=1, now=NOW)
        self.assertEqual(1, result["count"])
        self.assertTrue(result["truncated"] or result["count"] >= 1)

    def test_search_does_not_write_to_disk(self):
        add_shop_rule(self.store, "不做宠物类目")
        before = self.store.path.read_bytes()
        self.store.search(query="宠物", budget_tokens=50, now=NOW)
        self.assertEqual(before, self.store.path.read_bytes())

    def test_format_injection_skips_empty_content(self):
        records = [
            {"scope": "shop", "content": "不做宠物类目"},
            {"scope": "product", "content": ""},
            {"scope": "market", "content": "美国站优先"},
        ]
        text = format_injection(records)
        self.assertEqual("- [店铺] 不做宠物类目\n- [市场] 美国站优先", text)
        self.assertEqual("", format_injection([]))

    def test_token_estimate_counts_cjk_by_character(self):
        self.assertEqual(4, estimate_tokens("四个汉字"))
        self.assertGreaterEqual(estimate_tokens("abc def"), 2)

    # ---------------------------------------------------------------- 健壮

    def test_broken_lines_are_skipped_and_counted(self):
        add_shop_rule(self.store, "不做宠物类目")
        with self.store.path.open("a", encoding="utf-8") as handle:
            handle.write("{oops not json\n")
        self.assertEqual(1, len(self.store.list(now=NOW)))
        self.assertEqual(1, self.store.stats(now=NOW)["bad_lines"])

    def test_stats_counts_every_status(self):
        keep = add_shop_rule(self.store, "不做宠物类目")["fact"]
        old = add_shop_rule(self.store, "不做玻璃制品")["fact"]
        self.store.revoke(old["memory_id"])
        add_shop_rule(self.store, "包邮门槛 30 美元", importance=0.2, now=OLD)
        stats = self.store.stats(now=NOW)
        self.assertEqual(3, stats["total"])
        self.assertEqual(1, stats["by_status"][ACTIVE])
        self.assertEqual(1, stats["by_status"][REVOKED])
        self.assertEqual(1, stats["by_status"][EXPIRED])
        self.assertTrue(keep["memory_id"])


class FactsConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.audit = AuditDB(":memory:")
        self.app = make_app(self.root, audit=self.audit)
        self.admin = {"username": "admin", "role": "admin", "tenants": ["globaltrade", "other"]}
        self.operator = {"username": "operator", "role": "operator", "tenants": ["globaltrade"]}

    def tearDown(self):
        self.audit.close()
        self.tmp.cleanup()

    def test_add_then_list_roundtrip(self):
        created = self.app.facts_add(
            self.operator,
            "globaltrade",
            {"content": "不做宠物类目", "scope": "shop", "type": "rule", "source": "user_instruction"},
        )
        self.assertEqual("M-0001", created["fact"]["memory_id"])
        listed = self.app.facts_list(self.operator, "globaltrade")
        self.assertEqual(1, listed["count"])
        self.assertEqual("不做宠物类目", listed["facts"][0]["content"])
        self.assertEqual("店铺", listed["catalog"]["scopes"][0]["label"])

    def test_facts_are_written_under_the_tenant_memory_dir(self):
        self.app.facts_add(
            self.operator, "globaltrade", {"content": "不做宠物类目", "source": "user_instruction"}
        )
        path = self.root / "globaltrade" / "memory" / "facts.jsonl"
        self.assertTrue(path.exists())
        self.assertIn("不做宠物类目", path.read_text(encoding="utf-8"))

    def test_intake_text_field_is_rejected(self):
        with self.assertRaises(ConsoleError) as raised:
            self.app.facts_add(
                self.operator, "globaltrade", {"text": "这20个新品上到美国站"}
            )
        self.assertEqual(400, raised.exception.status)
        self.assertIn("原文", raised.exception.message)

    def test_bad_source_is_a_400(self):
        with self.assertRaises(ConsoleError) as raised:
            self.app.facts_add(
                self.operator, "globaltrade", {"content": "不做宠物类目", "source": "model"}
            )
        self.assertEqual(400, raised.exception.status)

    def test_non_string_content_is_a_400(self):
        with self.assertRaises(ConsoleError) as raised:
            self.app.facts_add(
                self.operator,
                "globaltrade",
                {"content": {"nested": 1}, "source": "user_instruction"},
            )
        self.assertEqual(400, raised.exception.status)

    def test_cross_tenant_access_is_403(self):
        for call in (
            lambda: self.app.facts_list(self.operator, "other"),
            lambda: self.app.facts_add(
                self.operator, "other", {"content": "不做宠物类目", "source": "user_instruction"}
            ),
        ):
            with self.assertRaises(ConsoleError) as raised:
                call()
            self.assertEqual(403, raised.exception.status)

    def test_revoke_unknown_id_is_a_404(self):
        with self.assertRaises(ConsoleError) as raised:
            self.app.facts_revoke(self.operator, "globaltrade", "M-9999")
        self.assertEqual(404, raised.exception.status)

    def test_search_by_query_returns_scored_facts(self):
        self.app.facts_add(
            self.operator, "globaltrade", {"content": "不做宠物类目", "source": "user_instruction"}
        )
        payload = self.app.facts_list(self.operator, "globaltrade", query="宠物")
        self.assertEqual(1, payload["count"])
        self.assertIn("score", payload["facts"][0])
        self.assertEqual(800, payload["budget_tokens"])

    def test_writes_are_audited(self):
        created = self.app.facts_add(
            self.operator, "globaltrade", {"content": "不做宠物类目", "source": "user_instruction"}
        )
        self.app.facts_revoke(self.operator, "globaltrade", created["fact"]["memory_id"])
        actions = [row["action"] for row in self.audit.query(limit=50)]
        self.assertIn("fact.add", actions)
        self.assertIn("fact.revoke", actions)

    def test_intake_preview_never_writes_facts(self):
        self.app.intake_preview(self.operator, "globaltrade", "这20个新品上到美国站")
        self.assertFalse((self.root / "globaltrade" / "memory" / "facts.jsonl").exists())


class FactsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.audit = AuditDB(":memory:")
        self.app = make_app(self.root, audit=self.audit)
        self.server = ConsoleServer(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ""
        self.csrf = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.close()
        self.thread.join(timeout=2)
        self.audit.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection(self.server.host, self.server.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.cookie:
            headers["Cookie"] = self.cookie
        if method != "GET" and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        set_cookie = response.getheader("Set-Cookie")
        if set_cookie and set_cookie.startswith("opspilot_session="):
            self.cookie = set_cookie.split(";", 1)[0]
        conn.close()
        return response.status, json.loads(raw.decode("utf-8"))

    def login(self):
        status, payload = self.request(
            "POST", "/api/login", {"username": "operator", "password": "operator-pass-1"}
        )
        self.assertEqual(200, status, payload)
        self.csrf = payload["csrf"]

    def test_anonymous_access_is_401(self):
        for method, path in (
            ("GET", "/api/tenants/globaltrade/facts"),
            ("POST", "/api/tenants/globaltrade/facts"),
            ("DELETE", "/api/tenants/globaltrade/facts/M-0001"),
        ):
            status, _ = self.request(method, path, {"content": "x"} if method == "POST" else None)
            self.assertEqual(401, status)

    def test_missing_csrf_is_403(self):
        self.login()
        self.csrf = ""
        status, payload = self.request(
            "POST",
            "/api/tenants/globaltrade/facts",
            {"content": "不做宠物类目", "source": "user_instruction"},
        )
        self.assertEqual(403, status)
        self.assertIn("CSRF", payload["error"])

    def test_post_get_delete_roundtrip(self):
        self.login()
        status, created = self.request(
            "POST",
            "/api/tenants/globaltrade/facts",
            {"content": "不做宠物类目", "scope": "shop", "source": "user_instruction"},
        )
        self.assertEqual(200, status, created)
        memory_id = created["fact"]["memory_id"]

        status, listed = self.request("GET", "/api/tenants/globaltrade/facts")
        self.assertEqual(200, status)
        self.assertEqual(1, listed["count"])
        self.assertEqual(1, listed["stats"]["by_status"]["active"])

        status, revoked = self.request(
            "DELETE", f"/api/tenants/globaltrade/facts/{memory_id}?reason=http"
        )
        self.assertEqual(200, status, revoked)
        self.assertTrue(revoked["changed"])

        status, after = self.request("GET", "/api/tenants/globaltrade/facts")
        self.assertEqual(0, after["count"])
        self.assertEqual(1, after["stats"]["by_status"]["revoked"])

    def test_delete_unknown_is_404(self):
        self.login()
        status, _ = self.request("DELETE", "/api/tenants/globaltrade/facts/M-9999")
        self.assertEqual(404, status)

    def test_cross_tenant_is_403(self):
        self.login()
        status, _ = self.request("GET", "/api/tenants/other/facts")
        self.assertEqual(403, status)

    def test_query_parameter_switches_to_search(self):
        self.login()
        self.request(
            "POST",
            "/api/tenants/globaltrade/facts",
            {"content": "不做宠物类目", "source": "user_instruction"},
        )
        status, payload = self.request(
            "GET", "/api/tenants/globaltrade/facts?q=%E5%AE%A0%E7%89%A9&budget=20"
        )
        self.assertEqual(200, status)
        self.assertEqual(1, payload["count"])
        self.assertEqual(20, payload["budget_tokens"])


if __name__ == "__main__":
    unittest.main()


def make_app(root: Path, audit: AuditDB | None = None) -> ConsoleApp:
    users = UserStore(users={}, iterations=1000)
    users.add("admin", "admin-pass-1", "admin", ["globaltrade", "other"], "管理员")
    users.add("operator", "operator-pass-1", "operator", ["globaltrade"], "运营")
    tenants = TenantStore(root / "unused.json", root)
    tenants.tenants = {
        "globaltrade": Tenant("globaltrade", "GlobalTrade", "", "", root / "globaltrade"),
        "other": Tenant("other", "Other Store", "", "", root / "other"),
    }
    for tenant_id in ("globaltrade", "other"):
        directory = root / tenant_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "site_snapshot.json").write_text(
            json.dumps({"store": {"slug": tenant_id}, "products": []}, ensure_ascii=False),
            encoding="utf-8",
        )
    return ConsoleApp(
        users,
        tenants,
        sessions=SessionManager(),
        limiter=LoginLimiter(),
        audit=audit,
    )


if __name__ == "__main__":
    unittest.main()
