"""大模型意图识别引擎。

引擎选择走环境变量 ``INTAKE_ENGINE``：

- ``rules`` 只用规则版（确定性、零成本，评测/CI 默认）；
- ``llm``   只用大模型，失败也回退规则（控制台永不因模型挂掉而 500）；
- ``auto``  默认。配置了可用的大模型就走大模型，出错回退规则。

失败回退带**熔断**：模型连续失败（欠费、超时、限流）后 5 分钟内不再尝试，
后续委托直接走规则——欠费时不能让每一次委托都白等一轮重试。

安全设计（模型输出永远不可信）：
- 意图必须命中 12 个枚举之一，否则整体判失败回退规则；
- 槽位键只放行规格表声明过的（外加 ``quantity``），未知键直接丢弃；
- 置信度 clamp 到 [0, 1]；candidates 只留合法意图名、最多 2 个；
- ``missing`` 由程序按规格表重算，不信模型自己报的"缺什么"。

成本控制：temperature=0、max_tokens 封顶、输入截断 2000 字、空输入不调模型。
"""

from __future__ import annotations

import os
import re
import threading
import time

from ..llm import LLMClient, LLMError
from .router import MAX_TEXT_CHARS, _missing_slots, classify
from .schema import INTENT_SPECS, Intent, IntentResult

ENGINE_ENV = "INTAKE_ENGINE"

# 温度 0：分类要的是稳定，不要"创造性"。
LLM_TEMPERATURE = 0.0
# 意图 + 槽位 + 简短 reason，400 tokens 足够；封顶防止模型跑题烧钱。
LLM_MAX_TOKENS = 400

# 熔断窗口：连续失败后冷却 5 分钟再试，避免欠费 key 拖垮每一次委托。
_COOLDOWN_SECONDS = 300.0
_cooldown_until = 0.0
_cooldown_lock = threading.Lock()

# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------

_INTENT_HELP: dict[Intent, str] = {
    Intent.BATCH_UPLOAD: "批量上新：把供应商 CSV/表格里的一批商品上架到站点",
    Intent.SINGLE_UPLOAD: "单品上新：上架/发布一款指定商品",
    Intent.LISTING_DIAGNOSE: "链接诊断：不出单/没流量/转化差/掉量，找原因",
    Intent.LISTING_REWRITE: "改写文案：重写标题/五点/详情/描述/卖点",
    Intent.MULTILANG_CONTENT: "多语言内容：把商品文案翻译成其他语言",
    Intent.MEDIA_COMPOSE: "素材制作：主图/场景图/视频/海报等图片视频",
    Intent.DATA_QUERY: "数据查询：查指标、问清单、看报表（曝光/点击/转化/销量/库存/排名）",
    Intent.PRICE_SUGGEST: "定价建议：定价/调价/价格带",
    Intent.MEMORY_MANAGE: "记忆管理：让系统记住或忘记一条规则偏好",
    Intent.CUSTOMER_OUTREACH: "客户触达：催评/邀评/售后邮件（本期只识别不执行）",
    Intent.CLARIFY: "反问：信息不足、或一次点了两件不同的活",
    Intent.UNKNOWN: "无法归类：与店铺运营无关的闲聊或乱码",
}

_SLOT_VOCAB = """- market: US（美国/美站）、DE（德国/欧洲站）、UK（英国）
- language / languages: en 英语、de 德语、fr 法语、ja 日语、es 西语、it 意语
- field: title 标题、bullets 五点/卖点、description 详情/描述
- asset_types: 主图/场景图/白底图/详情图/短视频/海报（用原词，列表）
- metrics: 曝光/点击率/转化率/销量/销售额/退货率/库存（用原词，列表）
- action: remember 记住、forget 忘记
- quantity: 数量（整数）
- sku: 原样保留编码，如 FT-001、SKU-123"""

