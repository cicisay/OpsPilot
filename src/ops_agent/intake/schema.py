"""意图层的静态定义：意图枚举、识别结果、意图规格表。

本模块只做数据声明与自校验，不读文件、不调模型、不导入任何运行时组件，
因此可以在任何进程里安全 import。

`toolset` 只是"这个意图可能用到哪些工具"的**声明**，不构成任何授权：
全项目只有 `research` 角色经 `ToolGateway` 访问工具（见改造 PRD §1.1）。

按意图声明，**不设 role 维度**——角色不是意图的属性。工具授权只发生在
`research` 一处，`toolset` 只是给计划预览与 Step 4 的 `risk_ceiling` 做对齐用的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Intent(str, Enum):
    """12 个委托意图。

    值等于名字（而不是小写短语），这样写进 jsonl 语料、前端渲染、
    日志比对时都不用再做一次映射，Step 2 的评测集可直接逐字对比。
    """

    BATCH_UPLOAD = "BATCH_UPLOAD"
    SINGLE_UPLOAD = "SINGLE_UPLOAD"
    LISTING_DIAGNOSE = "LISTING_DIAGNOSE"
    LISTING_REWRITE = "LISTING_REWRITE"
    MULTILANG_CONTENT = "MULTILANG_CONTENT"
    MEDIA_COMPOSE = "MEDIA_COMPOSE"
    DATA_QUERY = "DATA_QUERY"
    MEMORY_MANAGE = "MEMORY_MANAGE"
    PRICE_SUGGEST = "PRICE_SUGGEST"
    CUSTOMER_OUTREACH = "CUSTOMER_OUTREACH"
    CLARIFY = "CLARIFY"
    UNKNOWN = "UNKNOWN"


# 风险等级，Step 4 才真正实现判定；这里只用于校验规格表里写的天花板是否合法。
RISK_LEVELS: tuple[str, ...] = ("R0", "R1", "R2", "R3")

# Step 7 才会落地的两个工具。此处先声明，是因为计划预览（Step 5）必须先看到
# 上新链路的工具，否则"批量上新"的预览里只剩一个 compliance_check。
# 声明不等于存在：本步不注册、不授权、不调用。
PLANNED_TOOLS: frozenset[str] = frozenset({"file_parse", "listing_draft"})


@dataclass
class IntentResult:
    """一句话委托的识别结果。

    字段语义（Step 3 的映射层依赖这个约定）：
    - ``slots``     只放**文中有证据**的信息，可能多于规格表要求的槽位
                    （例如 ``quantity``），但绝不包含凭猜测补出来的值。
    - ``missing``   只针对规格表的必填槽位，且已经排除 ``defaults`` 里能给的值。
    - ``candidates`` 次优意图：UNKNOWN 时是最接近的 2—3 个，歧义/复合时是其余达标的意图。
    - ``raw_text``  原始输入，不做截断（匹配只看前 2000 字）。
    - ``engine``    识别引擎："rules"（规则版）/"llm"（大模型）/"rules(fallback)"
                    （大模型失败后回退）。前端与历史记录靠它区分这条结果是模型还是规则给的。
    """

    intent: Intent = Intent.UNKNOWN
    confidence: float = 0.0
    slots: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    raw_text: str = ""
    engine: str = "rules"

    def __post_init__(self) -> None:
        if not isinstance(self.intent, Intent):
            raise ValueError(f"intent must be Intent: {self.intent!r}")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if not isinstance(self.slots, dict):
            raise ValueError("slots must be a dict")
        for name in ("missing", "candidates"):
            value = getattr(self, name)
            if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
                raise ValueError(f"{name} must be a list[str]")

    def to_dict(self) -> dict:
        """给 console / CLI 用的纯 JSON 形态（枚举转成字符串）。"""
        return {
            "intent": self.intent.value,
            "confidence": self.confidence,
            "slots": dict(self.slots),
            "missing": list(self.missing),
            "candidates": list(self.candidates),
            "raw_text": self.raw_text,
            "engine": self.engine,
        }


# ---------------------------------------------------------------------------
# 意图规格表
# ---------------------------------------------------------------------------

INTENT_SPECS: dict[Intent, dict] = {
    # --- 内容工坊 ---
    Intent.BATCH_UPLOAD: {
        "slots": ["source_path", "market", "language"],
        "artifact": "task_orders",
        "toolset": ["file_parse", "listing_draft", "compliance_check"],
        "risk_ceiling": "R1",
        "owner": "内容工坊",
    },
    Intent.SINGLE_UPLOAD: {
        "slots": ["sku", "market", "language"],
        "artifact": "task_order",
        "toolset": ["get_product", "listing_draft", "compliance_check"],
        "risk_ceiling": "R1",
        "owner": "内容工坊",
    },
    Intent.LISTING_REWRITE: {
        "slots": ["sku", "field"],
        "artifact": "draft_set",
        "toolset": ["get_product", "generate_listing", "compliance_check"],
        "risk_ceiling": "R1",
        "owner": "内容工坊",
    },
    Intent.MULTILANG_CONTENT: {
        "slots": ["sku", "languages"],
        "artifact": "draft_set",
        "toolset": ["get_product", "listing_draft", "compliance_check"],
        "risk_ceiling": "R1",
        "owner": "内容工坊",
    },
    Intent.MEDIA_COMPOSE: {
        "slots": ["sku", "asset_types"],
        "artifact": "asset_briefs",
        "toolset": ["get_product", "generate_assets", "compliance_check"],
        "risk_ceiling": "R1",
        "owner": "内容工坊",
    },
    # --- 主管 ---
    Intent.LISTING_DIAGNOSE: {
        "slots": ["sku", "problem"],
        "artifact": "diagnosis",
        "toolset": ["get_product", "get_metrics", "get_reviews", "get_benchmarks", "price_calc"],
        "risk_ceiling": "R0",
        "owner": "主管",
    },
    Intent.DATA_QUERY: {
        "slots": ["metrics"],
        "artifact": "answer",
        "toolset": [
            "get_shop_snapshot",
            "get_metrics",
            "list_products",
            "get_trends",
            "get_competitors",
            "get_product",
        ],
        "risk_ceiling": "R0",
        "owner": "主管",
    },
    Intent.PRICE_SUGGEST: {
        "slots": ["sku"],
        "artifact": "price_band",
        "toolset": ["get_product", "price_calc", "get_competitors", "get_benchmarks"],
        "risk_ceiling": "R0",
        "owner": "主管",
    },
    Intent.MEMORY_MANAGE: {
        "slots": ["action", "content"],
        "artifact": "memory_record",
        "toolset": ["search_memory"],
        "risk_ceiling": "R1",
        "owner": "主管",
    },
    # --- 本轮只声明、不实现（PRD §8） ---
    Intent.CUSTOMER_OUTREACH: {
        "slots": ["customer", "topic"],
        "artifact": "outreach_draft",
        "toolset": [],
        "risk_ceiling": "R0",
        "owner": "内容工坊",
    },
    # --- 兜底 ---
    Intent.CLARIFY: {
        "slots": [],
        "artifact": "clarification",
        "toolset": [],
        "risk_ceiling": "R0",
    },
    Intent.UNKNOWN: {
        "slots": [],
        "artifact": "none",
        "toolset": [],
        "risk_ceiling": "R0",
    },
}


_ALLOWED_KEYS = frozenset({"slots", "artifact", "toolset", "risk_ceiling", "owner"})


def _validate_specs(specs: dict[Intent, dict]) -> None:
    """导入即校验：宁可 import 就报错，也不要让错表流到 Step 4 的风险分级里。"""
    if set(specs) != set(Intent):
        missing = sorted(i.value for i in set(Intent) - set(specs))
        extra = sorted(str(i) for i in set(specs) - set(Intent))
        raise ValueError(f"INTENT_SPECS 必须覆盖 12 个意图: missing={missing} extra={extra}")

    for intent, spec in specs.items():
        unknown = sorted(set(spec) - _ALLOWED_KEYS)
        if unknown:
            # "role" 会落在这里：角色不是意图的属性，一旦有人加回来就立刻炸。
            raise ValueError(f"{intent.value} 出现非法字段: {unknown}")
        for key in ("slots", "toolset"):
            value = spec[key]
            if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
                raise ValueError(f"{intent.value}.{key} 必须是 list[str]")
        if not isinstance(spec["artifact"], str) or not spec["artifact"]:
            raise ValueError(f"{intent.value}.artifact 必须是非空字符串")
        if spec["risk_ceiling"] not in RISK_LEVELS:
            raise ValueError(f"{intent.value}.risk_ceiling 非法: {spec['risk_ceiling']}")
        if "owner" in spec and (not isinstance(spec["owner"], str) or not spec["owner"]):
            raise ValueError(f"{intent.value}.owner 必须是非空字符串")


_validate_specs(INTENT_SPECS)
