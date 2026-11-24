"""意图结果 → 合约 dict 的映射（Step 3，dry-run 专用）。

这一层只做一件事：把 ``IntentResult`` 和一份**店铺上下文**翻译成可预览的
task order payload 列表。它不读文件、不调模型、不写 ``data/``，所有输出都能在
内存里复现，因此可以放心地在 CLI 里 --dry-run。

**产物是 dict，不是 dataclass。** 交接形状以 ``contracts.py`` 为准：这里直接
拼 payload 再交给 ``normalize_task_order()``，键名是 ``task_id`` /
``product_id`` / ``price_low_cents`` 这一套。``models.TaskOrder`` 已标 deprecated，
新代码不再实例化它——两套形状并存过一段时间，代价是每个消费方都要写一遍转换，
这笔账在 Step 3 就还掉，不留到后面。

四个约定，后面几步都依赖：

1. **能不能出单，看 artifact，不看意图名。**
   ``INTENT_SPECS[intent]["artifact"]`` 是 ``task_orders`` / ``task_order`` /
   ``draft_set`` / ``asset_briefs`` 的，内容工坊有活可干，才产单；
   ``diagnosis`` / ``answer`` / ``price_band`` / ``memory_record`` /
   ``outreach_draft`` 这些是别的产出物，本层返回空列表 + 原因。

2. **缺槽位补默认值，但补过的必须留痕。**
   补进去的值写进 ``slots``，同时在 ``slots["assumed"]`` 里记一笔
   ``{槽位名: True}``。执行前要让用户看见"这两个值是我替他定的"。

3. **产出一律 ``status="draft"``。**
   本层给的是计划草案：没有 evidence、没有 run_id、没定稿。合约层对 draft 豁免
   evidence 下限，但价格带、变体区间、窗口期这些照旧要合法；工坊会拒绝 draft 单，
   所以"草稿意外被执行"这条路在两层都被堵住。

4. **不满足最小条件就返回空列表 + 原因，不抛异常。**
   CLI 拿到 ``outcome.reason`` 直接给用户看，Step 5 的委托台也读同一个字段。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..contracts import normalize_task_order, validate_task_order
from ..models import Market, Problem
from .schema import INTENT_SPECS, Intent, IntentResult

# 一次委托最多出多少条单。批量上新时说"200 个新品"，预览不该刷屏；
# 超出部分只报数量，真正执行（Step 7）再分批。
MAX_ORDERS = 20

DEFAULT_MARKET = Market.US
DEFAULT_LANGUAGE = "en"
DEFAULT_MARGIN_FLOOR = 0.35
DEFAULT_BUDGET_TOKENS = 60000

# 草稿阶段先给一组保守的默认值：变体 2—3 版、观察窗口 14 天、最小提升 10%。
# 这些都要过合约校验，和 director 的默认口径保持一致，避免预览与执行两套数字。
DEFAULT_VARIANT_MIN = 2
DEFAULT_VARIANT_MAX = 3
DEFAULT_WINDOW_DAYS = 14
DEFAULT_MIN_LIFT = 0.1

# 价格带以当前价为锚，上下各 10%。不认识的商品不做价格猜测。
BAND_LOW_RATIO = 0.90
BAND_HIGH_RATIO = 1.10

# 委托原文在订单里只留一段摘要：完整原话进 history.jsonl，订单里放全文会让
# 每次预览的 JSON 体积翻几倍，而且订单之后会被落进 task_orders.json。
INTAKE_TEXT_LIMIT = 200

# 产出物是这些形状的意图，才是"给内容工坊派单"。
ORDER_ARTIFACTS: frozenset[str] = frozenset({"task_orders", "task_order", "draft_set", "asset_briefs"})

# 意图 → (问题标签, 假设, 成功指标)。文案口径与 supervisor.PLAYBOOK 对齐，
# 目的是让预览里看到的话和规则基线说的是同一套话。
PLAYBOOK: dict[Intent, tuple[Problem, str, str]] = {
    Intent.BATCH_UPLOAD: (Problem.LOW_IMPRESSION, "新品缺曝光，标题与关键词未覆盖搜索词", "PV +20%"),
    Intent.SINGLE_UPLOAD: (Problem.LOW_IMPRESSION, "单条上新，先解决可发现性", "PV +20%"),
    Intent.LISTING_REWRITE: (Problem.LOW_CTR, "主图/标题与搜索意图不匹配，点击被浪费", "CTR +15%"),
    Intent.MULTILANG_CONTENT: (Problem.LOW_IMPRESSION, "目标市场语言不通，曝光拿不到", "PV +20%"),
    Intent.MEDIA_COMPOSE: (Problem.LOW_CTR, "首图信息量不足，点击意愿低", "CTR +15%"),
}

# 诊断类要按症状选问题标签，不能一律按"转化差"。
_PROBLEM_HINTS: tuple[tuple[str, Problem], ...] = (
    (r"曝光|流量|没点击|看不到|搜不到", Problem.LOW_IMPRESSION),
    (r"点击率|ctr|没人点", Problem.LOW_CTR),
    (r"退货|退款|差评|投诉|纠纷", Problem.HIGH_RETURN),
    (r"库存|断货|积压|压货", Problem.STOCK_RISK),
    (r"转化|下单|不出单|成交", Problem.LOW_CVR),
)

# 文档里写明"店铺上下文"长什么样，CLI 按这个结构组装，测试也按它造。
STORE_CTX_KEYS = (
    "store_slug",
    "default_market",
    "default_language",
    "margin_floor",
    "budget_tokens",
    "products",
)


@dataclass
class MappingOutcome:
    """映射结果：订单 + 为什么没有订单 + 需要提醒用户的事。"""

    # 每条都是 contracts.normalize_task_order 的产物，键名 task_id / product_id …。
    orders: list[dict] = field(default_factory=list)
    reason: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.orders)

    def to_dict(self) -> dict:
        from ..models import jsonable

        return {
            "orders": [jsonable(order) for order in self.orders],
            "reason": self.reason,
            "notes": list(self.notes),
        }


def to_task_orders(result: IntentResult, store_ctx: dict | None = None) -> list[dict]:
    """PRD 约定的入口：能出单就出单，出不了就给空列表（原因见 plan_intent）。"""

    return plan_intent(result, store_ctx).orders


def plan_intent(result: IntentResult, store_ctx: dict | None = None) -> MappingOutcome:
    """完整版入口：除了订单，还把"为什么"带出来。"""

    if not isinstance(result, IntentResult):
        raise TypeError("result must be IntentResult")
    ctx = _store_ctx(store_ctx)

    if result.intent is Intent.CLARIFY:
        return MappingOutcome(reason="意图未定，先问清再派活（不做猜测性执行）")
    if result.intent is Intent.UNKNOWN:
        return MappingOutcome(reason="没听出业务意图，不派活")

    spec = INTENT_SPECS[result.intent]
    if spec["artifact"] not in ORDER_ARTIFACTS:
        return MappingOutcome(
            reason=f"产出物是 {spec['artifact']}，不是 task order，本层不派单",
            notes=[f"该意图的产出物由委托台按 {spec['artifact']} 渲染"],
        )

    slots, assumed, pending = _resolve_slots(result, ctx)
    products = _candidates(ctx)
    if not products:
        return MappingOutcome(
            reason="店铺上下文里没有商品候选，无法绑定 SKU",
            notes=["dry-run 需要快照或店铺上下文；没有候选就不编 SKU"],
        )

    picked, pick_notes = _pick_products(result, slots, products)
    if not picked:
        return MappingOutcome(reason=pick_notes[0] if pick_notes else "没有可绑定的商品", notes=pick_notes[1:])

    orders: list[dict] = []
    notes: list[str] = list(pick_notes)
    for product in picked:
        order, why = _build_order(result, slots, assumed, pending, product, ctx)
        if order is None:
            notes.append(why)
            continue
        orders.append(order)

    if not orders:
        return MappingOutcome(reason="候选商品都缺价格信息，无法给出价格带", notes=notes)

    if len(orders) > 1:
        for index, order in enumerate(orders, start=1):
            order["slots"]["batch_index"] = index
            order["slots"]["batch_size"] = len(orders)

    return MappingOutcome(orders=orders, notes=notes)


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _store_ctx(raw: dict | None) -> dict:
    """把外部传进来的上下文收敛成固定形状，缺的补默认值。"""

    ctx = dict(raw or {})
    market = _as_market(ctx.get("default_market"))
    language = str(ctx.get("default_language") or DEFAULT_LANGUAGE).strip() or DEFAULT_LANGUAGE
    try:
        margin_floor = float(ctx.get("margin_floor", DEFAULT_MARGIN_FLOOR))
    except (TypeError, ValueError):
        margin_floor = DEFAULT_MARGIN_FLOOR
    if not 0 <= margin_floor < 1:
        margin_floor = DEFAULT_MARGIN_FLOOR
    try:
        budget_tokens = int(ctx.get("budget_tokens", DEFAULT_BUDGET_TOKENS))
    except (TypeError, ValueError):
        budget_tokens = DEFAULT_BUDGET_TOKENS
    if budget_tokens <= 0:
        budget_tokens = DEFAULT_BUDGET_TOKENS
    return {
        "store_slug": str(ctx.get("store_slug") or ""),
        "default_market": market,
        "default_language": language,
        "margin_floor": margin_floor,
        "budget_tokens": budget_tokens,
        "products": [_normalize_product(item) for item in ctx.get("products") or []],
    }


def _normalize_product(item) -> dict:
    """商品候选统一成 dict；认不出的条目留空字段，由下游决定要不要用。"""

    if not isinstance(item, dict):
        return {
            "product_id": str(item or ""),
            "title": "",
            "status": "",
            "price_cents": 0,
            "cost_cents": None,
        }
    return {
        "product_id": str(item.get("product_id") or item.get("sku_id") or item.get("sku") or "").strip(),
        "title": str(item.get("title") or item.get("title_en") or "").strip(),
        "status": str(item.get("status") or "").strip().lower(),
        "price_cents": _as_int(item.get("price_cents")),
        "cost_cents": _as_int(item.get("cost_cents")) or None,
    }


def _resolve_slots(result: IntentResult, ctx: dict) -> tuple[dict, dict, list[str]]:
    """补默认值 + 记录哪些是补的 + 哪些还等着执行时再给。"""

    slots: dict = {}
    assumed: dict[str, bool] = {}
    pending: list[str] = []

    spec_slots = INTENT_SPECS[result.intent]["slots"]
    for name in spec_slots:
        if name in result.slots:
            slots[name] = result.slots[name]

    # 意图里带的其它槽位（如 quantity、metrics）一并保留，它们对拆单有用。
    for name, value in result.slots.items():
        slots.setdefault(name, value)

    if "market" in spec_slots and "market" not in slots:
        slots["market"] = ctx["default_market"].value
        assumed["market"] = True
    if "language" in spec_slots and "language" not in slots:
        slots["language"] = ctx["default_language"]
        assumed["language"] = True
    if "languages" in spec_slots:
        languages = slots.get("languages")
        if not isinstance(languages, list) or not languages:
            slots["languages"] = [ctx["default_language"]]
            assumed["languages"] = True
        else:
            slots["languages"] = [str(item) for item in languages]

    # 这些东西执行时才给：源文件、客户身份、字段名。dry-run 不拦。
    for name in spec_slots:
        if name not in slots:
            pending.append(name)

    slots["assumed"] = assumed
    slots["pending_input"] = pending
    return slots, assumed, pending


def _candidates(ctx: dict) -> list[dict]:
    """草稿品排前面：批量上新的候选本来就该是还没上架的那些。"""

    products = [item for item in ctx["products"] if item["product_id"]]
    return sorted(products, key=lambda item: 0 if item["status"] not in ("active", "on_sale") else 1)


def _pick_products(result: IntentResult, slots: dict, products: list[dict]) -> tuple[list[dict], list[str]]:
    """按意图决定要绑几个商品；说不清是哪几个就不猜。"""

    explicit = _explicit_skus(slots)
    if explicit:
        by_id = {item["product_id"]: item for item in products}
        by_title = {item["title"]: item for item in products if item["title"]}
        picked: list[dict] = []
        missing: list[str] = []
        for name in explicit:
            hit = by_id.get(name) or by_title.get(name)
            if hit is None:
                missing.append(name)
            else:
                picked.append(hit)
        if picked:
            return picked, [f"{name} 不在快照里，已跳过" for name in missing]
        # 站点商品 id 不一定是可读的 SKU 号（演示快照是 cuid），用户更可能直接
        # 复制粘贴 id 或商品名。原话里点名了哪条，就按哪条来。
        mentioned = _mentioned_products(result.raw_text, products)
        if mentioned:
            return mentioned, [f"{name} 不在快照里，已跳过" for name in missing]
        return [], [f"指定的 {', '.join(explicit)} 不在店铺快照里，无法确定价格带"]

    if result.intent is Intent.BATCH_UPLOAD:
        quantity = _as_int(slots.get("quantity"))
        notes: list[str] = []
        if quantity <= 0:
            quantity = len(products)
            notes.append(f"没说数量，按候选商品数 {quantity} 条预演")
        if quantity > len(products):
            notes.append(f"要 {quantity} 条，候选只有 {len(products)} 条，先出 {len(products)} 条")
            quantity = len(products)
        if quantity > MAX_ORDERS:
            notes.append(f"单次预览上限 {MAX_ORDERS} 条，其余走分批")
            quantity = MAX_ORDERS
        notes.append(f"绑定候选前 {quantity} 条商品")
        return products[:quantity], notes

    # 单条委托必须指到具体商品。原话里点名了（粘贴 id / 商品名）就按点名的来。
    mentioned = _mentioned_products(result.raw_text, products)
    if mentioned:
        return mentioned, ["按原话里点到的商品绑定"]
    # 候选只有一个时不算猜——那就是它；多个就必须问。
    if len(products) == 1:
        return products, ["只有一条候选商品，按它预演"]
    return [], [f"单条委托必须指定商品（缺 sku），候选有 {len(products)} 条，不替用户挑"]


def _mentioned_products(text: str, products: list[dict]) -> list[dict]:
    """原话里直接出现商品 id 或商品名，就认为用户点的是它。"""

    lowered = (text or "").lower()
    if not lowered:
        return []
    hits: list[dict] = []
    for item in products:
        product_id = item["product_id"].lower()
        title = item["title"].lower()
        if (product_id and product_id in lowered) or (len(title) >= 8 and title in lowered):
            hits.append(item)
    return hits


def _explicit_skus(slots: dict) -> list[str]:
    """商品要么写在 sku，要么写成 sku_ids 列表，两种都认。"""

    value = slots.get("sku")
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    value = slots.get("sku_ids")
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _build_order(
    result: IntentResult,
    slots: dict,
    assumed: dict,
    pending: list[str],
    product: dict,
    ctx: dict,
) -> tuple[dict | None, str]:
    """一条商品 → 一条合约 dict；缺价格 / 不合合约就返回 None + 原因。"""

    price_cents = product["price_cents"]
    if price_cents <= 0:
        return None, f"{product['product_id']} 没有价格，跳过（不编价格带）"

    problem = _problem_for(result)
    low, high = _price_band_cents(price_cents, product["cost_cents"], ctx["margin_floor"])
    if high <= low:
        # 价格带被毛利线下沿顶成零宽：合约要至少两个不同价位点，放宽 1 分钱
        # 比给出重复点位更诚实（后者会被合约整条拒绝，单子直接消失）。
        high = low + 1
    points = _price_points(low, high, DEFAULT_VARIANT_MAX)

    order_slots = dict(slots)
    # 补过的槽位在执行前必须人工确认一遍；这里带上原样的一份 assumed 映射。
    order_slots["assumed"] = dict(assumed)
    # 商品已经绑到这一条单上了，sku 不再算"还缺的输入"（用户可能粘贴的是站点 id）。
    order_slots["pending_input"] = [name for name in pending if name != "sku"]

    entry = PLAYBOOK.get(result.intent)
    if entry is not None:
        hypothesis, metric = entry[1], entry[2]
    else:
        hypothesis, metric = f"先解决 {problem.value}，再谈放量", "CTR +10%"

    raw_text = result.raw_text or ""
    payload = {
        # 合约键名：task_id / product_id / price_low_cents，不再用 order_id / sku_id。
        "task_id": f"{result.intent.value}:{product['product_id']}",
        "run_id": "",
        "product_id": product["product_id"],
        "title": product["title"],
        "category": "",
        "problem": problem.value,
        # 合约要求 problem_detail 非空：草稿里用假设句占位，执行前由工坊细化。
        "problem_detail": hypothesis,
        "evidence": [],
        "hypothesis": hypothesis,
        "success_metric": metric,
        "min_lift": DEFAULT_MIN_LIFT,
        "window_days": DEFAULT_WINDOW_DAYS,
        "price_low_cents": low,
        "price_high_cents": high,
        "price_points": points,
        "variant_min": DEFAULT_VARIANT_MIN,
        "variant_max": DEFAULT_VARIANT_MAX,
        "must_include": [],
        "forbidden": [],
        # 以下四个字段只在意图层出现，由合约层按 evidence_refs 的先例透传。
        "source": "intent",
        "status": "draft",
        "slots": order_slots,
        "intake": {
            "intent": result.intent.value,
            "confidence": round(float(result.confidence), 2),
            "artifact": INTENT_SPECS[result.intent]["artifact"],
            "text": raw_text[:INTAKE_TEXT_LIMIT],
            "text_truncated": len(raw_text) > INTAKE_TEXT_LIMIT,
        },
    }

    doc = normalize_task_order(payload)
    errors = validate_task_order(doc)
    if errors:  # 单条坏掉不该炸整批
        return None, f"{product['product_id']} 不合合约：{errors[0]}"
    return doc, ""


def _problem_for(result: IntentResult) -> Problem:
    if result.intent is Intent.LISTING_DIAGNOSE:
        text = f"{result.slots.get('problem') or ''}{result.raw_text}"
        for pattern, problem in _PROBLEM_HINTS:
            if re.search(pattern, text, re.IGNORECASE):
                return problem
        return Problem.LOW_CVR
    if result.intent is Intent.CUSTOMER_OUTREACH:
        return Problem.HIGH_RETURN
    return PLAYBOOK.get(result.intent, (Problem.LOW_CVR, "", ""))[0]


def _price_band_cents(price_cents: int, cost_cents: int | None, margin_floor: float) -> tuple[int, int]:
    """以当前价为锚上下浮动，但下沿不得跌破毛利线。单位是分，和合约一致。"""

    low = int(round(price_cents * BAND_LOW_RATIO))
    high = int(round(price_cents * BAND_HIGH_RATIO))
    if cost_cents and margin_floor < 1:
        floor_cents = math.ceil(cost_cents / (1 - margin_floor))
        low = max(low, floor_cents)
    high = max(high, low)
    return max(1, low), max(1, high)


def _price_points(low: int, high: int, count: int) -> list[int]:
    """在价格带内均匀取 ``count`` 个**互不相同**的价位点。

    合约要求 price_points 非空、落在带内、不重复。价格带只有 1 分钱宽时
    （极端低价商品），放宽 1 分钱比返回重复价位更诚实：后者会被合约整条拒绝。
    """

    count = max(2, int(count))
    low = max(1, int(low))
    high = max(low, int(high))
    if high == low:
        high = low + 1
    span = high - low + 1
    if span < count:
        return list(range(low, high + 1))
    step = (high - low) / (count - 1)
    return [int(round(low + step * index)) for index in range(count)]


def _as_market(value) -> Market:
    if isinstance(value, Market):
        return value
    text = str(value or "").strip().upper()
    try:
        return Market(text)
    except ValueError:
        return DEFAULT_MARKET


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
