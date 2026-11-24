"""按需扫描的后端测试：四步串联、失败中止、防重入、中断识别。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import http.client
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.console.app import ConsoleApp, ConsoleError  # noqa: E402
from ops_agent.console.audit import AuditDB  # noqa: E402
from ops_agent.console.auth import UserStore  # noqa: E402
from ops_agent.console.server import ConsoleServer  # noqa: E402
from ops_agent.console.scan import (  # noqa: E402
    STEPS,
    CliCommands,
    ScanBusy,
    ScanError,
    ScanManager,
    ScanRunner,
    StepOutcome,
    _write_json,
    blank_state,
    load_state,
)
from ops_agent.console.tenants import Tenant, TenantStore  # noqa: E402


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class FakeCommands:
    """四步的替身：每一步都写一份真实形状的文件，用来验证摘要读取。"""

    def __init__(self, directory, *, fail_at: str = "", delay: float = 0.0, score=61) -> None:
        self.directory = Path(directory)
        self.fail_at = fail_at
        self.delay = delay
        self.score = score
        self.calls: list[str] = []

    def _tick(self, key: str) -> None:
        self.calls.append(key)
        if self.delay:
            time.sleep(self.delay)
        if self.fail_at == key:
            raise ScanError(f"{key} 失败", "Traceback ...\n接口错误: 401 unauthorized")

    def sync(self) -> StepOutcome:
        self._tick("sync")
        write_json(
            self.directory / "site_snapshot.json",
            {"products": [{}, {}, {}], "fetched_at": "2026-11-16T10:00:00"},
        )
        return StepOutcome(detail="同步 3 个商品", summary={"products": 3})

    def decide(self) -> StepOutcome:
        self._tick("decide")
        write_json(
            self.directory / "task_orders.json",
            {
                "scanned": 3,
                "matched": 2,
                "selected": [{}, {}],
                "store_health": {"score": self.score},
                "data_as_of": "2026-11-16",
            },
        )
        return StepOutcome(
            detail=f"体检 {self.score} 分 · 选中 2 个商品",
            summary={"selected": 2, "health_score": self.score},
        )

    def write(self) -> StepOutcome:
        self._tick("write")
        write_json(
            self.directory / "workshop_proposals.json",
            {"proposals": [{"variants": [{}, {}, {}]}, {"variants": [{}, {}]}]},
        )
        return StepOutcome(detail="2 个方案包 · 5 版文案", summary={"proposals": 2, "variants": 5})

    def review(self) -> StepOutcome:
        self._tick("review")
        write_json(
            self.directory / "review_results.json",
            {"results": [{}, {}, {}], "summary": {"block": 1, "warn": 0, "pass": 2}},
        )
        return StepOutcome(
            detail="审查 3 版 · 拦截 1 版",
            warning="1 版未通过合规，发布前需要改",
            summary={"reviews": 3, "blocked": 1},
        )


def tenant_for(directory: Path, token_env: str = "") -> Tenant:
    return Tenant(
        "globaltrade",
        "GlobalTrade",
        "http://127.0.0.1:3000/api/agent",
        token_env,
        Path(directory),
    )


class ScanRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.tenant = tenant_for(self.directory)

    def tearDown(self):
        self.temp.cleanup()

    def test_all_four_steps_done_and_summary_merged(self):
        commands = FakeCommands(self.directory)
        state = ScanRunner(self.tenant, commands).run()

        self.assertEqual(state["status"], "done")
        self.assertEqual([step["status"] for step in state["steps"]], ["done"] * 4)
        self.assertEqual([step["key"] for step in state["steps"]], [key for key, _ in STEPS])
        self.assertEqual(commands.calls, ["sync", "decide", "write", "review"])
        self.assertEqual(state["summary"]["health_score"], 61)
        self.assertEqual(state["summary"]["variants"], 5)
        self.assertEqual(state["summary"]["blocked"], 1)
        # 落盘的那份和内存里的一致：页面读的是文件，不是返回值。
        self.assertEqual(load_state(self.directory)["status"], "done")

    def test_failure_stops_chain_and_keeps_output_tail(self):
        commands = FakeCommands(self.directory, fail_at="decide")
        state = ScanRunner(self.tenant, commands).run()

        self.assertEqual(state["status"], "failed")
        self.assertEqual([step["status"] for step in state["steps"]],
                         ["done", "failed", "pending", "pending"])
        self.assertEqual(commands.calls, ["sync", "decide"])  # 后两步不该跑
        self.assertIn("401", state["error"])
        self.assertIn("decide 失败", state["steps"][1]["detail"])
        self.assertIn("401", state["steps"][1]["output"])
        self.assertEqual(state["finished_at"] != "", True)

    def test_state_file_is_valid_json_during_run(self):
        # 原子写：中途任何时刻读到的都是完整 JSON，不会读到半截。
        commands = FakeCommands(self.directory, delay=0.05)
        runner = ScanRunner(self.tenant, commands)
        state = runner.run()
        self.assertEqual(state["status"], "done")
        raw = (self.directory / "scan_state.json").read_text(encoding="utf-8")
        self.assertIsInstance(json.loads(raw), dict)


class CliCommandsTests(unittest.TestCase):
    """真实命令封装：只把 CLI 调用本身打桩，摘要与告警逻辑照跑。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.tenant = tenant_for(self.directory)
        self.commands = CliCommands(self.tenant)

    def tearDown(self):
        self.temp.cleanup()

    def test_rule_engine_without_health_score_is_flagged(self):
        def fake_run(name, args):
            self.assertEqual(name, "cmd_supervisor_run")
            self.assertEqual(args.planner, "auto")  # 必须走分流，不能写死 rule
            write_json(
                self.directory / "task_orders.json",
                {"scanned": 3, "matched": 2, "selected": [{}, {}]},
            )
            return 0, "选中 2 个"

        self.commands._run = fake_run
        outcome = self.commands.decide()
        self.assertIsNone(outcome.summary["health_score"])
        self.assertIn("没有体检分", outcome.warning)

    def test_nonzero_exit_becomes_scan_error_with_tail(self):
        def fake_run(name, args):
            return 1, "读取评价原文：0 个商品\n接口错误: 401 unauthorized"

        self.commands._run = fake_run
        with self.assertRaises(ScanError) as caught:
            self.commands.sync()
        self.assertIn("同步店铺快照失败", str(caught.exception))
        self.assertIn("401", str(caught.exception))

    def test_each_step_passes_explicit_paths(self):
        seen = {}

        def fake_run(name, args):
            seen[name] = args
            if name == "cmd_site_sync":
                write_json(self.directory / "site_snapshot.json", {"products": [{}]})
            elif name == "cmd_supervisor_run":
                write_json(self.directory / "task_orders.json", {"selected": [], "store_health": {"score": 60}})
            elif name == "cmd_workshop_run":
                write_json(self.directory / "workshop_proposals.json", {"proposals": []})
            else:
                write_json(self.directory / "review_results.json", {"results": []})
            return 0, ""

        self.commands._run = fake_run
        self.commands.sync()
        self.commands.decide()
        self.commands.write()
        self.commands.review()

        self.assertEqual(Path(seen["cmd_site_sync"].out), self.directory / "site_snapshot.json")
        self.assertEqual(Path(seen["cmd_supervisor_run"].out), self.directory / "task_orders.json")
        self.assertEqual(Path(seen["cmd_workshop_run"].tasks), self.directory / "task_orders.json")
        self.assertEqual(Path(seen["cmd_workshop_run"].out), self.directory / "workshop_proposals.json")
        self.assertEqual(Path(seen["cmd_review_run"].proposals), self.directory / "workshop_proposals.json")
        self.assertEqual(Path(seen["cmd_review_run"].out), self.directory / "review_results.json")


