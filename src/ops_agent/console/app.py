"""Application service for the OpsPilot console.

The HTTP layer stays thin; tests can call these methods directly.
"""

from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ..approval_store import ApprovalStore
from ..config import TRACE_DIR
from ..experiment import ExperimentRegistrar
from ..intake import Intent, classify_auto
from ..intake.history import append_history, read_history
from ..intake.mapping import plan_intent
from ..intake.schema import INTENT_SPECS
from ..memory import MemoryStore
from ..memory.facts import (
    DEFAULT_IMPORTANCE,
    MAX_REASON,
    SCOPES,
    SCOPE_LABELS,
    SOURCE_CONFIDENCE,
    SOURCE_LABELS,
    TYPES,
    TYPE_LABELS,
    FactsStore,
)
from ..models import jsonable
from ..publish import PublishOrchestrator, load_verdicts
from ..site_platform import SiteAPIError, SiteProduct
from ..tools import build_registry, build_research_registry
from .audit import AuditDB
from .auth import LoginLimiter, SessionManager, UserStore
from .connectors import ConnectorError, build_site
from .insight import (
    attach_verdicts,
    build_checkup,
    build_experiment_board,
    build_memory,
    build_metrics,
    replay_snapshot,
)
from .scan import ScanBusy, ScanError, ScanManager
from .tenants import Tenant, TenantStore


class ConsoleError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _snapshot_baseline(directory: Path, product_id: str) -> dict:
    """Baseline for a console publish, read from the tenant's last snapshot.

    The console has no daily series for a site product, so this is a 30-day
    roll-up; the record marks it as such and the judge treats it accordingly.
    """
    snapshot = _read_json(directory / "site_snapshot.json", {})
    for item in snapshot.get("products") or []:
        if str(item.get("product_id")) != str(product_id):
            continue
        metrics = item.get("metrics") or {}
        impressions = int(metrics.get("pv") or 0)
        if impressions <= 0:
            return {}
        orders = int(metrics.get("orders") or 0)
        return {
            "days": int(metrics.get("days") or 30),
            "impressions": impressions,
            # 站点只统计 pv / uv / 询盘 / 订单，没有点击计数。把 uv 当 clicks
            # 会让点击率算出 70% 这种假数，进而把实验判成"暴跌 100%"。
            "clicks": 0,
            "orders": orders,
            "returns": 0,
            "order_rate": round(orders / impressions, 6),
            "click_rate": 0.0,
            "return_rate": 0.0,
            "source": "site_snapshot",
            "note": "站点无点击计数，click_rate 不可用",
        }
    return {}