_EXAMPLES = """{"intent":"BATCH_UPLOAD","confidence":0.95,"slots":{"quantity":20,"market":"US"},"candidates":[],"reason":"批量上新到美国站"}  ← 这 20 个新品上到美国站
{"intent":"LISTING_REWRITE","confidence":0.93,"slots":{"sku":"FT-001","field":"title"},"candidates":[],"reason":"改写指定商品标题"}  ← 把 FT-001 的标题重写一下
{"intent":"MULTILANG_CONTENT","confidence":0.9,"slots":{"languages":["de","fr"]},"candidates":[],"reason":"翻译成德法双语"}  ← 帮我把这个商品翻译成德语和法语
{"intent":"DATA_QUERY","confidence":0.92,"slots":{"metrics":["曝光","转化率"]},"candidates":[],"reason":"查数据指标"}  ← 帮我看看最近 7 天的曝光和转化率
{"intent":"CLARIFY","confidence":0.55,"slots":{},"candidates":["MEDIA_COMPOSE","LISTING_REWRITE"],"reason":"一次点了两件活，需确认先做哪个"}  ← 帮我弄弄这个标题和图片
{"intent":"MEMORY_MANAGE","confidence":0.94,"slots":{"action":"remember","content":"美国站标题不要用感叹号"},"candidates":[],"reason":"让系统记住一条规则"}  ← 以后美国站标题不要用感叹号
{"intent":"UNKNOWN","confidence":0.1,"slots":{},"candidates":[],"reason":"与店铺运营无关"}  ← 今天天气怎么样"""


def _system_prompt() -> str:
    """从 INTENT_SPECS 生成系统提示词。规格表是唯一事实源，prompt 跟着表走。"""
    lines = [
        "你是跨境电商运营系统的意图识别模块。把一句卖家委托分类到 12 个意图之一，并抽取槽位。",
        "",
        "## 意图清单",
    ]
    for intent, help_text in _INTENT_HELP.items():
        slots = "、".join(INTENT_SPECS[intent]["slots"]) or "（无）"
        lines.append(f"- {intent.value}：{help_text}。常用槽位：{slots}")
    lines += [
        "",
        "## 槽位词表",
        _SLOT_VOCAB,
        "",
        "## 规则",
        "1. 只输出一个 JSON 对象："
        '{"intent":"...","confidence":0.0到1.0,"slots":{...},"candidates":[...],"reason":"一句话"}',
        "2. slots 只放原话里有证据的值，绝不猜测补全；没有证据就留空对象。",
        "3. 委托同时点了两件不同的活且没有说明先后（如“标题和图片都弄一下”）→ "
        "intent 用 CLARIFY，candidates 列出两个最可能的方向。",
        "4. 方向清楚但缺关键信息（改写没说商品、上新没说市场）→ 照常判意图，缺什么系统会去问。",
        "5. 想派活但说不清要干什么 → CLARIFY；纯闲聊/与运营无关 → UNKNOWN，candidates 留空。",
        "6. confidence：方向明确 ≥0.9；方向对但细节缺 0.7—0.9；在猜 ≤0.6。",
        "",
        "## 示例（← 后面是委托原文）",
        _EXAMPLES,
    ]
    return "\n".join(lines)


_SYSTEM_PROMPT = _system_prompt()

# ---------------------------------------------------------------------------
# 输出消毒
# ---------------------------------------------------------------------------

# 槽位键白名单：规格表声明的全部槽位 + quantity（委托量，计划层要用但不入规格表）。
_ALLOWED_SLOT_KEYS = frozenset(
    {name for spec in INTENT_SPECS.values() for name in spec["slots"]} | {"quantity"}
)
_STR_SLOTS = frozenset(
    {"market", "language", "sku", "source_path", "field", "action", "content", "customer", "topic", "problem"}
)
_LIST_SLOTS = frozenset({"languages", "asset_types", "metrics"})
_LIST_SPLIT = re.compile(r"[,，、/]")


def _sanitize_slots(raw) -> dict:
    """模型给的槽位只留白名单键，值型不对就丢，绝不静默纠正语义。"""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key, value in raw.items():
        key = str(key).strip()
        if key not in _ALLOWED_SLOT_KEYS:
            continue
        if key in _LIST_SLOTS:
            if isinstance(value, str):
                items = [item.strip() for item in _LIST_SPLIT.split(value) if item.strip()]
            elif isinstance(value, list):
                items = [str(item).strip() for item in value if str(item).strip()]
            else:
                continue
            if items:
                out[key] = items
        elif key == "quantity":
            match = re.search(r"\d{1,4}", str(value))
            if match:
                out["quantity"] = int(match.group(0))
        else:
            text = str(value).strip()
            if not text:
                continue
            if key == "market":
                text = text.upper()
            elif key == "language":
                text = text.lower()
            out[key] = text
    return out