class AtomicWriteTests(unittest.TestCase):
    """状态文件的写入必须扛得住「页面正在读」这种并发。"""

    def test_windows_sharing_violation_is_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "scan_state.json"
            real_replace = os.replace
            calls = {"count": 0}

            def flaky(src, dst):
                calls["count"] += 1
                if calls["count"] <= 3:  # 模拟读句柄没释放的瞬间
                    raise PermissionError(5, "拒绝访问")
                return real_replace(src, dst)

            with patch("ops_agent.console.scan.os.replace", flaky):
                _write_json(path, {"ok": True})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": True})
            self.assertGreaterEqual(calls["count"], 4)
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])

    def test_persistent_failure_still_lands_the_data(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "scan_state.json"

            def always_denied(src, dst):
                raise PermissionError(5, "拒绝访问")

            with patch("ops_agent.console.scan.os.replace", always_denied):
                _write_json(path, {"status": "done"})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "done")
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])

    def test_polling_while_writing_does_not_kill_the_run(self):
        # 回归：以前 status() 的读会和写盘撞上，PermissionError 直接掀掉整轮扫描。
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            tenant = tenant_for(directory)
            manager = ScanManager(lambda item: FakeCommands(item.data_dir, delay=0.02))
            thread = threading.Thread(
                target=lambda: [manager.status(tenant) for _ in range(200)], daemon=True
            )
            thread.start()
            state = ScanRunner(tenant, FakeCommands(directory, delay=0.02)).run()
            thread.join(timeout=3)
            self.assertEqual(state["status"], "done")


class ScanManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.tenant = tenant_for(self.directory)
        self.manager = ScanManager(lambda tenant: FakeCommands(tenant.data_dir))

    def tearDown(self):
        self.temp.cleanup()

    def wait(self, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.manager.status(self.tenant)
            if state["status"] != "running":
                return state
            time.sleep(0.02)
        self.fail("扫描没有在预期时间内结束")

    def test_second_start_is_rejected_while_running(self):
        self.manager._commands_factory = lambda tenant: FakeCommands(tenant.data_dir, delay=0.2)
        self.manager.start(self.tenant, actor="admin")
        with self.assertRaises(ScanBusy):
            self.manager.start(self.tenant, actor="admin")
        self.wait()

    def test_foreign_lock_file_blocks(self):
        lock = self.directory / "locks" / "scan.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("99999 2026-11-16T10:00:00", encoding="utf-8")
        with self.assertRaises(ScanBusy):
            self.manager.start(self.tenant, actor="admin")

    def test_stale_lock_is_taken_over(self):
        lock = self.directory / "locks" / "scan.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("99999 2026-11-16T00:00:00", encoding="utf-8")
        old = time.time() - 3600
        os.utime(lock, (old, old))
        self.manager.start(self.tenant, actor="admin")
        state = self.wait()
        self.assertEqual(state["status"], "done")
        self.assertFalse(lock.exists())  # 跑完要释放

    def test_status_marks_interrupted_after_restart(self):
        state = blank_state()
        stamp = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        state.update({"status": "running", "started_at": stamp, "heartbeat_at": stamp,
                      "steps": [dict(step, status="running" if index == 0 else "pending")
                                for index, step in enumerate(state["steps"])]})
        write_json(self.directory / "scan_state.json", state)

        result = self.manager.status(self.tenant)
        self.assertEqual(result["status"], "failed")
        self.assertIn("中断", result["error"])
        self.assertEqual(load_state(self.directory)["status"], "failed")

    def test_fresh_running_state_is_not_called_interrupted(self):
        state = blank_state()
        stamp = datetime.now().isoformat(timespec="seconds")
        state.update({"status": "running", "started_at": stamp, "heartbeat_at": stamp})
        write_json(self.directory / "scan_state.json", state)
        self.assertEqual(self.manager.status(self.tenant)["status"], "running")

    def test_idle_when_no_state_file(self):
        result = self.manager.status(self.tenant)
        self.assertEqual(result["status"], "idle")
        self.assertEqual(len(result["steps"]), 4)

    def test_missing_token_is_a_clear_error(self):
        tenant = tenant_for(self.directory, token_env="SCAN_TEST_TOKEN_NOT_SET")
        manager = ScanManager()  # 默认工厂 = 真实 CliCommands
        with self.assertRaises(ScanError) as caught:
            manager.start(tenant, actor="admin")
        self.assertIn("SCAN_TEST_TOKEN_NOT_SET", str(caught.exception))

    def test_tenant_without_site_is_rejected(self):
        tenant = Tenant("globaltrade", "GT", "", "", self.directory)
        with self.assertRaises(ScanError):
            ScanManager().start(tenant, actor="admin")


class ScanAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.users = UserStore(users={}, iterations=1000)
        self.users.add("admin", "admin-pass-1", "admin", ["globaltrade"], "管理员")

    def tearDown(self):
        self.temp.cleanup()

    def make_app(self, *, demo: bool = False, factory=None) -> ConsoleApp:
        tenants = TenantStore(self.directory / "unused.json", self.directory)
        tenants.tenants = {"globaltrade": tenant_for(self.directory)}
        return ConsoleApp(
            self.users,
            tenants,
            audit=AuditDB(":memory:"),
            demo=demo,
            scan_factory=factory or (lambda tenant: FakeCommands(tenant.data_dir)),
        )

    def wait(self, app: ConsoleApp, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = app.scan_status(self.users.get("admin"), "globaltrade")["scan"]
            if state["status"] != "running":
                return state
            time.sleep(0.02)
        self.fail("扫描没有在预期时间内结束")

    def seed_proposals(self, count: int = 2) -> None:
        write_json(
            self.directory / "workshop_proposals.json",
            {"proposals": [{"product_id": f"p{index}"} for index in range(count)]},
        )

    def test_status_reports_pending_counts(self):
        app = self.make_app()
        self.seed_proposals(2)
        write_json(
            self.directory / "publish_results.json",
            {"results": [{"product_id": "p0", "status": "published"}]},
        )
        payload = app.scan_status(self.users.get("admin"), "globaltrade")
        self.assertEqual(payload["pending"], {"proposals": 2, "unpublished": 1})
        self.assertEqual(payload["scan"]["status"], "idle")

    def test_unpublished_proposals_require_force(self):
        app = self.make_app()
        self.seed_proposals(3)
        with self.assertRaises(ConsoleError) as caught:
            app.scan_start(self.users.get("admin"), "globaltrade")
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("3 个方案", caught.exception.message)

    def test_force_starts_scan_and_finishes(self):
        app = self.make_app()
        self.seed_proposals(1)
        result = app.scan_start(self.users.get("admin"), "globaltrade", force=True)
        self.assertEqual(result["scan"]["status"], "running")
        state = self.wait(app)
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["summary"]["health_score"], 61)

    def test_demo_mode_is_read_only(self):
        app = self.make_app(demo=True)
        with self.assertRaises(ConsoleError) as caught:
            app.scan_start(self.users.get("admin"), "globaltrade", force=True)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("演示模式", caught.exception.message)

    def test_unknown_tenant_is_404(self):
        app = self.make_app()
        with self.assertRaises(ConsoleError) as caught:
            app.scan_status(self.users.get("admin"), "nope")
        self.assertEqual(caught.exception.status, 404)


class ScanHttpTests(unittest.TestCase):
    """走真实 HTTP：确认两个路由挂在正确的位置，且 CSRF 没有被绕开。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.users = UserStore(users={}, iterations=1000)
        self.users.add("admin", "admin-pass-1", "admin", ["globaltrade"], "管理员")
        tenants = TenantStore(self.directory / "unused.json", self.directory)
        tenants.tenants = {"globaltrade": tenant_for(self.directory)}
        self.app = ConsoleApp(
            self.users,
            tenants,
            audit=AuditDB(":memory:"),
            scan_factory=lambda tenant: FakeCommands(tenant.data_dir),
        )
        self.server = ConsoleServer(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ""
        self.csrf = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.close()
        self.thread.join(timeout=2)
        self.app.audit.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(self.server.host, self.server.port, timeout=5)
        request_headers = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if self.cookie:
            request_headers["Cookie"] = self.cookie
        conn.request(method, path, body=payload, headers=request_headers)
        response = conn.getresponse()
        raw = response.read()
        set_cookie = response.getheader("Set-Cookie")
        if set_cookie and set_cookie.startswith("opspilot_session="):
            self.cookie = set_cookie.split(";", 1)[0]
        conn.close()
        return response.status, json.loads(raw.decode("utf-8"))

    def login(self):
        status, payload = self.request(
            "POST", "/api/login", {"username": "admin", "password": "admin-pass-1"}
        )
        self.assertEqual(status, 200, payload)
        self.csrf = payload["csrf"]

    def wait(self, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.app.scan_status(self.users.get("admin"), "globaltrade")["scan"]
            if state["status"] != "running":
                return state
            time.sleep(0.02)
        self.fail("扫描没有在预期时间内结束")

    def test_status_requires_login(self):
        status, _ = self.request("GET", "/api/tenants/globaltrade/scan/status")
        self.assertEqual(status, 401)

    def test_status_route_returns_idle(self):
        self.login()
        status, payload = self.request("GET", "/api/tenants/globaltrade/scan/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["scan"]["status"], "idle")
        self.assertEqual(len(payload["scan"]["steps"]), 4)
        self.assertIn("unpublished", payload["pending"])

    def test_post_without_csrf_is_rejected(self):
        self.login()
        status, payload = self.request("POST", "/api/tenants/globaltrade/scan", {"force": True})
        self.assertEqual(status, 403)
        self.assertIn("CSRF", payload["error"])

    def test_post_starts_scan_and_status_follows(self):
        self.login()
        status, payload = self.request(
            "POST",
            "/api/tenants/globaltrade/scan",
            {"force": True},
            {"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["scan"]["status"], "running")
        state = self.wait()
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["summary"]["products"], 3)

    def test_second_post_while_running_is_409(self):
        self.app.scans._commands_factory = lambda tenant: FakeCommands(tenant.data_dir, delay=0.3)
        self.login()
        headers = {"X-CSRF-Token": self.csrf}
        status, payload = self.request(
            "POST", "/api/tenants/globaltrade/scan", {"force": True}, headers
        )
        self.assertEqual(status, 200, payload)
        status, payload = self.request(
            "POST", "/api/tenants/globaltrade/scan", {"force": True}, headers
        )
        self.assertEqual(status, 409)
        self.wait()


if __name__ == "__main__":
    unittest.main()
