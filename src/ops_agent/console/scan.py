"""按需扫描：把「同步 → 体检 → 文案 → 审查」包成一次后台任务。

为什么放在 console 而不是 cli：网页上点一下需要「现在跑到第几步」的状态，
CLI 是一次性、跑完就退出的。但四步本身仍然调用 CLI 里同一批函数，
所以网页点出来的结果和双击 RUN-PIPELINE.bat 完全一致，不存在两套逻辑。

三条硬约束：

1. **不越过审批线**：扫描只跑到合规审查，不发布、不写店铺。
2. **不并发**：同一租户同时只允许一次扫描，否则四步会互相覆盖中间文件。
3. **不撒谎**：每步的真实产出（扫了多少个、选中几个、体检分多少）都落到
   ``scan_state.json``，页面读它，而不是自己猜。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .tenants import Tenant

#: 四步的顺序即页面进度条的顺序，改这里就等于改进度条。
STEPS: tuple[tuple[str, str], ...] = (
    ("sync", "同步店铺快照"),
    ("decide", "体检与选品"),
    ("write", "生成文案方案"),
    ("review", "合规审查"),
)

STATE_FILE = "scan_state.json"
LOCK_FILE = "locks/scan.lock"

#: 心跳超过这么久没更新，说明上一次扫描的进程已经不在了。
BUSY_AFTER_S = 900
#: 页面轮询时，超过这么久没心跳就显示「已中断」，不必等满 15 分钟。
INTERRUPT_AFTER_S = 180
#: 单步输出保留多少字符，够定位报错又不至于把状态文件撑爆。
OUTPUT_LIMIT = 4000


class ScanError(Exception):
    """某一步失败。``output`` 保留该步打印的最后几行，便于直接显示给用户。"""

    def __init__(self, message: str, output: str = "") -> None:
        self.output = output
        tail = _tail(output)
        super().__init__(f"{message}：{tail}" if tail else message)


class ScanBusy(Exception):
    """已有一次扫描在跑。"""


def _tail(text: str, limit: int = 240) -> str:
    for line in reversed([item.strip() for item in (text or "").splitlines()]):
        if line:
            return line[:limit]
    return ""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path, default):
    for attempt in range(3):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            if attempt == 2:
                return default
            time.sleep(0.01)
    return default


def _write_json(path: Path, payload) -> None:
    """先写临时文件再替换，页面永远读不到写了一半的 JSON。

    Windows 有个坑：目标文件正被别的线程读时，``os.replace`` 会抛
    ``PermissionError``（共享冲突，不是权限问题）。页面每 1–2 秒轮询一次
    进度，正好会和后台写盘撞上——不重试的话，一次轮询就能把整轮扫描搞死。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(text, encoding="utf-8")
    for attempt in range(8):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 7:
                break
            time.sleep(0.02 * (attempt + 1))
    # 退到直接覆盖：极少见，但比让整轮扫描崩掉强。
    path.write_text(text, encoding="utf-8")
    temp.unlink(missing_ok=True)


def blank_state() -> dict:
    return {
        "run_id": "",
        "status": "idle",
        "actor": "",
        "started_at": "",
        "finished_at": "",
        "heartbeat_at": "",
        "current": "",
        "steps": [
            {"key": key, "label": label, "status": "pending", "duration_ms": 0,
             "detail": "", "warning": "", "output": ""}
            for key, label in STEPS
        ],
        "summary": {},
        "error": "",
    }


def load_state(directory: Path) -> dict:
    state = _read_json(Path(directory) / STATE_FILE, None)
    if not isinstance(state, dict) or not state.get("steps"):
        return blank_state()
    return state