def _sanitize_candidates(raw, intent: Intent) -> list[str]:
    """candidates 只留合法意图名，排除主意图，最多 2 个（与规则版口径一致）。"""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        name = str(item or "").strip().upper()
        if name in Intent._value2member_map_ and name != intent.value and name not in out:
            out.append(name)
        if len(out) >= 2:
            break
    return out


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


def engine_mode() -> str:
    """当前引擎模式。非法值一律按 auto 处理——配置写错不能变成规则静默丢失。"""
    value = (os.environ.get(ENGINE_ENV) or "auto").strip().lower()
    return value if value in {"auto", "llm", "rules"} else "auto"


def llm_available(client: LLMClient | None = None) -> bool:
    """大模型现在能不能用：配置齐 + 不在熔断冷却期。"""
    if _cooling_down():
        return False
    client = client or LLMClient()
    return client.enabled


def _cooling_down() -> bool:
    with _cooldown_lock:
        return time.monotonic() < _cooldown_until


def _trip_cooldown() -> None:
    with _cooldown_lock:
        global _cooldown_until
        _cooldown_until = time.monotonic() + _COOLDOWN_SECONDS


def _reset_cooldown() -> None:
    with _cooldown_lock:
        global _cooldown_until
        _cooldown_until = 0.0


def classify_llm(text: str, defaults: dict | None = None, client: LLMClient | None = None) -> IntentResult | None:
    """大模型识别一次。任何失败返回 None（不抛），由调用方决定回退。

    空输入直接返回 UNKNOWN，一次模型调用都不花。
    """
    raw = "" if text is None else str(text)
    trimmed = raw.strip()
    if not trimmed:
        return IntentResult(intent=Intent.UNKNOWN, confidence=0.0, raw_text=raw, engine="llm")

    client = client or LLMClient()
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": trimmed[:MAX_TEXT_CHARS]},
    ]
    payload, _reply = client.chat_json(messages, temperature=LLM_TEMPERATURE, max_tokens=LLM_MAX_TOKENS)

    intent_name = str(payload.get("intent") or "").strip().upper()
    if intent_name not in Intent._value2member_map_:
        # 意图是所有下游决策的根：模型编了一个不存在的意图，整条输出都不可信。
        return None
    intent = Intent(intent_name)

    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = round(min(1.0, max(0.0, confidence)), 2)

    slots = _sanitize_slots(payload.get("slots"))
    spec = INTENT_SPECS[intent]
    return IntentResult(
        intent=intent,
        confidence=confidence,
        slots=slots,
        missing=_missing_slots(spec, slots, defaults),
        candidates=_sanitize_candidates(payload.get("candidates"), intent),
        raw_text=raw,
        engine="llm",
    )


def classify_auto(text: str, defaults: dict | None = None, client: LLMClient | None = None) -> IntentResult:
    """引擎感知的分类入口：控制台 / CLI 的活路径都走这里。

    ``rules`` 模式与空输入不碰模型；其余先试大模型，失败回退规则并带熔断。
    """
    raw = "" if text is None else str(text)
    mode = engine_mode()

    if mode == "rules" or not raw.strip():
        # 空输入连模型都不该碰：结果必然是 UNKNOWN，没必要烧一次调用。
        return classify(raw, defaults)

    client = client or LLMClient()
    if not llm_available(client):
        return classify(raw, defaults)

    try:
        result = classify_llm(raw, defaults, client)
    except Exception:
        # 捕 Exception 而不只是 LLMError：transport 是注入点，第三方实现可能
        # 抛任何东西。活路径上"静默回退规则 + 引擎标记"远好过让委托接口 500；
        # 回退结果带 rules(fallback) 标记，问题在响应里可见，不会被完全吞掉。
        result = None
    if result is not None:
        _reset_cooldown()
        return result

    # 失败（网络/欠费/坏输出）：进冷却期，本次回退规则。
    _trip_cooldown()
    fallback = classify(raw, defaults)
    fallback.engine = "rules(fallback)"
    return fallback
