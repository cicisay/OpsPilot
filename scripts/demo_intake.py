"""端到端演示：一句话委托 → 意图 → 澄清 → 计划 → 风险 → 记忆 → 草稿 → 收口。

用法::

    python scripts/demo_intake.py --text "这 20 个新品上到美国站"
    python scripts/demo_intake.py --text "这 20 个新品上到美国站" --source data/ingest/a.csv
    python scripts/demo_intake.py --text "..." --tenant data/tenants/globaltrade
    python scripts/demo_intake.py --text "..." --no-memory --no-preview
    python scripts/demo_intake.py --text "..." --json

这个脚本**只读**：不写 `data/`、不发布、不进工坊。它把 Step 1/3/4/6/7 串成一条
可读轨迹，每一步都打印「看到了什么、凭什么这么判、下一步给了谁」。三条硬规则：

1. **不写业务数据。** 唯一的临时写入是 `--tenant` 缺省时给记忆开的临时目录，
   进程退出即删除；`data/` 全程零变化。
2. **不补数值。** 缺字段就报缺字段，规格没溯源就报未溯源——和 Step 7 同一条线。
3. **阻断不可被演示脚本绕过。** 合规命中的草稿照样标 `blocked`，脚本只负责把它
   显示出来，不会为了"演示好看"把它放行。

`run_pipeline()` 返回纯 dict，评测脚本（`eval_intake.py --full`）和测试直接复用，
保证"演示看到的"和"评测量到的"是同一段代码。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ops_agent.intake import INTENT_SPECS, Intent, classify  # noqa: E402
from ops_agent.intake.mapping import ORDER_ARTIFACTS, plan_intent  # noqa: E402
from ops_agent.memory.facts import SOURCE_LABELS, FactsStore, estimate_tokens  # noqa: E402
from ops_agent.runtime.risk import check_level, is_visible  # noqa: E402
from ops_agent.tools import (  # noqa: E402
    ToolContext,
    build_ingest_registry,
    build_registry,
    build_research_registry,
)

SAMPLE_CSV = ROOT / "tests" / "fixtures" / "ingest" / "sample_supplier.csv"
INGEST_ROOT = ROOT / "data" / "ingest"

#: 上新链路能消费的意图。其余意图（诊断/问答/改价…）不走 CSV，直接停在计划层。
INGEST_INTENTS = (Intent.BATCH_UPLOAD, Intent.SINGLE_UPLOAD)

#: 演示用店铺上下文。真快照要用 `--snapshot` 显式给，避免"测试依赖 data/"。
DEMO_PRODUCTS = [
    {"product_id": "FT-001", "title": "Soy Wax Candle 200g in Amber Jar", "status": "draft", "price_cents": 1690, "cost_cents": 420},
    {"product_id": "FT-002", "title": "Bamboo Drawer Divider Expandable", "status": "draft", "price_cents": 2590, "cost_cents": 650},
    {"product_id": "FT-003", "title": "Building Brick Organizer", "status": "active", "price_cents": 1250, "cost_cents": 380},
    {"product_id": "FT-004", "title": "Ceramic Mug 350ml Matte Glaze", "status": "active", "price_cents": 1490, "cost_cents": 410},
]

#: 没有租户 facts 时用的演示事实。**只进临时目录**，不落 `data/`。
DEMO_FACTS = [
    {
        "scope": "shop",
        "type": "rule",
        "content": "上新前必须核对品牌授权，未经授权的品牌词一律不得出现在标题里",
        "importance": 0.9,
    },
    {
        "scope": "market",
        "type": "constraint",
        "content": "美国站蜡烛类目需要加州 65 号提案警示语",
        "importance": 0.7,
    },
    {
        "scope": "shop",
        "type": "preference",
        "content": "美国站 Listing 标题控制在 120 字符以内，不使用感叹号",
        "importance": 0.6,
    },
]

MEMORY_LIMIT = 5
MEMORY_BUDGET_TOKENS = 300
MAX_DRAFT_FACTS = 10


# ---------------------------------------------------------------------------
# 风险表
# ---------------------------------------------------------------------------


def tool_index() -> dict[str, dict]:
    """三张注册表合成一张索引：名字 → spec。

    分成三张是历史原因（工坊 9 个、研究 7 个、上新 2 个），风险判定只关心
    `side_effect` / `requires_approval`，合成一张才不会漏。
    """

    index: dict[str, dict] = {}
    for registry in (build_registry(), build_research_registry(), build_ingest_registry()):
        for spec in registry.specs():
            index[str(spec["name"])] = spec
    return index


def risk_rows(toolset: list[str], index: dict[str, dict] | None = None) -> list[dict]:
    """把意图声明的工具集翻译成「工具 / 等级 / 模型可见 / 是否要人确认」。

    未注册的工具按最坏情况处理（R3、不可见），这是 `runtime/risk.py` 的
    「未知即失败关闭」在演示层的同一条口径。
    """

    table = index if index is not None else tool_index()
    rows: list[dict] = []
    for name in toolset:
        spec = table.get(name)
        if spec is None:
            rows.append(
                {
                    "name": name,
                    "side_effect": "",
                    "level": "R3",
                    "requires_approval": True,
                    "visible": False,
                    "note": "未注册，按不可逆处理",
                }
            )
            continue
        rows.append(
            {
                "name": name,
                "side_effect": str(spec.get("side_effect") or ""),
                "level": infer_level(spec),
                "requires_approval": bool(spec.get("requires_approval")),
                "visible": None,  # 由 ceiling 决定，下面统一填
                "note": "",
            }
        )
    return rows


def infer_level(spec: dict) -> str:
    """和 `runtime/risk.infer_risk_level` 同一个函数，这里只是补 import 噪音。"""

    from ops_agent.runtime.risk import infer_risk_level

    return infer_risk_level(
        str(spec.get("name") or ""),
        str(spec.get("side_effect") or ""),
        bool(spec.get("requires_approval")),
    )


def risk_block(intent: Intent, index: dict[str, dict] | None = None) -> dict:
    spec = INTENT_SPECS[intent]
    ceiling = check_level(spec["risk_ceiling"])
    rows = risk_rows(list(spec["toolset"]), index)
    for row in rows:
        row["visible"] = is_visible(row["level"], ceiling)
    return {
        "ceiling": ceiling,
        "owner": str(spec.get("owner") or ""),
        "artifact": str(spec["artifact"]),
        "tools": rows,
        "hidden": [row["name"] for row in rows if not row["visible"]],
        "needs_approval": [row["name"] for row in rows if row["requires_approval"]],
    }


# ---------------------------------------------------------------------------
# 上下文与记忆
# ---------------------------------------------------------------------------


def demo_store_ctx() -> dict:
    return {
        "store_slug": "demo-store",
        "default_market": "US",
        "default_language": "en",
        "products": [dict(item) for item in DEMO_PRODUCTS],
        "source": "demo_fixture",
    }


def store_ctx_from_snapshot(path: Path) -> tuple[dict, str | None]:
    """读真快照；读不动就退回演示上下文并说明原因（不编商品）。"""

    from ops_agent.site_platform import load_snapshot

    try:
        store, products = load_snapshot(path)
    except (OSError, ValueError, KeyError) as exc:
        return demo_store_ctx(), f"快照读取失败（{exc}），退回演示上下文"
    country = str(getattr(store, "country", "") or "").strip().upper()
    ctx = {
        "store_slug": getattr(store, "slug", "") or "snapshot",
        "default_market": country if country in ("US", "DE", "UK") else "US",
        "default_language": "en",
        "products": [
            {
                "product_id": product.product_id,
                "title": product.title_en,
                "status": product.status,
                "price_cents": product.min_price_cents,
                "cost_cents": min(
                    (variant.cost_cents for variant in product.variants if variant.cost_cents),
                    default=None,
                ),
            }
            for product in products
        ],
        "source": str(path),
    }
    note = None
    if country not in ("US", "DE", "UK"):
        note = f"快照国家是 {country or '空'}（卖家所在地），市场按 US 预演"
    return ctx, note


def _price_cents(raw) -> int:
    """供应商价 → 分。读不出就是 0：映射层看到 0 会跳过这条单，不会编价格。"""

    text = str(raw or "").strip().replace(",", "")
    if not text:
        return 0
    try:
        value = float(text)
    except ValueError:
        return 0
    return int(round(value * 100)) if value > 0 else 0


def context_for_ingest(ctx: dict, csv_path: Path | str) -> tuple[dict, str | None]:
    """上新意图把候选换成 CSV 里的行。

    不这么做的话，计划预览绑的是店铺里已有的商品、草稿却来自供应商资料，
    两张皮——真快照下会看到"预览 20 条、草稿 3 条"这种对不上的结果。
    """

    from ops_agent.ingest import IngestError, read_supplier_csv

    try:
        root, relative = resolve_source(csv_path)
        parsed = read_supplier_csv(root / relative)
    except (ValueError, IngestError, OSError) as exc:
        return ctx, f"读不了供应商资料（{exc}），计划仍按店铺商品预演"

    products = []
    for row in parsed.rows:
        sku = str(row.get("sku") or "").strip()
        if not sku:
            continue
        products.append(
            {
                "product_id": sku,
                "title": str(row.get("title") or "").strip(),
                # 新品还没上架，状态就是 draft；映射层会优先绑它们。
                "status": "draft",
                "price_cents": _price_cents(row.get("price")),
                "cost_cents": _price_cents(row.get("cost")) or None,
            }
        )
    if not products:
        return ctx, "供应商资料里没有可用 sku，计划仍按店铺商品预演"
    return {**ctx, "products": products, "source": str(csv_path)}, None


@contextmanager
def memory_store(tenant: str | None):
    """租户有 facts 就读真的；没有就开临时目录灌演示事实，退出即删。"""

    if tenant:
        store = FactsStore(Path(tenant) / "memory")
        yield store, "tenant"
        return
    with tempfile.TemporaryDirectory(prefix="demo-facts-") as tmp:
        store = FactsStore(tmp)
        for item in DEMO_FACTS:
            store.add(
                scope=item["scope"],
                fact_type=item["type"],
                content=item["content"],
                source="user_instruction",
                importance=item["importance"],
            )
        yield store, "demo_fixture"


def memory_block(text: str, tenant: str | None, enabled: bool) -> dict:
    if not enabled:
        return {
            "enabled": False,
            "count": 0,
            "tokens": 0,
            "budget_tokens": MEMORY_BUDGET_TOKENS,
            "truncated": False,
            "fallback": False,
            "source": "off",
            "facts": [],
        }

    with memory_store(tenant) as (store, origin):
        found = store.search(query=text, budget_tokens=MEMORY_BUDGET_TOKENS, limit=MEMORY_LIMIT)
        # 店铺级规则必须一直生效：一条"不做宠物类目"跟今天的委托字面无关，但它
        # 决定了这条委托能不能做。所以除了关键词命中，再按 scope 钉住前 N 条，
        # 而不是"查询没命中才回退"——后者会漏掉"命中了两条无关的、却漏掉规则"。
        pinned = store.search(scope="shop", query="", budget_tokens=MEMORY_BUDGET_TOKENS, limit=MEMORY_LIMIT)
        stats = store.stats()

    picked: list[dict] = []
    seen: set[str] = set()
    for item in list(found["facts"]) + list(pinned["facts"]):
        memory_id = str(item["memory_id"])
        if memory_id in seen:
            continue
        seen.add(memory_id)
        picked.append(item)

    # 合并后重新按预算截断：两次检索各自都在预算内，合起来可能超。
    budget = max(1, MEMORY_BUDGET_TOKENS)
    used = 0
    trimmed: list[dict] = []
    for item in picked:
        cost = estimate_tokens(item["content"])
        if trimmed and used + cost > budget:
            break
        trimmed.append(item)
        used += cost

    return {
        "enabled": True,
        "count": len(trimmed),
        "tokens": used,
        "budget_tokens": budget,
        "truncated": len(trimmed) < len(picked),
        "candidates": found["candidates"],
        "pinned": len([item for item in trimmed if item["memory_id"] not in {f["memory_id"] for f in found["facts"]}]),
        "source": origin,
        "total_facts": stats["total"],
        "empty": stats["total"] == 0,
        "facts": [
            {
                "memory_id": item["memory_id"],
                "scope": item["scope"],
                "content": item["content"],
                "source": item["source"],
                "source_label": SOURCE_LABELS.get(item["source"], item["source"]),
                "score": item["score"],
                "tokens": estimate_tokens(item["content"]),
            }
            for item in trimmed
        ],
    }


# ---------------------------------------------------------------------------
# 澄清
# ---------------------------------------------------------------------------


def clarifications(result, csv_name: str, store_size: int, bound: list[dict] | None = None) -> list[dict]:
    """意图层说缺的槽位，逐条给出「怎么解决」或「只能等人回答」。

    ``bound`` 是计划层已经绑上的商品。意图层没抽到 sku、但原话里点了名的，
    计划层会兜住；这时候不能再报"等用户指定"，否则同一件事两处口径打架。
    """

    bound = bound or []
    items: list[dict] = []
    seen: set[str] = set()
    for slot in list(result.missing):
        if slot in seen:
            continue
        seen.add(slot)
        if slot == "source_path":
            items.append(
                {
                    "slot": slot,
                    "question": "供应商资料在哪？",
                    "resolution": f"用 {csv_name} 作为本次来源（--source）",
                    "resolved": True,
                }
            )
        elif slot == "language":
            items.append(
                {
                    "slot": slot,
                    "question": "用哪种语言写 Listing？",
                    "resolution": "取店铺默认 en（assumed，执行前需人确认）",
                    "resolved": True,
                }
            )
        elif slot == "market":
            items.append(
                {
                    "slot": slot,
                    "question": "上到哪个站点？",
                    "resolution": "取店铺默认 US（assumed）",
                    "resolved": True,
                }
            )
        elif slot == "sku":
            if bound:
                names = "、".join(str(item.get("product_id") or "") for item in bound[:3])
                items.append(
                    {
                        "slot": slot,
                        "question": "是哪一条商品？",
                        "resolution": f"按原话点到的 {names} 绑定（意图层未抽到，计划层兜底）",
                        "resolved": True,
                    }
                )
            else:
                items.append(
                    {
                        "slot": slot,
                        "question": "是哪一条商品？",
                        "resolution": f"待用户指定（候选 {store_size} 条，不替用户挑）",
                        "resolved": False,
                    }
                )
        else:
            items.append(
                {
                    "slot": slot,
                    "question": f"{slot} 还没给",
                    "resolution": "待用户补充，本步不猜",
                    "resolved": False,
                }
            )
    return items


# ---------------------------------------------------------------------------
# 上新
# ---------------------------------------------------------------------------


def resolve_source(path: Path | str) -> tuple[Path, str]:
    """把 ``--source`` 收敛成 (ingest 根目录, 相对路径)。

    不能拿 CSV 自己的目录当根：那样等于把 `file_parse` 的"只读 data/ingest"
    换成"哪儿都能读"，路径策略就白写了。仓库样例是唯一例外（只读、明确标注）。
    """

    target = Path(path).resolve()
    if target == SAMPLE_CSV.resolve():
        return target.parent, target.name
    root = INGEST_ROOT.resolve()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"只接受 data/ingest 下的 CSV（或仓库样例）：{path}"
        ) from exc
    return root, relative.as_posix()


def ingest_file(root: Path, relative: str, market: str, language: str, facts: list[str]) -> dict:
    """走真实的 file_parse + listing_draft 工具，而不是直接调 build_draft。

    演示要证明的是「工具这条路能通」，直接调内部函数只能证明函数能跑。
    """

    registry = build_ingest_registry()
    context = ToolContext(ingest_root=root)
    parsed = registry.call("file_parse", {"path": relative, "kind": "csv"}, context)
    if not parsed.ok:
        return {
            "ok": False,
            "code": parsed.code,
            "error": parsed.error,
            "rows": 0,
            "drafts": [],
            "by_status": {},
            "notes": [],
        }

    drafts: list[dict] = []
    errors: list[dict] = []
    for row in parsed.data["rows"]:
        sku = str(row.get("sku") or "").strip()
        if not sku:
            errors.append({"sku": "", "code": "missing_sku", "error": "行里没有 sku，跳过"})
            continue
        result = registry.call(
            "listing_draft",
            {"sku": sku, "market": market, "language": language, "facts": facts},
            context,
        )
        if result.ok:
            drafts.append(result.data)
        else:
            errors.append({"sku": sku, "code": result.code, "error": result.error})

    by_status: dict[str, int] = {}
    for draft in drafts:
        by_status[draft["status"]] = by_status.get(draft["status"], 0) + 1

    return {
        "ok": True,
        "path": parsed.data["path"],
        "rows": parsed.data["count"],
        "columns": parsed.data["columns"],
        "ignored_columns": parsed.data["ignored_columns"],
        "drafts": drafts,
        "by_status": by_status,
        "errors": errors,
        "notes": list(parsed.data["notes"]),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_pipeline(
    text: str,
    *,
    csv_path: Path | str | None = None,
    tenant: str | None = None,
    snapshot: Path | str | None = None,
    plan_preview: bool = True,
    memory: bool = True,
) -> dict:
    """跑一条委托，返回可 JSON 化的轨迹。不写任何业务数据。"""

    text = (text or "").strip()
    trace: dict = {
        "text": text,
        "status": "ok",
        "stages": [],
        "warnings": [],
        "clarifications": [],
        "orders": [],
        "drafts": [],
        "memory": {},
        "risk": {},
    }
    if not text:
        trace["status"] = "empty"
        trace["warnings"].append("空委托，不派活")
        return trace

    # ---- Step 1 意图 -------------------------------------------------------
    result = classify(text)
    trace["intent"] = {
        "name": result.intent.value,
        "confidence": result.confidence,
        "slots": dict(result.slots),
        "missing": list(result.missing),
        "candidates": list(result.candidates),
    }
    trace["stages"].append("intent")

    if result.intent in (Intent.CLARIFY, Intent.UNKNOWN):
        trace["status"] = "clarify"
        trace["stages"].append("clarify")
        trace["clarifications"] = [
            {
                "slot": "intent",
                "question": "这条委托具体要做什么？",
                "resolution": "不猜，先问清（候选：%s）" % ("、".join(result.candidates) or "无"),
                "resolved": False,
            }
        ]
        return trace

    # ---- 上下文 ------------------------------------------------------------
    csv_target = Path(csv_path) if csv_path else SAMPLE_CSV
    ctx = demo_store_ctx()
    if snapshot:
        ctx, note = store_ctx_from_snapshot(Path(snapshot))
        if note:
            trace["warnings"].append(note)
    if result.intent in INGEST_INTENTS:
        ctx, note = context_for_ingest(ctx, csv_target)
        if note:
            trace["warnings"].append(note)

    # ---- Step 3 计划 -------------------------------------------------------
    outcome = plan_intent(result, ctx)
    trace["orders"] = [dict(item) for item in outcome.orders]
    if plan_preview:
        trace["stages"].append("plan")
    trace["plan"] = {
        "preview": plan_preview,
        "orders": len(outcome.orders),
        "reason": outcome.reason,
        "notes": list(outcome.notes),
        "assumed": dict((outcome.orders[0].get("slots") or {}).get("assumed") or {}) if outcome.orders else {},
        "pending_input": list((outcome.orders[0].get("slots") or {}).get("pending_input") or []) if outcome.orders else [],
        "price_bands": [
            {
                "product_id": order.get("product_id"),
                "low_cents": order.get("price_low_cents"),
                "high_cents": order.get("price_high_cents"),
            }
            for order in outcome.orders[:5]
        ],
    }
    if not plan_preview:
        trace["warnings"].append(
            "跳过计划预览：%d 处假设值未展示（assumed=%s）"
            % (len(trace["plan"]["assumed"]), ",".join(trace["plan"]["assumed"]) or "无")
        )

    # ---- 澄清 --------------------------------------------------------------
    market = str(result.slots.get("market") or ctx["default_market"]).upper()
    language = str(result.slots.get("language") or ctx["default_language"])
    trace["clarifications"] = clarifications(
        result, csv_target.name, len(ctx["products"]), outcome.orders
    )
    unresolved = [item for item in trace["clarifications"] if not item["resolved"]]
    if trace["clarifications"]:
        trace["stages"].append("clarify")

    # ---- Step 4 风险 -------------------------------------------------------
    trace["risk"] = risk_block(result.intent)
    trace["stages"].append("risk")

    # ---- Step 6 记忆 -------------------------------------------------------
    trace["memory"] = memory_block(text, tenant, memory)
    trace["stages"].append("memory")

    # ---- Step 7 上新 -------------------------------------------------------
    ingest = {
        "ok": False,
        "skipped": True,
        "reason": "",
        "rows": 0,
        "drafts": [],
        "by_status": {},
        "errors": [],
        "notes": [],
    }
    if unresolved:
        ingest["reason"] = "槽位没补齐，不进上新链路"
    elif result.intent not in INGEST_INTENTS:
        ingest["reason"] = f"意图 {result.intent.value} 不走供应商资料，草稿由对应产出物渲染"
    else:
        fact_texts = [item["content"] for item in trace["memory"].get("facts", [])][:MAX_DRAFT_FACTS]
        try:
            ingest_root, ingest_rel = resolve_source(csv_target)
        except ValueError as exc:
            ingest = {
                "ok": False,
                "skipped": False,
                "code": "bad_source",
                "error": str(exc),
                "rows": 0,
                "drafts": [],
                "by_status": {},
                "errors": [],
                "notes": [],
            }
            trace["warnings"].append(f"供应商资料路径不接受：{exc}")
        else:
            ingest = ingest_file(ingest_root, ingest_rel, market, language, fact_texts)
            ingest["skipped"] = False
            ingest["facts_used"] = len(fact_texts)
            trace["stages"].append("ingest")
            if not ingest["ok"]:
                trace["warnings"].append(f"上线链路失败：{ingest['error']}")

    trace["drafts"] = ingest["drafts"]
    trace["ingest"] = {key: value for key, value in ingest.items() if key != "drafts"}

    # 委托说 DE、资料写 US 这类冲突，草稿按资料市场生成，但同时标 market_conflict
    # 并降级成 needs_fill——资料是权威源，可"上错站点"不能由机器替人决定。
    trace["market_conflicts"] = [
        {"sku": draft["sku"], "draft_market": draft["market"], "requested_market": market}
        for draft in trace["drafts"]
        # blocked 的行已经整条不进工坊了，再报一次市场冲突只是噪音。
        if draft["status"] != "blocked" and draft["market"] != market
    ]
    for item in trace["market_conflicts"]:
        trace["warnings"].append(
            f"{item['sku']}：资料市场是 {item['draft_market']}，与委托的 {market} 不一致，已标 needs_fill 待确认"
        )

    # ---- 收口 --------------------------------------------------------------
    blocked = [draft for draft in trace["drafts"] if draft["status"] == "blocked"]
    needs_fill = [draft for draft in trace["drafts"] if draft["status"] == "needs_fill"]
    ready = [draft for draft in trace["drafts"] if draft["status"] == "ready"]
    trace["summary"] = {
        "orders": len(trace["orders"]),
        "rows": ingest["rows"],
        "drafts": len(trace["drafts"]),
        "ready": len(ready),
        "needs_fill": len(needs_fill),
        "blocked": len(blocked),
        "blocked_skus": [draft["sku"] for draft in blocked],
        "needs_fill_skus": [draft["sku"] for draft in needs_fill],
        "ready_skus": [draft["sku"] for draft in ready],
        "review_points": review_points(trace),
    }
    artifact = str(INTENT_SPECS[result.intent]["artifact"])
    if unresolved:
        trace["status"] = "clarify"
    elif not trace["orders"] and artifact in ORDER_ARTIFACTS:
        # 该派单却一条都没派出去（商品不在快照、没有价格带）：不能报 ok。
        trace["status"] = "no_order"
        if outcome.reason:
            trace["warnings"].append(f"没有派单：{outcome.reason}")
    elif not trace["drafts"] and result.intent in INGEST_INTENTS:
        trace["status"] = "no_draft"
    trace["stages"].append("close")
    return trace


def review_points(trace: dict) -> int:
    """要人动手的点：未决澄清 + 缺字段 + 合规阻断 + 补过的假设。

    「人工修改率」在评测里用这个数除以草稿数，是一个**代理指标**：它数的是
    "必须有人看一眼"的位置，不是真实编辑次数。口径写在这里，免得每次算的时候
    换一种数法。
    """

    points = len([item for item in trace.get("clarifications") or [] if not item["resolved"]])
    for draft in trace.get("drafts") or []:
        points += len(draft.get("missing") or [])
        if draft.get("status") == "blocked":
            points += 1
    points += len((trace.get("plan") or {}).get("assumed") or {})
    return points


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _line(title: str) -> str:
    return f"\n── {title} " + "─" * max(2, 54 - len(title) * 2)


def render(trace: dict) -> str:
    out: list[str] = []
    out.append("═" * 66)
    out.append("委托：" + (trace.get("text") or "(空)"))
    out.append("═" * 66)

    intent = trace.get("intent") or {}
    if intent:
        out.append(
            "意图（Step 1）：%s  conf=%.2f  slots=%s  缺=%s"
            % (
                intent["name"],
                intent["confidence"],
                json.dumps(intent["slots"], ensure_ascii=False),
                ",".join(intent["missing"]) or "无",
            )
        )
    if trace["status"] == "clarify" and not trace.get("drafts"):
        out.append("")
        out.append("→ 停下来问：")
        for item in trace.get("clarifications") or []:
            out.append(f"   · {item['question']} —— {item['resolution']}")
        return "\n".join(out)

    plan = trace.get("plan") or {}
    if plan:
        out.append(_line("Step 3 计划"))
        if plan.get("preview"):
            out.append(f"出单 {plan['orders']} 条；{plan.get('reason') or '按意图派单'}")
            for band in plan.get("price_bands") or []:
                out.append(
                    f"   · {band['product_id']} 价格带 {band['low_cents']}–{band['high_cents']} 分"
                )
            if plan.get("assumed"):
                out.append("   补过的值（执行前需人确认）：" + "、".join(plan["assumed"]))
            if plan.get("pending_input"):
                out.append("   还缺的输入：" + "、".join(plan["pending_input"]))
            for note in plan.get("notes") or []:
                out.append("   · " + note)
        else:
            out.append(f"已关闭预览，直接按意图派单 {plan['orders']} 条")

    if trace.get("clarifications"):
        out.append(_line("槽位澄清（Step 1/3）"))
        for item in trace["clarifications"]:
            mark = "已解决" if item["resolved"] else "待回答"
            out.append(f"   · [{mark}] {item['question']} → {item['resolution']}")

    risk = trace.get("risk") or {}
    if risk:
        out.append(_line(f"Step 4 风险（上限 {risk['ceiling']}，归属 {risk['owner']}）"))
        for row in risk["tools"]:
            flag = "可见" if row["visible"] else "不可见"
            approve = "需确认" if row["requires_approval"] else ""
            note = f"（{row['note']}）" if row.get("note") else ""
            out.append(
                f"   · {row['name']:<18}{row['level']}  {row['side_effect'] or '-':<8}{flag} {approve}{note}"
            )
        if risk["hidden"]:
            out.append("   隐藏：" + "、".join(risk["hidden"]))

    mem = trace.get("memory") or {}
    if mem:
        out.append(_line("Step 6 作业记忆"))
        if not mem.get("enabled"):
            out.append("   已关闭记忆，注入 0 条")
        else:
            out.append(
                "   召回 %d 条（关键词候选 %d 条），%d tokens（预算 %d）%s"
                % (
                    mem["count"],
                    mem.get("candidates", mem["count"]),
                    mem["tokens"],
                    mem["budget_tokens"],
                    "，已截断" if mem["truncated"] else "",
                )
            )
            for item in mem["facts"]:
                out.append(
                    f"   · [{item['scope']}] {item['content']}（{item['source_label']}，score {item['score']}）"
                )
            if mem.get("pinned"):
                out.append(f"   （其中 {mem['pinned']} 条为店铺级规则，按 scope 固定注入）")
            if mem.get("empty"):
                out.append("   （租户 facts 为空：记忆目录里还没有任何记录）")

    ingest = trace.get("ingest") or {}
    if ingest:
        out.append(_line("Step 7 上新"))
        if ingest.get("skipped"):
            out.append("   跳过：" + (ingest.get("reason") or ""))
        elif not ingest.get("ok"):
            out.append(f"   失败：{ingest.get('code')} {ingest.get('error')}")
        else:
            out.append(
                "   解析 %s：%d 行（忽略列：%s）"
                % (ingest.get("path"), ingest.get("rows"), ",".join(ingest.get("ignored_columns") or []) or "无")
            )
            for draft in trace.get("drafts") or []:
                flags = ",".join(draft.get("flags") or []) or "-"
                out.append(
                    f"   · {draft['sku']:<8}{draft['status']:<11}missing={','.join(draft.get('missing') or []) or '无':<16}flags={flags}"
                )
                if draft["status"] == "blocked":
                    for hit in ((draft.get("compliance") or {}).get("violations") or [])[:2]:
                        out.append(
                            "       阻断依据：%s %s（quote=%s）"
                            % (hit.get("rule_id"), hit.get("message"), hit.get("quote"))
                        )
                if draft.get("unverified_numbers"):
                    out.append("       未溯源数字：" + "、".join(draft["unverified_numbers"]))

    summary = trace.get("summary") or {}
    if summary:
        out.append(_line("收口"))
        out.append(
            "草稿 %d 条：ready %d，needs_fill %d，blocked %d；要人处理 %d 处"
            % (
                summary["drafts"],
                summary["ready"],
                summary["needs_fill"],
                summary["blocked"],
                summary["review_points"],
            )
        )
        if summary["blocked_skus"]:
            out.append("阻断：" + "、".join(summary["blocked_skus"]) + "（不进工坊）")
        if summary["needs_fill_skus"]:
            out.append("需补资料：" + "、".join(summary["needs_fill_skus"]))
        if summary["ready_skus"]:
            out.append("可进工坊：" + "、".join(summary["ready_skus"]))
        if trace.get("warnings"):
            for item in trace["warnings"]:
                out.append("注意：" + item)

    out.append("")
    out.append("（本脚本只读：data/ 零写入，未发布、未进工坊）")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="委托 → 计划 → 草稿 → 阻断 端到端演示")
    parser.add_argument("--text", action="append", default=None, help="委托原话，可重复")
    parser.add_argument("--source", type=Path, default=None, help="供应商 CSV，默认用仓库样例")
    parser.add_argument("--tenant", default=None, help="租户目录（读 <tenant>/memory/facts.jsonl）")
    parser.add_argument("--snapshot", type=Path, default=None, help="店铺快照，默认用演示上下文")
    parser.add_argument("--no-preview", action="store_true", help="关闭计划预览（消融用）")
    parser.add_argument("--no-memory", action="store_true", help="关闭记忆注入（消融用）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 轨迹而不是可读文本")
    args = parser.parse_args(argv)

    texts = [item for item in (args.text or []) if item.strip()]
    if not texts:
        texts = ["这 20 个新品上到美国站"]

    csv_path = args.source or SAMPLE_CSV
    if not csv_path.exists():
        print(f"找不到供应商资料：{csv_path}", file=sys.stderr)
        return 2
    try:
        resolve_source(csv_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    traces = [
        run_pipeline(
            text,
            csv_path=csv_path,
            tenant=args.tenant,
            snapshot=args.snapshot,
            plan_preview=not args.no_preview,
            memory=not args.no_memory,
        )
        for text in texts
    ]

    if args.json:
        print(json.dumps(traces if len(traces) > 1 else traces[0], ensure_ascii=False, indent=2))
    else:
        for index, trace in enumerate(traces):
            if index:
                print()
            print(render(trace))

    # 跑完但没出草稿（资料读不了、行全坏）或该派单却没派出去，不能算成功：
    # CI 里 exit 0 会被当成通过。
    failed = [trace for trace in traces if trace["status"] in ("no_draft", "no_order", "error")]
    for trace in failed:
        print(f"未完成：{trace['text']}（{trace['status']}）", file=sys.stderr)
    return 3 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