SOURCE_META = {
    "model": ("模型生成", "模型"),
    "fallback": ("模型失败 · 规则兜底", "兜底"),
    "rule": ("规则生成", "规则"),
    "legacy": ("历史数据 · 来源未记录", "历史"),
}


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _number(value, default=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


# ---------------------------------------------------------------------------
# 委托台（Step 5）：只读预览，不执行任何工具
# ---------------------------------------------------------------------------

# 委托内容上限与 classify 的匹配窗口一致（router 只看前 2000 字）。
# 这里直接拒绝而不是静默截断：用户以为整段都听了，实际只听了前 2000 字，
# 这种"看起来成功"的失败比报错更难查。
MAX_INTAKE_TEXT = 2000

# 意图规格里已经声明、但工具要到 Step 7 才注册的名字。等级按设计意图预先标好，
# 响应里 source="planned" 标明这是"设计值"而不是从 registry 读到的"实际值"。
PLANNED_TOOLS: dict[str, dict] = {
    "file_parse": {"side_effect": "read", "requires_approval": False, "risk_level": "R0"},
    "listing_draft": {"side_effect": "write", "requires_approval": True, "risk_level": "R1"},
}

# 工具目录只读一次：build_registry() 每次都会造平台对象，没必要每请求跑一遍。
# 这里刻意不缓存成模块常量，而是懒加载——导入 console 时不该有副作用。
_TOOL_CATALOG: dict[str, dict] | None = None


def tool_catalog() -> dict[str, dict]:
    """工具名 → spec（含推导后的 risk_level）。只读，进程内复用。

    合并两套 registry 而不是只取一套：`build_registry()` 是工坊的九个工具，
    `build_research_registry()` 是 research 的七个只读工具，两套没有包含关系。
    预览要覆盖意图规格里出现的所有工具，只取一套会把另一半误标成"未注册"。
    同名工具（`price_calc`）取先出现的那份——等级一样，没有必要合并字段。
    """

    global _TOOL_CATALOG
    if _TOOL_CATALOG is None:
        catalog: dict[str, dict] = {}
        for spec in list(build_registry().specs()) + list(build_research_registry().specs()):
            catalog.setdefault(spec["name"], spec)
        _TOOL_CATALOG = catalog
    return _TOOL_CATALOG


def _risk_rank(level: str) -> int:
    from ..runtime.risk import rank

    return rank(level)


def plan_preview(intent: Intent, ceiling: str) -> list[dict]:
    """把意图的 toolset 展开成"每一步用什么工具、几级风险、能不能跑"。

    三种状态，前端各显示各的：

    - ``ready``：工具已注册，且在意图的风险上限内（本步只预览，不调用）；
    - ``planned``：工具已在设计里定好、但还没注册（Step 7 才建）。等级取设计值，
      响应里 ``source="planned"``，不冒充从 registry 读到的实际等级；
    - ``blocked``：实际等级超过意图上限 —— 这一步在预览里就该被看见，而不是等到
      执行时才发现工具调不动。

    没登记过的工具名按 ``ceiling`` 保守估级（上限即天花板），而不是默认放行。
    """

    catalog = tool_catalog()
    allowed = _risk_rank(ceiling)
    steps: list[dict] = []
    # CLARIFY / UNKNOWN 的 toolset 是空的，循环自然不跑——不需要特判。
    for index, name in enumerate(INTENT_SPECS[intent]["toolset"], start=1):
        spec = catalog.get(name)
        if spec is not None:
            level = str(spec.get("risk_level") or ceiling)
            source = "registry"
            side_effect = str(spec.get("side_effect") or "")
            requires_approval = bool(spec.get("requires_approval"))
        elif name in PLANNED_TOOLS:
            planned = PLANNED_TOOLS[name]
            level = str(planned["risk_level"])
            source = "planned"
            side_effect = str(planned["side_effect"])
            requires_approval = bool(planned["requires_approval"])
        else:
            level, source, side_effect, requires_approval = ceiling, "estimated", "", False

        if _risk_rank(level) > allowed:
            status = "blocked"
        elif source == "registry":
            status = "ready"
        else:
            status = "planned"
        steps.append(
            {
                "step": index,
                "tool": name,
                "risk_level": level,
                "status": status,
                "source": source,
                "side_effect": side_effect,
                "requires_approval": requires_approval,
            }
        )
    return steps


def _min_cents(variants: list[dict], key: str) -> int | None:
    values = [item.get(key) for item in variants if isinstance(item, dict)]
    numbers = [int(value) for value in values if isinstance(value, int) and value > 0]
    return min(numbers) if numbers else None


def intake_store_ctx(directory: Path) -> tuple[dict, list[str]]:
    """把租户快照裁成映射层要的上下文；读不到就给空候选，不编商品。"""

    notes: list[str] = []
    snapshot = _read_json(directory / "site_snapshot.json", {})
    store = _mapping(snapshot.get("store"))
    ctx = {
        "store_slug": str(store.get("slug") or ""),
        "default_market": "US",
        "default_language": "en",
        "products": [],
    }
    country = str(store.get("country") or "").strip().upper()
    if country in ("US", "DE", "UK"):
        ctx["default_market"] = country
    elif country:
        notes.append(f"快照国家是 {country}（卖家所在地，非目标市场），市场按 US 预演")

    products: list[dict] = []
    for item in snapshot.get("products") or []:
        if not isinstance(item, dict) or not item.get("product_id"):
            continue
        variants = [v for v in item.get("variants") or [] if isinstance(v, dict)]
        products.append(
            {
                "product_id": str(item["product_id"]),
                "title": str(item.get("title_en") or item.get("title") or ""),
                "status": str(item.get("status") or ""),
                "price_cents": _min_cents(variants, "price_cents") or 0,
                "cost_cents": _min_cents(variants, "cost_cents"),
            }
        )
    ctx["products"] = products
    if not products:
        notes.append("快照里没有商品候选，无法绑定 SKU（不编商品）")
    return ctx, notes


def agent_metadata(item: dict) -> dict:
    """Normalize provenance from old and new proposal/task payloads.

    Old files have no ``planner`` at all. They are labelled ``legacy`` rather
    than pretending the rule engine produced them, so the console never makes
    an audit claim it cannot support.
    """
    item = _mapping(item)
    task = _mapping(item.get("task"))
    recorded = "planner" in item or "planner" in task
    planner = str(item.get("planner") or task.get("planner") or "rule").strip() or "rule"
    if not recorded:
        source = "legacy"
        planner = "legacy"
    elif "fallback" in planner.lower():
        source = "fallback"
    elif planner.lower().startswith("llm"):
        source = "model"
    else:
        source = "rule"
    label, short = SOURCE_META[source]

    raw_strategy = _mapping(item.get("strategy")) or _mapping(task.get("strategy"))
    raw_angles = raw_strategy.get("angles")
    raw_angles = raw_angles if isinstance(raw_angles, list) else []
    angles = []
    for raw in raw_angles[:5]:
        raw = _mapping(raw)
        angle = str(raw.get("angle") or "").strip()[:80]
        if not angle:
            continue
        price_index = raw.get("price_index")
        if isinstance(price_index, bool) or not isinstance(price_index, int):
            price_index = None
        angles.append(
            {
                "angle": angle,
                "focus": str(raw.get("focus") or "").strip()[:240],
                "price_index": price_index,
            }
        )
    confidence = _number(raw_strategy.get("confidence"))
    raw_evidence = raw_strategy.get("evidence")
    raw_evidence = raw_evidence if isinstance(raw_evidence, list) else []
    strategy = {
        "problem": str(raw_strategy.get("problem") or "").strip()[:60],
        "problem_label": str(raw_strategy.get("problem_label") or "").strip()[:60],
        "hypothesis": str(raw_strategy.get("hypothesis") or "").strip()[:400],
        "action": str(raw_strategy.get("action") or "").strip()[:400],
        "success_metric": str(raw_strategy.get("success_metric") or "").strip()[:240],
        "reason": str(raw_strategy.get("reason") or "").strip()[:600],
        "confidence": round(min(1.0, max(0.0, confidence)), 3) if confidence is not None else None,
        "angles": angles,
        "evidence": [str(text)[:200] for text in raw_evidence[:6]],
    }

    raw_critique = _mapping(item.get("critique"))
    score = _number(raw_critique.get("score"))
    raw_issues = raw_critique.get("issues")
    raw_issues = raw_issues if isinstance(raw_issues, list) else []
    revisions = item.get("revisions")
    if isinstance(revisions, bool) or not isinstance(revisions, int):
        revisions = 0
    critique = {
        "score": round(min(1.0, max(0.0, score)), 3) if score is not None else None,
        "verdict": str(raw_critique.get("verdict") or "").strip()[:20],
        "issues": [str(text)[:240] for text in raw_issues[:6]],
    }
    return {
        "source": source,
        "label": label,
        "short": short,
        "planner": planner,
        "recorded": recorded,
        "strategy": strategy,
        "critique": critique,
        "revisions": min(20, max(0, revisions)),
    }


def _with_agent_meta(item) -> dict:
    payload = dict(item) if isinstance(item, dict) else {}
    payload["agent_meta"] = agent_metadata(payload)
    return payload


class ConsoleApp:
    def __init__(
        self,
        users: UserStore,
        tenants: TenantStore,
        sessions: SessionManager | None = None,
        limiter: LoginLimiter | None = None,
        static_dir: Path | str | None = None,
        site_factory=None,
        approval_factory=None,
        audit: AuditDB | None = None,
        demo: bool = False,
        scan_factory=None,
    ) -> None:
        self.users = users
        self.tenants = tenants
        self.sessions = sessions or SessionManager()
        self.limiter = limiter or LoginLimiter()
        self.static_dir = Path(static_dir) if static_dir else Path(__file__).resolve().parent / "static"
        self.site_factory = site_factory
        self.approval_factory = approval_factory
        self.audit = audit
        self.demo = demo
        # 扫描跑在后台线程里，进程内只应有一个调度器；测试注入假命令即可脱离模型。
        self.scans = ScanManager(scan_factory)

    def _audit(
        self,
        actor: str,
        action: str,
        tenant: str = "",
        target: str = "",
        ip: str = "",
        status: str = "ok",
        detail: str = "",
    ) -> None:
        if self.audit is not None:
            self.audit.record(actor, action, tenant, target, ip, status, detail)

    # ---- auth -------------------------------------------------
    def login(self, username: str, password: str, ip: str) -> dict:
        key = f"{ip}:{username}".lower()
        if self.limiter.blocked(key):
            self._audit(username, "login", target=username, ip=ip, status="blocked", detail="rate limited")
            raise ConsoleError(429, "尝试次数过多，请 5 分钟后再试")
        user = self.users.authenticate(username, password)
        if user is None:
            self.limiter.fail(key)
            self._audit(username, "login", target=username, ip=ip, status="failed", detail="invalid credentials")
            raise ConsoleError(401, "用户名或密码错误")
        self.limiter.reset(key)
        session = self.sessions.create(str(user["username"]))
        self._audit(user["username"], "login", target=user["username"], ip=ip)
        return {
            "session_token": session.token,
            "csrf": session.csrf,
            "user": self.public_user(user),
        }

    def logout(self, token: str | None, actor: str = "", ip: str = "") -> None:
        self.sessions.destroy(token)
        if actor:
            self._audit(actor, "logout", target=actor, ip=ip)

    def session_user(self, token: str | None) -> dict | None:
        session = self.sessions.get(token)
        if session is None:
            return None
        user = self.users.get(session.username)
        if user is None or user.get("disabled"):
            self.sessions.destroy(token)
            return None
        return user

    @staticmethod
    def public_user(user: dict) -> dict:
        return {
            "username": user["username"],
            "display_name": user.get("display_name") or user["username"],
            "role": user["role"],
            "tenants": list(user.get("tenants") or []),
            "disabled": bool(user.get("disabled", False)),
        }

    # ---- users & audit ----------------------------------------
    @staticmethod
    def _require_admin(user: dict) -> None:
        if user.get("role") != "admin":
            raise ConsoleError(403, "只有管理员可以执行该操作")

    def _validate_tenants(self, tenant_ids: list[str]) -> list[str]:
        result = [str(item) for item in tenant_ids]
        if not result:
            raise ConsoleError(400, "至少选择一个租户")
        for tenant_id in result:
            if self.tenants.get(tenant_id) is None:
                raise ConsoleError(404, f"租户不存在：{tenant_id}")
        return result

    def list_users(self, user: dict) -> list[dict]:
        self._require_admin(user)
        return self.users.list_public()

    def create_user(
        self,
        actor: dict,
        username: str,
        password: str,
        role: str,
        tenant_ids: list[str],
        display_name: str = "",
        ip: str = "",
    ) -> dict:
        self._require_admin(actor)
        tenants = self._validate_tenants(tenant_ids) if role != "admin" else list(tenant_ids)
        try:
            record = self.users.add(username, password, role, tenants, display_name)
        except ValueError as exc:
            raise ConsoleError(400, str(exc)) from exc
        self._audit(actor["username"], "user.create", target=record["username"], ip=ip)
        return self.public_user(record)

    def update_user(
        self,
        actor: dict,
        username: str,
        *,
        role: str | None = None,
        tenant_ids: list[str] | None = None,
        disabled: bool | None = None,
        display_name: str | None = None,
        ip: str = "",
    ) -> dict:
        self._require_admin(actor)
        tenants = self._validate_tenants(tenant_ids) if tenant_ids is not None else None
        try:
            record = self.users.update(
                username,
                role=role,
                tenants=tenants,
                disabled=disabled,
                display_name=display_name,
            )
        except ValueError as exc:
            raise ConsoleError(400, str(exc)) from exc
        if disabled or role is not None or tenant_ids is not None:
            self.sessions.destroy_user(username)
        self._audit(
            actor["username"],
            "user.update",
            target=username,
            ip=ip,
            detail=f"role={record['role']} disabled={record.get('disabled', False)}",
        )
        return self.public_user(record)

    def reset_password(self, actor: dict, username: str, password: str, ip: str = "") -> None:
        self._require_admin(actor)
        try:
            self.users.set_password(username, password)
        except ValueError as exc:
            raise ConsoleError(400, str(exc)) from exc
        self.sessions.destroy_user(username)
        self._audit(actor["username"], "user.password", target=username, ip=ip)

    def change_password(self, actor: dict, old_password: str, new_password: str, ip: str = "") -> None:
        if self.users.authenticate(actor["username"], old_password) is None:
            raise ConsoleError(400, "原密码错误")
        try:
            self.users.set_password(actor["username"], new_password)
        except ValueError as exc:
            raise ConsoleError(400, str(exc)) from exc
        self.sessions.destroy_user(actor["username"])
        self._audit(actor["username"], "password.change", target=actor["username"], ip=ip)

    def audit_logs(self, user: dict, tenant_id: str | None = None, limit: int = 100) -> list[dict]:
        if user.get("role") != "admin":
            if not tenant_id:
                raise ConsoleError(400, "非管理员查询审计时必须指定租户")
            self.tenant_for(user, tenant_id)
        elif tenant_id:
            self.tenant_for(user, tenant_id)
        return self.audit.query(limit=limit, tenant=tenant_id) if self.audit else []

    def export_audit_csv(self, user: dict, tenant_id: str | None = None, limit: int = 5000) -> str:
        if user.get("role") != "admin":
            if not tenant_id:
                raise ConsoleError(400, "非管理员导出审计时必须指定租户")
            self.tenant_for(user, tenant_id)
        elif tenant_id:
            self.tenant_for(user, tenant_id)
        rows = self.audit.export(tenant=tenant_id, limit=limit) if self.audit else []

        def safe(value) -> str:
            text = str(value if value is not None else "")
            return "'" + text if text[:1] in ("=", "+", "-", "@") else text

        stream = io.StringIO()
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["ts", "actor", "action", "tenant", "target", "ip", "status", "detail"])
        for row in rows:
            writer.writerow(
                [
                    safe(row.get("ts")),
                    safe(row.get("actor")),
                    safe(row.get("action")),
                    safe(row.get("tenant")),
                    safe(row.get("target")),
                    safe(row.get("ip")),
                    safe(row.get("status")),
                    safe(row.get("detail")),
                ]
            )
        return stream.getvalue()

    # ---- tenancy ----------------------------------------------
    def tenant_for(self, user: dict, tenant_id: str) -> Tenant:
        tenant = self.tenants.get(tenant_id)
        if tenant is None:
            raise ConsoleError(404, "租户不存在")
        if user.get("role") != "admin" and tenant.tenant_id not in (user.get("tenants") or []):
            raise ConsoleError(403, "没有该租户的访问权限")
        return tenant

    def my_tenants(self, user: dict) -> list[dict]:
        return [
            {
                "tenant_id": tenant.tenant_id,
                "name": tenant.name,
                "currency": tenant.currency,
                "timezone": tenant.timezone,
                "site_configured": self.demo or bool(tenant.site_base_url and os.environ.get(tenant.token_env, "")),
            }
            for tenant in self.tenants.for_user(user)
        ]

    def overview(self, user: dict, tenant_id: str) -> dict:
        tenant = self.tenant_for(user, tenant_id)
        directory = tenant.data_dir
        tasks = _read_json(directory / "task_orders.json", {}).get("selected", [])
        proposals = _read_json(directory / "workshop_proposals.json", {}).get("proposals", [])
        reviews = _read_json(directory / "review_results.json", {}).get("results", [])
        publishes = _read_json(directory / "publish_results.json", {}).get("results", [])
        snapshot = _read_json(directory / "site_snapshot.json", {})
        memory = MemoryStore(directory / "memory")
        products = snapshot.get("products", []) if isinstance(snapshot, dict) else []
        skus = sum(len(item.get("variants", [])) for item in products)
        store_name = (snapshot.get("store") or {}).get("name") if isinstance(snapshot, dict) else ""
        approvals = [
            {
                "token": record.token[:10] + "…",
                "product_id": record.product_id,
                "variant_key": record.variant_key,
                "issued_by": record.issued_by,
                "expires_at": record.expires_at,
            }
            for record in self._approvals(tenant).list_active()
        ]
        return {
            "tenant": {
                "tenant_id": tenant.tenant_id,
                "name": tenant.name,
                "currency": tenant.currency,
                "timezone": tenant.timezone,
                "site_configured": self.demo or bool(tenant.site_base_url and os.environ.get(tenant.token_env, "")),
            },
            "demo": self.demo,
            "store": {"name": store_name or tenant.name, "products": len(products), "skus": skus},
            "checkup": build_checkup(directory),
            "metrics": build_metrics(directory, trace_dir=TRACE_DIR),
            "task_orders": [_with_agent_meta(item) for item in tasks[:20]],
            # Compliance is joined onto each variant here rather than in the
            # browser: the four-check matrix and the "can this be approved"
            # answer must come from one place, and that place is the server.
            "proposals": [
                _with_agent_meta(item) for item in attach_verdicts(proposals[:10], reviews)
            ],
            "reviews": reviews[:30],
            "publishes": publishes[:20],
            "approvals": approvals,
            "experiments": memory.read_experiments(limit=20, include_open=True),
            "experiment_board": build_experiment_board(directory),
            "lessons": memory.read_lessons(),
            "memory": build_memory(directory),
            "memory_health": memory.health(),
        }

    def memory_replay(self, user: dict, tenant_id: str, run_id: str) -> dict:
        """Rebuild the memory a given run was given (FR-804)."""
        tenant = self.tenant_for(user, tenant_id)
        try:
            return replay_snapshot(tenant.data_dir, run_id)
        except ValueError as exc:
            raise ConsoleError(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise ConsoleError(404, f"没有 run_id 为 {run_id} 的记忆快照") from exc

    # ---- 委托台（只读预览 + 独立历史） -------------------------
    def intake_history(self, user: dict, tenant_id: str, limit: int = 50) -> dict:
        """委托历史：读 ``data/tenants/<tid>/intake/history.jsonl``（只增不改）。

        与 facts 分开存：facts 是会被检索、注入后续任务的店铺事实，委托原文
        不属于事实，只用于回看。这里只读，不提供改写/删除接口。
        """

        tenant = self.tenant_for(user, tenant_id)
        entries = read_history(tenant.data_dir, limit=limit)
        return {
            "history": entries,
            "count": len(entries),
            "note": "只增不改；原文不进 facts，也不参与任何检索注入",
        }

    def intake_preview(self, user: dict, tenant_id: str, text: str) -> dict:
        """一句话 → 意图 / 槽位 / 计划预览 / 合约草案。

        预览本身不执行任何工具、不写业务数据；唯一落盘的是这次委托的历史记录
        （``intake/history.jsonl``，append-only、best-effort）。历史写失败不影响
        预览返回，``history_recorded`` 会如实告诉前端写没写成。

        这里刻意不写审计：审计库也在 data/ 下，且登录已经留过一次痕；委托预览
        不是业务动作，记进审计只会让审计表被"看了一眼"这种事件淹没。
        """

        tenant = self.tenant_for(user, tenant_id)
        text = str(text or "").strip()
        if not text:
            raise ConsoleError(400, "委托内容不能为空")
        if len(text) > MAX_INTAKE_TEXT:
            raise ConsoleError(400, f"委托内容过长（上限 {MAX_INTAKE_TEXT} 字，实际 {len(text)} 字）")

        ctx, notes = intake_store_ctx(tenant.data_dir)
        # classify_auto：INTAKE_ENGINE 控制引擎（默认 auto = 大模型优先，失败回退规则）。
        # 引擎与熔断状态只影响这条结果怎么来，不影响下游合约/风险链路。
        result = classify_auto(text)
        spec = INTENT_SPECS[result.intent]
        outcome = plan_intent(result, ctx)

        assumed: dict[str, bool] = {}
        pending: list[str] = []
        for order in outcome.orders:
            for name, flag in (order.get("slots", {}).get("assumed") or {}).items():
                if flag:
                    assumed[name] = True
            for name in order.get("slots", {}).get("pending_input") or []:
                if name not in pending:
                    pending.append(name)

        recorded = append_history(
            tenant.data_dir,
            {
                "tenant_id": tenant.tenant_id,
                "actor": str(user.get("username") or ""),
                "role": str(user.get("role") or ""),
                "text": text,
                "intent": result.intent.value,
                "confidence": result.confidence,
                "engine": result.engine,
                "artifact": spec["artifact"],
                "risk_ceiling": spec["risk_ceiling"],
                "slots": dict(result.slots),
                "missing": list(result.missing),
                "assumed": assumed,
                "pending_input": pending,
                "order_count": len(outcome.orders),
                "task_ids": [order.get("task_id", "") for order in outcome.orders],
                "reason": outcome.reason,
            },
        )

        return {
            "dry_run": True,
            "tenant_id": tenant.tenant_id,
            "text": text,
            "intent": result.intent.value,
            "confidence": result.confidence,
            "engine": result.engine,
            "slots": dict(result.slots),
            "missing": list(result.missing),
            "candidates": list(result.candidates),
            "artifact": spec["artifact"],
            "owner": spec.get("owner", ""),
            "risk_ceiling": spec["risk_ceiling"],
            "plan_preview": plan_preview(result.intent, spec["risk_ceiling"]),
            "task_orders": [jsonable(order) for order in outcome.orders],
            "assumed": assumed,
            "pending_input": pending,
            "reason": outcome.reason,
            "notes": notes + list(outcome.notes),
            # 布尔值而不是 history_id：预览要保持"同一输入 → 同一输出"。
            "history_recorded": recorded is not None,
            "store": {
                "slug": ctx["store_slug"],
                "market": ctx["default_market"],
                "products": len(ctx["products"]),
            },
        }

    # ---- 事实（作业记忆，Step 6） ------------------------------
    def _facts(self, tenant: Tenant) -> FactsStore:
        # 和主管的四个记忆文件同目录、不同文件：facts.jsonl 只由这里读写。
        return FactsStore(tenant.data_dir / "memory")

    def facts_catalog(self) -> dict:
        """前端下拉框的选项。写死在两边容易漂，所以由服务端给一份。"""

        return {
            "scopes": [
                {"value": value, "label": SCOPE_LABELS.get(value, value)} for value in SCOPES
            ],
            "types": [
                {"value": value, "label": TYPE_LABELS.get(value, value)} for value in TYPES
            ],
            "sources": [
                {"value": value, "label": SOURCE_LABELS.get(value, value), "confidence": score}
                for value, score in SOURCE_CONFIDENCE.items()
            ],
            "default_importance": DEFAULT_IMPORTANCE,
        }

    def facts_list(
        self,
        user: dict,
        tenant_id: str,
        scope: str = "",
        status: str = "active",
        limit: int = 50,
        query: str = "",
        budget_tokens: int = 800,
    ) -> dict:
        """列事实；带 ``query`` 时走检索（打分 + 预算截断）。"""

        tenant = self.tenant_for(user, tenant_id)
        store = self._facts(tenant)
        payload = {
            "catalog": self.facts_catalog(),
            "stats": store.stats(),
        }
        if str(query or "").strip():
            payload.update(store.search(scope=scope, query=query, budget_tokens=budget_tokens))
            payload["query"] = query
            return payload
        facts = store.list(scope=scope, status=status, limit=limit)
        payload.update({"facts": facts, "count": len(facts), "status": status})
        return payload

    def facts_add(
        self,
        user: dict,
        tenant_id: str,
        payload: dict,
        ip: str = "",
    ) -> dict:
        """记一条事实。写入必须由这次显式请求触发，没有别的代码路径会调它。"""

        tenant = self.tenant_for(user, tenant_id)
        if not isinstance(payload, dict):
            raise ConsoleError(400, "请求体必须是对象")
        # 委托原文只进 intake/history.jsonl；这里连字段名都不接受，避免"顺手塞进来"。
        if "text" in payload:
            raise ConsoleError(400, "facts 不接受委托原文（text 字段）；原文只写 intake/history.jsonl")

        content = payload.get("content")
        if not isinstance(content, str):
            raise ConsoleError(400, "content 必须是字符串")
        try:
            result = self._facts(tenant).add(
                scope=str(payload.get("scope") or "shop"),
                fact_type=str(payload.get("type") or "fact"),
                content=content,
                source=str(payload.get("source") or ""),
                importance=payload.get("importance", DEFAULT_IMPORTANCE),
                confidence=payload.get("confidence"),
                supersedes=payload.get("supersedes") or None,
                actor=str(user.get("username") or ""),
            )
        except ValueError as exc:
            raise ConsoleError(400, str(exc)) from exc

        fact = result["fact"]
        self._audit(
            str(user.get("username") or ""),
            "fact.add",
            tenant=tenant_id,
            target=fact.get("memory_id", ""),
            ip=ip,
            detail=f"{fact.get('scope')}/{fact.get('type')} · {fact.get('source')}",
        )
        return {
            "fact": fact,
            "duplicate": result["duplicate"],
            "superseded": result["superseded"],
            "stats": self._facts(tenant).stats(),
        }

    def facts_revoke(
        self,
        user: dict,
        tenant_id: str,
        memory_id: str,
        reason: str = "",
        ip: str = "",
    ) -> dict:
        """软删（撤回）。历史行不动，只追加一条状态事件。"""

        tenant = self.tenant_for(user, tenant_id)
        try:
            result = self._facts(tenant).revoke(
                memory_id,
                reason=reason,
                actor=str(user.get("username") or ""),
            )
        except KeyError as exc:
            raise ConsoleError(404, f"没有 memory_id 为 {memory_id} 的事实") from exc
        if result["changed"]:
            self._audit(
                str(user.get("username") or ""),
                "fact.revoke",
                tenant=tenant_id,
                target=memory_id,
                ip=ip,
                detail=str(reason or "")[:MAX_REASON],
            )
        return {**result, "stats": self._facts(tenant).stats()}

    # ---- on-demand scan ---------------------------------------
    def _pending_proposals(self, tenant: Tenant) -> dict:
        """扫描会重写任务单与方案包，先数清楚有多少成果还没发布出去。"""
        directory = tenant.data_dir
        proposals = _read_json(directory / "workshop_proposals.json", {}).get("proposals", [])
        publishes = _read_json(directory / "publish_results.json", {}).get("results", [])
        published = {
            str(item.get("product_id"))
            for item in publishes
            if isinstance(item, dict) and item.get("product_id")
        }
        pending = [
            item for item in proposals
            if isinstance(item, dict) and str(item.get("product_id")) not in published
        ]
        return {"proposals": len(proposals), "unpublished": len(pending)}

    def scan_status(self, user: dict, tenant_id: str) -> dict:
        tenant = self.tenant_for(user, tenant_id)
        return {"scan": self.scans.status(tenant), "pending": self._pending_proposals(tenant)}

    def scan_start(
        self, user: dict, tenant_id: str, *, force: bool = False, ip: str = ""
    ) -> dict:
        tenant = self.tenant_for(user, tenant_id)
        if self.demo:
            raise ConsoleError(409, "演示模式（--demo）是只读的，不能扫描店铺")
        pending = self._pending_proposals(tenant)
        if pending["unpublished"] and not force:
            raise ConsoleError(
                409,
                f"还有 {pending['unpublished']} 个方案没发布，扫描会重写任务单和方案包；"
                "确认覆盖后重试",
            )
        try:
            state = self.scans.start(tenant, actor=str(user.get("username", "")))
        except ScanBusy as exc:
            raise ConsoleError(409, str(exc)) from exc
        except ScanError as exc:
            raise ConsoleError(400, str(exc)) from exc
        self._audit(
            str(user.get("username", "")),
            "scan.start",
            tenant=tenant.tenant_id,
            target=str(state.get("run_id") or ""),
            ip=ip,
        )
        return {"scan": state, "pending": pending}

    # ---- approval / publish -----------------------------------
    def _approvals(self, tenant: Tenant) -> ApprovalStore:
        if self.approval_factory is not None:
            return self.approval_factory(tenant)
        return ApprovalStore(tenant.data_dir / "approvals.json")

    def _site(self, tenant: Tenant):
        try:
            return build_site(tenant, self.site_factory)
        except ConnectorError as exc:
            raise ConsoleError(400, str(exc)) from exc

    def _orchestrator(self, tenant: Tenant) -> PublishOrchestrator:
        registrar = ExperimentRegistrar(
            MemoryStore(tenant.data_dir / "memory"),
            baseline_provider=lambda product_id, started_at: _snapshot_baseline(
                tenant.data_dir, product_id
            ),
            store_slug=tenant.tenant_id,
        )

        def register(proposal: dict, preview) -> str:
            record = registrar.register(proposal, preview)
            return str(record.get("experiment_id") or "")

        return PublishOrchestrator(
            self._site(tenant),
            approvals=self._approvals(tenant),
            store_slug=tenant.tenant_id,
            lock_dir=tenant.data_dir / "locks",
            verdict_path=tenant.data_dir / "review_results.json",
            on_published=register,
        )

    def _find_proposal(self, tenant: Tenant, product_id: str) -> dict:
        proposals = _read_json(tenant.data_dir / "workshop_proposals.json", {}).get("proposals", [])
        for proposal in proposals:
            if str(proposal.get("product_id")) == str(product_id):
                return proposal
        raise ConsoleError(404, "找不到该商品的方案")

    def approve(
        self,
        user: dict,
        tenant_id: str,
        product_id: str,
        variant_key: str,
        ttl: int = 86400,
        ip: str = "",
    ) -> dict:
        if user.get("role") not in ("admin", "approver"):
            raise ConsoleError(403, "只有审批人可以签发令牌")
        tenant = self.tenant_for(user, tenant_id)
        if not 60 <= int(ttl) <= 604800:
            raise ConsoleError(400, "TTL 必须在 60 到 604800 秒之间")
        verdict = load_verdicts(tenant.data_dir / "review_results.json").get(
            (str(product_id), str(variant_key)), {}
        )
        if not verdict:
            # Fail closed: a version nobody reviewed is not "probably fine".
            # Publish would let it through (it only blocks on an explicit
            # verdict), so the approval gate is where the queue stays honest.
            raise ConsoleError(409, "该版本还没有合规结论，先运行 review 再审批")
        if str(verdict.get("severity") or "").lower() == "block":
            raise ConsoleError(409, "该版本已被合规否决，不能签发审批令牌")
        record = self._approvals(tenant).issue(
            product_id, variant_key, str(user["username"]), ttl_seconds=int(ttl)
        )
        self._audit(
            user["username"],
            "approve",
            tenant=tenant_id,
            target=f"{product_id}/{variant_key}",
            ip=ip,
        )
        return {
            "token": record.token,
            "product_id": record.product_id,
            "variant_key": record.variant_key,
            "issued_by": record.issued_by,
            "expires_at": record.expires_at,
        }

    def _product(self, site, product_id: str) -> SiteProduct:
        products = site.all_products()
        for product in products:
            if product.product_id == product_id:
                return product
        raise ConsoleError(404, "站点上找不到该商品")

    def preview(
        self,
        user: dict,
        tenant_id: str,
        product_id: str,
        variant_index: int = 0,
        ip: str = "",
    ) -> dict:
        tenant = self.tenant_for(user, tenant_id)
        proposal = self._find_proposal(tenant, product_id)
        try:
            orchestrator = self._orchestrator(tenant)
            product = self._product(orchestrator.site, product_id)
            preview = orchestrator.preview(proposal, product, variant_index)
        except SiteAPIError as exc:
            self._audit(
                user["username"],
                "preview",
                tenant=tenant_id,
                target=product_id,
                ip=ip,
                status="failed",
                detail=str(exc),
            )
            raise ConsoleError(502, f"站点接口错误：{exc.status} {exc.message}") from exc
        self._audit(
            user["username"],
            "preview",
            tenant=tenant_id,
            target=f"{product_id}/{preview.variant_key}",
            ip=ip,
            detail=f"price_change={preview.price_change_pct}",
        )
        return {
            "product_id": preview.product_id,
            "variant_id": preview.variant_id,
            "variant_key": preview.variant_key,
            "price_change_pct": preview.price_change_pct,
            "requires_approval": preview.requires_approval,
            "review": asdict(preview.review),
            "product_diff": preview.product_diff,
            "variant_diff": preview.variant_diff,
        }

    def publish(
        self,
        user: dict,
        tenant_id: str,
        product_id: str,
        variant_index: int = 0,
        approval_token: str = "",
        ip: str = "",
    ) -> dict:
        if self.demo:
            raise ConsoleError(400, "演示模式只读：可以预览，不能发布")
        if not approval_token:
            raise ConsoleError(400, "发布需要审批令牌")
        tenant = self.tenant_for(user, tenant_id)
        proposal = self._find_proposal(tenant, product_id)
        try:
            orchestrator = self._orchestrator(tenant)
            product = self._product(orchestrator.site, product_id)
            result = orchestrator.publish(
                proposal, product, variant_index, approval_token=approval_token
            )
        except SiteAPIError as exc:
            self._audit(
                user["username"],
                "publish",
                tenant=tenant_id,
                target=product_id,
                ip=ip,
                status="failed",
                detail=str(exc),
            )
            raise ConsoleError(502, f"站点接口错误：{exc.status} {exc.message}") from exc

        self._audit(
            user["username"],
            "publish",
            tenant=tenant_id,
            target=f"{product_id}/{result.preview.variant_key if result.preview else ''}",
            ip=ip,
            status=result.status,
            detail=result.message,
        )
        record = {
            "product_id": product_id,
            "variant_key": result.preview.variant_key if result.preview else "",
            "status": result.status,
            "message": result.message,
            "price_change_pct": result.preview.price_change_pct if result.preview else 0,
            "requires_approval": result.preview.requires_approval if result.preview else False,
            "review": asdict(result.preview.review) if result.preview else {},
            "product_diff": result.preview.product_diff if result.preview else {},
            "variant_diff": result.preview.variant_diff if result.preview else {},
            "product_version_id": result.product_version_id,
            "variant_version_id": result.variant_version_id,
            "approved_by": result.approved_by,
            "published_at": datetime.now().isoformat(timespec="seconds"),
        }
        path = tenant.data_dir / "publish_results.json"
        payload = _read_json(path, {})
        results = payload.get("results", []) if isinstance(payload, dict) else []
        results.insert(0, record)
        _write_json(path, {"store": tenant.tenant_id, "results": results[:50]})

        return {
            "ok": result.ok,
            "status": result.status,
            "message": result.message,
            "approved_by": result.approved_by,
            "product_version_id": result.product_version_id,
            "variant_version_id": result.variant_version_id,
        }