class _Capture:
    """把扫描线程的 ``print`` 收进缓冲区，其他线程照常输出。

    CLI 的那几个命令是给人看的（会 print 进度和错误）。后台线程里如果直接
    往 stdout 打，一是和服务器日志混在一起，二是失败原因抓不到。这里做一层
    按线程分流的转发：本线程进缓冲区，别的线程原样透传。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self._real: io.TextIOBase | None = None
        self._local = threading.local()

    def __enter__(self) -> io.StringIO:
        with self._lock:
            if self._count == 0:
                self._real = sys.stdout
                sys.stdout = self  # type: ignore[assignment]
            self._count += 1
        buffer = io.StringIO()
        self._local.buffer = buffer
        return buffer

    def __exit__(self, *exc_info) -> None:
        self._local.buffer = None
        with self._lock:
            self._count -= 1
            if self._count <= 0 and self._real is not None:
                sys.stdout = self._real  # type: ignore[assignment]
                self._real = None

    def write(self, text: str) -> int:
        buffer = getattr(self._local, "buffer", None)
        if buffer is not None:
            return buffer.write(text)
        real = self._real or sys.__stdout__
        return real.write(text) if real is not None else len(text)

    def flush(self) -> None:
        real = self._real or sys.__stdout__
        if real is not None:
            try:
                real.flush()
            except (OSError, ValueError):
                pass

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"


_CAPTURE = _Capture()


@dataclass
class StepOutcome:
    """一步跑完的结果：一句话摘要 + 往状态文件里并的键值。"""

    detail: str = ""
    warning: str = ""
    summary: dict = field(default_factory=dict)


class CliCommands:
    """真实执行：调用 CLI 的四个命令，路径全部显式指定到租户目录。

    显式传路径而不是靠 ``OPS_TENANT_DATA_DIR`` 环境变量，是因为环境变量是
    整个进程共用的，两个租户同时扫描会互相串。
    """

    def __init__(self, tenant: Tenant) -> None:
        self.tenant = tenant
        self.directory = Path(tenant.data_dir)
        self.snapshot = self.directory / "site_snapshot.json"
        self.tasks = self.directory / "task_orders.json"
        self.proposals = self.directory / "workshop_proposals.json"
        self.reviews = self.directory / "review_results.json"
        if not tenant.site_base_url:
            raise ScanError("该租户还没有配置店铺地址，无法扫描")
        if tenant.token_env and not (os.environ.get(tenant.token_env) or "").strip():
            raise ScanError(
                f"环境变量 {tenant.token_env} 为空，运营台拿不到店铺 token；"
                "请设置后重启运营台"
            )

    # ---- 四个步骤 --------------------------------------------
    def sync(self) -> StepOutcome:
        args = argparse.Namespace(base_url=self.tenant.site_base_url, out=str(self.snapshot))
        code, output = self._run("cmd_site_sync", args)
        if code != 0:
            raise ScanError("同步店铺快照失败", output)
        snapshot = _read_json(self.snapshot, {})
        products = snapshot.get("products") or []
        return StepOutcome(
            detail=f"同步 {len(products)} 个商品",
            summary={"products": len(products), "fetched_at": snapshot.get("fetched_at", "")},
        )

    def decide(self) -> StepOutcome:
        args = argparse.Namespace(
            base_url=self.tenant.site_base_url,
            offline=False,
            snapshot=None,
            limit="auto",
            days=30,
            planner="auto",
            runtime=None,
            no_memory=False,
            out=str(self.tasks),
        )
        code, output = self._run("cmd_supervisor_run", args)
        if code != 0:
            raise ScanError("体检与选品失败", output)
        payload = _read_json(self.tasks, {})
        health = payload.get("store_health") or {}
        selected = payload.get("selected") or []
        score = health.get("score")
        summary = {
            "scanned": payload.get("scanned"),
            "matched": payload.get("matched"),
            "selected": len(selected),
            "health_score": score,
            "data_as_of": payload.get("data_as_of", ""),
            "planner": payload.get("planner", ""),
        }
        if score is None:
            # 规则引擎不产体检分。这里必须说清楚，否则用户会以为界面坏了。
            return StepOutcome(
                detail=f"选中 {len(selected)} 个商品（本次无体检分）",
                warning="本次走规则引擎，没有体检分；配好 LLM_API_KEY 后重扫即出分",
                summary=summary,
            )
        return StepOutcome(
            detail=f"体检 {score} 分 · 选中 {len(selected)} 个商品", summary=summary
        )

    def write(self) -> StepOutcome:
        args = argparse.Namespace(
            base_url=self.tenant.site_base_url,
            offline=False,
            tasks=str(self.tasks),
            snapshot=None,
            limit="auto",
            market="US",
            planner="auto",
            out=str(self.proposals),
        )
        code, output = self._run("cmd_workshop_run", args)
        if code != 0:
            raise ScanError("生成文案方案失败", output)
        payload = _read_json(self.proposals, {})
        proposals = payload.get("proposals") or []
        variants = sum(len(item.get("variants") or []) for item in proposals)
        failures = payload.get("failures") or []
        warning = f"{len(failures)} 个商品没写出方案" if failures else ""
        return StepOutcome(
            detail=f"{len(proposals)} 个方案包 · {variants} 版文案",
            warning=warning,
            summary={"proposals": len(proposals), "variants": variants},
        )

    def review(self) -> StepOutcome:
        args = argparse.Namespace(
            base_url=self.tenant.site_base_url,
            offline=False,
            proposals=str(self.proposals),
            snapshot=None,
            limit="auto",
            market="US",
            planner="auto",
            out=str(self.reviews),
        )
        code, output = self._run("cmd_review_run", args)
        if code != 0:
            raise ScanError("合规审查失败", output)
        payload = _read_json(self.reviews, {})
        results = payload.get("results") or []
        summary = payload.get("summary") or {}
        blocked = int(summary.get("block") or 0)
        return StepOutcome(
            detail=f"审查 {len(results)} 版 · 拦截 {blocked} 版",
            warning=f"{blocked} 版未通过合规，发布前需要改" if blocked else "",
            summary={"reviews": len(results), "blocked": blocked},
        )

    # ---- 调用 CLI --------------------------------------------
    def _run(self, name: str, args: argparse.Namespace) -> tuple[int, str]:
        # 延迟导入：cli 在模块顶层引用了 console.app，这里提前导入会成环。
        from .. import cli

        with _CAPTURE as buffer:
            try:
                code = int(getattr(cli, name)(args))
            except SystemExit as exc:  # argparse 的报错路径
                code = int(exc.code or 1)
        return code, buffer.getvalue()


class ScanRunner:
    """跑一次完整扫描，并把每一步的真实结果写进 ``scan_state.json``。"""

    def __init__(self, tenant: Tenant, commands, actor: str = "", run_id: str = "") -> None:
        self.tenant = tenant
        self.directory = Path(tenant.data_dir)
        self.commands = commands
        self.actor = actor
        self.run_id = run_id or f"scan-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    def run(self) -> dict:
        state = blank_state()
        state.update(
            {
                "run_id": self.run_id,
                "status": "running",
                "actor": self.actor,
                "started_at": _now(),
                "heartbeat_at": _now(),
            }
        )
        self._save(state)

        for index, (key, _label) in enumerate(STEPS):
            step = state["steps"][index]
            state["current"] = key
            step["status"] = "running"
            step["started_at"] = _now()
            self._save(state)
            started = time.monotonic()
            try:
                outcome = getattr(self.commands, key)()
            except ScanError as exc:
                step["status"] = "failed"
                step["duration_ms"] = int((time.monotonic() - started) * 1000)
                step["detail"] = str(exc)
                step["output"] = (exc.output or "")[-OUTPUT_LIMIT:]
                state.update({"status": "failed", "error": str(exc), "finished_at": _now()})
                state["current"] = ""
                self._save(state)
                return state
            except Exception as exc:  # 未预期错误：留栈到状态文件，别只留一句话
                step["status"] = "failed"
                step["duration_ms"] = int((time.monotonic() - started) * 1000)
                step["detail"] = f"未预期错误：{exc}"
                step["output"] = traceback.format_exc()[-OUTPUT_LIMIT:]
                state.update({"status": "failed", "error": step["detail"], "finished_at": _now()})
                state["current"] = ""
                self._save(state)
                return state

            step["status"] = "done"
            step["duration_ms"] = int((time.monotonic() - started) * 1000)
            step["detail"] = outcome.detail
            step["warning"] = outcome.warning
            state["summary"].update(outcome.summary or {})
            state["heartbeat_at"] = _now()
            self._save(state)

        state["current"] = ""
        state["status"] = "done"
        state["finished_at"] = _now()
        state["heartbeat_at"] = _now()
        self._save(state)
        return state

    def _save(self, state: dict) -> None:
        _write_json(self.directory / STATE_FILE, state)


class ScanManager:
    """进程级的扫描调度：起线程、防重入、回答进度。"""

    def __init__(self, commands_factory=None) -> None:
        self._threads: dict[str, threading.Thread] = {}
        self._guard = threading.Lock()
        self._commands_factory = commands_factory or CliCommands

    def _thread_alive(self, tenant_id: str) -> bool:
        thread = self._threads.get(tenant_id)
        return bool(thread and thread.is_alive())

    def start(self, tenant: Tenant, actor: str = "") -> dict:
        directory = Path(tenant.data_dir)
        with self._guard:
            if self._thread_alive(tenant.tenant_id):
                raise ScanBusy("已有一次扫描正在进行")
            state = load_state(directory)
            if state.get("status") == "running" and self._fresh(state, BUSY_AFTER_S):
                raise ScanBusy("已有一次扫描正在进行")
        commands = self._commands_factory(tenant)
        self._acquire_lock(directory)
        run_id = f"scan-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        runner = ScanRunner(tenant, commands, actor=actor, run_id=run_id)
        try:
            # 先写一份 running 状态再起线程：页面立刻就有反馈，
            # 不用等到第一个模型调用返回才看到进度条。
            state = blank_state()
            state.update(
                {
                    "run_id": run_id,
                    "status": "running",
                    "actor": actor,
                    "started_at": _now(),
                    "heartbeat_at": _now(),
                }
            )
            _write_json(directory / STATE_FILE, state)
            thread = threading.Thread(
                target=self._wrap, args=(runner, directory),
                name=f"scan-{tenant.tenant_id}", daemon=True,
            )
            with self._guard:
                self._threads[tenant.tenant_id] = thread
            thread.start()
        except Exception as exc:
            # 起不来也要落一个失败状态，否则页面会一直显示"扫描中"，
            # 而重试又被 15 分钟的心跳窗口挡住。
            failed = blank_state()
            failed.update(
                {
                    "run_id": run_id,
                    "status": "failed",
                    "actor": actor,
                    "error": f"扫描未能启动：{exc}",
                    "heartbeat_at": _now(),
                    "finished_at": _now(),
                }
            )
            _write_json(directory / STATE_FILE, failed)
            self._release_lock(directory)
            raise
        return state

    def _wrap(self, runner: ScanRunner, directory: Path) -> None:
        try:
            runner.run()
        except Exception:  # run() 自己已兜底，这里只防线程静默死亡
            traceback.print_exc()
        finally:
            self._release_lock(directory)

    def status(self, tenant: Tenant) -> dict:
        directory = Path(tenant.data_dir)
        state = load_state(directory)
        if state.get("status") == "running" and not self._thread_alive(tenant.tenant_id):
            if not self._fresh(state, INTERRUPT_AFTER_S):
                state["status"] = "failed"
                state["error"] = "扫描已中断：运营台可能重启过，请重新扫描"
                for step in state.get("steps") or []:
                    if step.get("status") == "running":
                        step["status"] = "failed"
                        step["detail"] = state["error"]
                _write_json(directory / STATE_FILE, state)
        return state

    @staticmethod
    def _fresh(state: dict, seconds: int) -> bool:
        stamp = state.get("heartbeat_at") or state.get("started_at") or ""
        try:
            seen = datetime.fromisoformat(str(stamp))
        except ValueError:
            return False
        return (datetime.now() - seen).total_seconds() < seconds

    # ---- 跨进程锁 --------------------------------------------
    def _acquire_lock(self, directory: Path) -> None:
        path = directory / LOCK_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(2):
            try:
                handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._stale_lock(path):
                    path.unlink(missing_ok=True)
                    continue
                raise ScanBusy("已有一次扫描正在进行（锁文件未被释放）") from None
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(f"{os.getpid()} {_now()}")
            return
        raise ScanBusy("已有一次扫描正在进行（锁文件未被释放）")

    @staticmethod
    def _stale_lock(path: Path) -> bool:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        return age > BUSY_AFTER_S

    @staticmethod
    def _release_lock(directory: Path) -> None:
        try:
            (directory / LOCK_FILE).unlink(missing_ok=True)
        except OSError:
            pass
