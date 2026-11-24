"""委托台（Step 5）：一句话 → 意图 / 计划预览 / 合约草案。

这一层不执行工具、不写业务数据。测试除了接口形状，还要钉住两件事——
1. 未登录 401、跨租户 403（复用现有 _require_session + tenant_for）；
2. 一次预览跑完，租户目录除 `intake/history.jsonl` 外逐字节不变（含 data/ 与 audit）。

历史单独落盘是本轮修订的验收口径：G7 从"零写入"放宽为"只允许这一处追加"。
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.console.app import (  # noqa: E402
    MAX_INTAKE_TEXT,
    ConsoleApp,
    ConsoleError,
    intake_store_ctx,
    plan_preview,
    tool_catalog,
)
from ops_agent.console.audit import AuditDB  # noqa: E402
from ops_agent.console.auth import LoginLimiter, SessionManager, UserStore  # noqa: E402
from ops_agent.console.server import ConsoleServer  # noqa: E402
from ops_agent.console.tenants import Tenant, TenantStore  # noqa: E402
from ops_agent.intake import Intent  # noqa: E402

# 两个租户各一份小快照：globaltrade 有货，empty 没货，用来测"没候选不编 SKU"。
SNAPSHOT = {
    "store": {"store_id": "s1", "slug": "globaltrade", "name": "GlobalTrade", "country": "CN"},
    "products": [
        {
            "product_id": "p-alpha",
            "title_en": "ANC Wireless Earbuds with Charging Case",
            "status": "draft",
            "variants": [{"variant_id": "v1", "price_cents": 2999, "cost_cents": 1200}],
        },
        {
            "product_id": "p-beta",
            "title_en": "Stainless Steel Water Bottle 1L",
            "status": "draft",
            "variants": [{"variant_id": "v2", "price_cents": 1999, "cost_cents": 800}],
        },
        {
            "product_id": "p-gamma",
            "title_en": "Silicone Baking Mat Set",
            "status": "draft",
            "variants": [{"variant_id": "v3", "price_cents": 1299, "cost_cents": 500}],
        },
    ],
}


def write_snapshot(directory: Path, payload: dict | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "site_snapshot.json").write_text(
        json.dumps(payload if payload is not None else SNAPSHOT, ensure_ascii=False),
        encoding="utf-8",
    )


def make_app(root: Path, audit: AuditDB | None = None) -> ConsoleApp:
    users = UserStore(users={}, iterations=1000)
    users.add("admin", "admin-pass-1", "admin", ["globaltrade", "other"], "管理员")
    users.add("operator", "operator-pass-1", "operator", ["globaltrade"], "运营")
    tenants = TenantStore(root / "unused.json", root)
    tenants.tenants = {
        "globaltrade": Tenant("globaltrade", "GlobalTrade", "", "", root / "globaltrade"),
        "other": Tenant("other", "Other Store", "", "", root / "other"),
    }
    write_snapshot(root / "globaltrade")
    write_snapshot(root / "other")
    return ConsoleApp(
        users,
        tenants,
        sessions=SessionManager(),
        limiter=LoginLimiter(),
        audit=audit,
    )


class IntakeAppTests(unittest.TestCase):
    """直接调 app 方法，不走 HTTP。"""

    def setUp(self):
        # 强制规则引擎：这些断言钉的是意图层的确定性行为，
        # 走大模型会让结果随 key 额度、限流、模型版本漂移（UI 已下线，能力仍要可回归）。
        self._env_patch = mock.patch.dict(os.environ, {"INTAKE_ENGINE": "rules"})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = make_app(self.root)
        self.admin = {"username": "admin", "role": "admin", "tenants": ["globaltrade", "other"]}
        self.operator = {"username": "operator", "role": "operator", "tenants": ["globaltrade"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_batch_upload_returns_intent_and_orders(self):
        payload = self.app.intake_preview(self.admin, "globaltrade", "这20个新品上到美国站")
        self.assertTrue(payload["dry_run"])
        self.assertEqual("BATCH_UPLOAD", payload["intent"])
        self.assertGreaterEqual(payload["confidence"], 0.8)
        self.assertEqual("US", payload["slots"]["market"])
        self.assertEqual(20, payload["slots"]["quantity"])
        # 快照只有 3 条商品：拆单以候选数为上限，不凭空造 20 条。
        self.assertEqual(3, len(payload["task_orders"]))
        self.assertEqual({"intent"}, {order["source"] for order in payload["task_orders"]})
        self.assertEqual({"draft"}, {order["status"] for order in payload["task_orders"]})
        self.assertEqual(1, payload["task_orders"][0]["slots"]["batch_index"])
        self.assertEqual(3, payload["task_orders"][0]["slots"]["batch_size"])
        # 合约键名：task_id / product_id / price_low_cents，不再是 order_id / sku_id。
        self.assertIn("task_id", payload["task_orders"][0])
        self.assertIn("price_low_cents", payload["task_orders"][0])

    def test_every_plan_step_carries_a_risk_level(self):
        payload = self.app.intake_preview(self.admin, "globaltrade", "这20个新品上到美国站")
        steps = payload["plan_preview"]
        self.assertEqual(["file_parse", "listing_draft", "compliance_check"], [s["tool"] for s in steps])
        self.assertEqual(["R0", "R1", "R0"], [s["risk_level"] for s in steps])
        self.assertEqual([1, 2, 3], [s["step"] for s in steps])
        # 未注册的工具标 planned，已注册的标 ready —— 不把"设计里存在"说成"现在能跑"。
        self.assertEqual(["planned", "planned", "ready"], [s["status"] for s in steps])
        self.assertEqual(["planned", "planned", "registry"], [s["source"] for s in steps])

    def test_no_step_reaches_r3(self):
        for text in (
            "这20个新品上到美国站",
            "把这个产品上架到德国站",
            "把 p-alpha 的标题重写一下",
            "帮我诊断一下 p-alpha 为什么转化差",
            "上架并发布到美国站",
        ):
            payload = self.app.intake_preview(self.admin, "globaltrade", text)
            levels = {step["risk_level"] for step in payload["plan_preview"]}
            self.assertNotIn("R3", levels, text)

    def test_plan_preview_blocks_a_tool_above_the_ceiling(self):
        # DATA_QUERY 的上限是 R0；硬塞一个 R3 工具进去，必须显示 blocked 而不是 ready。
        from ops_agent.intake.schema import INTENT_SPECS

        spec = INTENT_SPECS[Intent.DATA_QUERY]
        backup = list(spec["toolset"])
        spec["toolset"] = backup + ["publish_listing", "listing_draft"]
        try:
            steps = plan_preview(Intent.DATA_QUERY, spec["risk_ceiling"])
        finally:
            spec["toolset"] = backup
        status = {step["tool"]: step["status"] for step in steps}
        self.assertEqual("blocked", status["publish_listing"])
        self.assertEqual("blocked", status["listing_draft"])
        self.assertEqual("ready", status["get_metrics"])

    def test_unknown_tool_is_estimated_at_the_ceiling_and_not_ready(self):
        from ops_agent.intake.schema import INTENT_SPECS

        spec = INTENT_SPECS[Intent.DATA_QUERY]
        backup = list(spec["toolset"])
        spec["toolset"] = backup + ["totally_new_tool"]
        try:
            steps = plan_preview(Intent.DATA_QUERY, spec["risk_ceiling"])
        finally:
            spec["toolset"] = backup
        last = steps[-1]
        self.assertEqual("totally_new_tool", last["tool"])
        self.assertEqual("R0", last["risk_level"])
        self.assertEqual("planned", last["status"])
        self.assertEqual("estimated", last["source"])

    def test_clarify_has_empty_plan_and_a_reason(self):
        payload = self.app.intake_preview(self.admin, "globaltrade", "这个产品的价格和销量怎么样")
        self.assertEqual("CLARIFY", payload["intent"])
        self.assertEqual([], payload["plan_preview"])
        self.assertEqual([], payload["task_orders"])
        self.assertTrue(payload["reason"])
        self.assertGreaterEqual(len(payload["candidates"]), 1)

    def test_unknown_text_is_not_executed(self):
        payload = self.app.intake_preview(self.admin, "globaltrade", "今天天气不错")
        self.assertEqual("UNKNOWN", payload["intent"])
        self.assertEqual([], payload["task_orders"])
        self.assertEqual("none", payload["artifact"])

    def test_single_intent_without_sku_is_refused_not_guessed(self):
        payload = self.app.intake_preview(self.admin, "globaltrade", "把这个产品上架到德国站")
        self.assertEqual("SINGLE_UPLOAD", payload["intent"])
        self.assertEqual([], payload["task_orders"])
        self.assertIn("指定商品", payload["reason"])

    def test_pasted_product_id_binds_that_product(self):
        payload = self.app.intake_preview(self.admin, "globaltrade", "把 p-beta 的标题重写一下")
        self.assertEqual(1, len(payload["task_orders"]))
        self.assertEqual("p-beta", payload["task_orders"][0]["product_id"])

    def test_assumed_slots_are_declared(self):
        payload = self.app.intake_preview(self.admin, "globaltrade", "这20个新品上到美国站")
        # 语言是补的；市场是原话里的"美国站"，不算假设。
        self.assertEqual({"language": True}, payload["assumed"])
        self.assertNotIn("market", payload["assumed"])
        self.assertIn("source_path", payload["pending_input"])

    def test_empty_and_blank_text_is_400(self):
        for text in ("", "   ", "\n\t "):
            with self.assertRaises(ConsoleError) as raised:
                self.app.intake_preview(self.admin, "globaltrade", text)
            self.assertEqual(400, raised.exception.status)

    def test_too_long_text_is_rejected_not_truncated(self):
        text = "这20个新品上到美国站" + "。" * MAX_INTAKE_TEXT
        with self.assertRaises(ConsoleError) as raised:
            self.app.intake_preview(self.admin, "globaltrade", text)
        self.assertEqual(400, raised.exception.status)
        self.assertIn(str(MAX_INTAKE_TEXT), raised.exception.message)

    def test_cross_tenant_preview_is_403(self):
        with self.assertRaises(ConsoleError) as raised:
            self.app.intake_preview(self.operator, "other", "这20个新品上到美国站")
        self.assertEqual(403, raised.exception.status)

    def test_unknown_tenant_is_404(self):
        with self.assertRaises(ConsoleError) as raised:
            self.app.intake_preview(self.admin, "nope", "这20个新品上到美国站")
        self.assertEqual(404, raised.exception.status)

    def test_history_is_empty_before_any_intake(self):
        payload = self.app.intake_history(self.admin, "globaltrade")
        self.assertEqual([], payload["history"])
        self.assertEqual(0, payload["count"])
        self.assertIn("只增不改", payload["note"])

    def test_history_records_the_preview_in_its_own_file(self):
        text = "这20个新品上到美国站"
        payload = self.app.intake_preview(self.admin, "globaltrade", text)
        self.assertTrue(payload["history_recorded"])
        history = self.app.intake_history(self.admin, "globaltrade")
        self.assertEqual(1, history["count"])
        entry = history["history"][0]
        self.assertEqual(text, entry["text"])
        self.assertEqual("BATCH_UPLOAD", entry["intent"])
        self.assertEqual("admin", entry["actor"])
        self.assertEqual("globaltrade", entry["tenant_id"])
        self.assertEqual(3, entry["order_count"])
        self.assertEqual(3, len(entry["task_ids"]))
        self.assertTrue(entry["history_id"].startswith("H-"))
        # 落盘位置独立：租户目录下 intake/history.jsonl，不碰 memory/。
        self.assertTrue((self.root / "globaltrade" / "intake" / "history.jsonl").exists())
        self.assertFalse((self.root / "globaltrade" / "memory" / "facts.jsonl").exists())

    def test_history_is_append_only_and_newest_first(self):
        for text in ("这20个新品上到美国站", "把 p-alpha 的标题重写一下", "今天天气不错"):
            self.app.intake_preview(self.admin, "globaltrade", text)
        entries = self.app.intake_history(self.admin, "globaltrade")["history"]
        self.assertEqual(3, len(entries))
        self.assertEqual("今天天气不错", entries[0]["text"])  # 新的在前
        self.assertEqual("这20个新品上到美国站", entries[-1]["text"])
        raw = (self.root / "globaltrade" / "intake" / "history.jsonl").read_text(encoding="utf-8")
        self.assertEqual(3, len([line for line in raw.splitlines() if line.strip()]))

    def test_history_is_scoped_to_the_tenant(self):
        self.app.intake_preview(self.admin, "globaltrade", "这20个新品上到美国站")
        self.assertEqual(1, self.app.intake_history(self.admin, "globaltrade")["count"])
        self.assertEqual(0, self.app.intake_history(self.admin, "other")["count"])
        with self.assertRaises(ConsoleError) as raised:
            self.app.intake_history(self.operator, "other")
        self.assertEqual(403, raised.exception.status)

    def test_history_write_failure_does_not_break_the_preview(self):
        from unittest.mock import patch

        with patch("ops_agent.console.app.append_history", return_value=None):
            payload = self.app.intake_preview(self.admin, "globaltrade", "这20个新品上到美国站")
        self.assertEqual(3, len(payload["task_orders"]))
        self.assertFalse(payload["history_recorded"])
        self.assertEqual(0, self.app.intake_history(self.admin, "globaltrade")["count"])

    def test_preview_only_writes_the_history_file(self):
        """G7 修订后：唯一允许的写入是 intake/history.jsonl，其它文件必须逐字节不变。"""

        def fingerprint() -> dict:
            return {
                str(path.relative_to(self.root)): path.read_bytes()
                for path in sorted(self.root.rglob("*"))
                if path.is_file() and path.name != "history.jsonl"
            }

        before = fingerprint()
        for text in ("这20个新品上到美国站", "把 p-alpha 的标题重写一下", "今天天气不错"):
            self.app.intake_preview(self.admin, "globaltrade", text)
        self.assertEqual(before, fingerprint())

    def test_preview_does_not_call_any_tool(self):
        from unittest.mock import patch

        def explode(*args, **kwargs):
            raise AssertionError("预览不该调用任何工具")

        # 工具函数是真的、注册表也是真的，这里只把"被调用"这件事变成异常：
        # 预览走的是 mapping 层，压根不该碰到 tool 函数。
        with patch("ops_agent.tools.get_product", explode), patch(
            "ops_agent.tools.get_product_metrics", explode
        ), patch("ops_agent.tools.generate_listing", explode):
            payload = self.app.intake_preview(self.admin, "globaltrade", "把 p-alpha 的标题重写一下")
        self.assertEqual(1, len(payload["task_orders"]))

    def test_preview_is_deterministic(self):
        first = self.app.intake_preview(self.admin, "globaltrade", "这20个新品上到美国站")
        second = self.app.intake_preview(self.admin, "globaltrade", "这20个新品上到美国站")
        self.assertEqual(json.dumps(first, ensure_ascii=False), json.dumps(second, ensure_ascii=False))


class StoreContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_snapshot_gives_no_products(self):
        ctx, notes = intake_store_ctx(self.root)
        self.assertEqual([], ctx["products"])
        self.assertTrue(notes)
        self.assertEqual("US", ctx["default_market"])

    def test_empty_snapshot_is_reported(self):
        write_snapshot(self.root, {"store": {"slug": "x", "country": "CN"}, "products": []})
        ctx, notes = intake_store_ctx(self.root)
        self.assertEqual([], ctx["products"])
        self.assertTrue(any("没有商品候选" in note for note in notes))

    def test_seller_country_is_not_treated_as_target_market(self):
        write_snapshot(self.root, {"store": {"slug": "x", "country": "CN"}, "products": []})
        ctx, notes = intake_store_ctx(self.root)
        self.assertEqual("US", ctx["default_market"])
        self.assertTrue(any("卖家所在地" in note for note in notes))

    def test_product_without_variants_keeps_zero_price(self):
        write_snapshot(
            self.root,
            {
                "store": {"slug": "x", "country": "US"},
                "products": [{"product_id": "p1", "title_en": "Thing", "status": "draft", "variants": []}],
            },
        )
        ctx, _ = intake_store_ctx(self.root)
        self.assertEqual(0, ctx["products"][0]["price_cents"])
        self.assertIsNone(ctx["products"][0]["cost_cents"])


class ToolCatalogTests(unittest.TestCase):
    def test_catalog_merges_both_registries(self):
        catalog = tool_catalog()
        # 工坊侧
        self.assertIn("generate_listing", catalog)
        self.assertIn("publish_listing", catalog)
        # research 侧
        self.assertIn("get_metrics", catalog)
        self.assertIn("list_products", catalog)

    def test_forbidden_tools_are_r3_in_the_catalog(self):
        catalog = tool_catalog()
        self.assertEqual("R3", catalog["publish_listing"]["risk_level"])
        self.assertEqual("R3", catalog["rollback"]["risk_level"])


class ConsoleIntakeHTTPTests(unittest.TestCase):
    def setUp(self):
        # 控制台活路径走 classify_auto（大模型优先）；测试必须离线，
        # 强制 rules 引擎，绝不允许测试去打真实 API。
        self._env_patch = mock.patch.dict(os.environ, {"INTAKE_ENGINE": "rules"})
        self._env_patch.start()
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
        self._env_patch.stop()
        self.server.shutdown()
        self.server.close()
        self.thread.join(timeout=2)
        self.audit.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(self.server.host, self.server.port, timeout=5)
        request_headers = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if self.cookie:
            request_headers["Cookie"] = self.cookie
        if method != "GET" and self.csrf:
            request_headers.setdefault("X-CSRF-Token", self.csrf)
        conn.request(method, path, body=payload, headers=request_headers)
        response = conn.getresponse()
        raw = response.read()
        set_cookie = response.getheader("Set-Cookie")
        if set_cookie and set_cookie.startswith("opspilot_session="):
            self.cookie = set_cookie.split(";", 1)[0]
        conn.close()
        return response.status, json.loads(raw.decode("utf-8"))

    def login(self, username, password):
        status, payload = self.request(
            "POST", "/api/login", {"username": username, "password": password}
        )
        self.assertEqual(200, status, payload)
        self.csrf = payload["csrf"]
        return payload

    def test_unauthenticated_intake_is_401(self):
        status, payload = self.request("POST", "/api/tenants/globaltrade/intake", {"text": "上架"})
        self.assertEqual(401, status)
        self.assertFalse(payload["ok"])

    def test_unauthenticated_history_is_401(self):
        status, _ = self.request("GET", "/api/tenants/globaltrade/intake/history")
        self.assertEqual(401, status)

    def test_operator_can_preview_own_tenant(self):
        self.login("operator", "operator-pass-1")
        status, payload = self.request(
            "POST", "/api/tenants/globaltrade/intake", {"text": "这20个新品上到美国站"}
        )
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual("BATCH_UPLOAD", payload["intent"])
        for key in (
            "confidence",
            "slots",
            "missing",
            "candidates",
            "plan_preview",
            "task_orders",
            "risk_ceiling",
            "artifact",
        ):
            self.assertIn(key, payload)
        self.assertEqual("R1", payload["risk_ceiling"])
        self.assertTrue(all(step["risk_level"] for step in payload["plan_preview"]))

    def test_cross_tenant_intake_is_403(self):
        self.login("operator", "operator-pass-1")
        status, payload = self.request(
            "POST", "/api/tenants/other/intake", {"text": "这20个新品上到美国站"}
        )
        self.assertEqual(403, status)
        self.assertFalse(payload["ok"])

    def test_cross_tenant_history_is_403(self):
        self.login("operator", "operator-pass-1")
        status, _ = self.request("GET", "/api/tenants/other/intake/history")
        self.assertEqual(403, status)

    def test_history_returns_an_empty_list_before_any_intake(self):
        self.login("operator", "operator-pass-1")
        status, payload = self.request("GET", "/api/tenants/globaltrade/intake/history")
        self.assertEqual(200, status)
        self.assertEqual([], payload["history"])

    def test_history_returns_what_the_preview_recorded(self):
        self.login("operator", "operator-pass-1")
        self.request("POST", "/api/tenants/globaltrade/intake", {"text": "这20个新品上到美国站"})
        status, payload = self.request("GET", "/api/tenants/globaltrade/intake/history")
        self.assertEqual(200, status)
        self.assertEqual(1, payload["count"])
        self.assertEqual("BATCH_UPLOAD", payload["history"][0]["intent"])
        self.assertEqual("operator", payload["history"][0]["actor"])

    def test_missing_csrf_is_403(self):
        self.login("operator", "operator-pass-1")
        self.csrf = ""
        status, payload = self.request(
            "POST", "/api/tenants/globaltrade/intake", {"text": "这20个新品上到美国站"}
        )
        self.assertEqual(403, status)
        self.assertIn("CSRF", payload["error"])

    def test_non_string_text_is_400(self):
        self.login("operator", "operator-pass-1")
        status, payload = self.request(
            "POST", "/api/tenants/globaltrade/intake", {"text": {"nested": True}}
        )
        self.assertEqual(400, status)
        self.assertIn("字符串", payload["error"])

    def test_empty_text_is_400(self):
        self.login("operator", "operator-pass-1")
        status, _ = self.request("POST", "/api/tenants/globaltrade/intake", {"text": "  "})
        self.assertEqual(400, status)

    def test_unknown_intake_subpath_is_404(self):
        self.login("operator", "operator-pass-1")
        status, _ = self.request("GET", "/api/tenants/globaltrade/intake/nope")
        self.assertEqual(404, status)

    def test_preview_does_not_write_an_audit_row(self):
        self.login("operator", "operator-pass-1")
        before = len(self.audit.query(limit=500))
        self.request("POST", "/api/tenants/globaltrade/intake", {"text": "这20个新品上到美国站"})
        after = len(self.audit.query(limit=500))
        self.assertEqual(before, after)

    def test_preview_leaves_the_tenant_directory_untouched_except_history(self):
        self.login("operator", "operator-pass-1")

        def fingerprint() -> dict:
            return {
                str(path.relative_to(self.root)): path.read_bytes()
                for path in sorted(self.root.rglob("*"))
                if path.is_file() and path.name != "history.jsonl"
            }

        before = fingerprint()
        self.request("POST", "/api/tenants/globaltrade/intake", {"text": "这20个新品上到美国站"})
        self.assertEqual(before, fingerprint())
        self.assertTrue((self.root / "globaltrade" / "intake" / "history.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
