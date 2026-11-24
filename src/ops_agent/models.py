"""Domain models shared by the supervisor, producer and reviewer agents.

The models are intentionally dependency-free (standard library only) so the
project can run in a clean Python environment and tests stay fast.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import date
from enum import Enum
from typing import Any


class Market(str, Enum):
    US = "US"
    DE = "DE"
    UK = "UK"


class Problem(str, Enum):
    LOW_IMPRESSION = "low_impression"
    LOW_CTR = "low_ctr"
    LOW_CVR = "low_cvr"
    HIGH_RETURN = "high_return"
    STOCK_RISK = "stock_risk"


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"


class ExperimentStatus(str, Enum):
    RUNNING = "running"
    KEPT = "kept"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class SKU:
    """A product listing and the levers the agents are allowed to change."""

    sku_id: str
    title: str
    category: str
    market: Market
    cost: float
    price: float
    stock_days: int
    image_score: float
    title_score: float
    detail_score: float
    review_score: float
    shipping_days: int
    created_at: date

    def __post_init__(self) -> None:
        if not self.sku_id.startswith("SKU-"):
            raise ValueError(f"sku_id must start with SKU-: {self.sku_id}")
        if self.price <= 0 or self.cost <= 0:
            raise ValueError("price and cost must be positive")
        if self.price <= self.cost:
            raise ValueError("price must be greater than cost")
        for name in ("image_score", "title_score", "detail_score", "review_score"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1], got {value}")
        if self.stock_days < 0:
            raise ValueError("stock_days must be non-negative")
        if not 0 <= self.shipping_days <= 30:
            raise ValueError("shipping_days must be in [0, 30]")

    @property
    def margin(self) -> float:
        return (self.price - self.cost) / self.price

    def replace(self, **changes: Any) -> "SKU":
        return replace(self, **changes)


@dataclass(frozen=True)
class Performance:
    """One day of traffic and conversion for a SKU."""

    sku_id: str
    day: date
    impressions: int
    clicks: int
    orders: int
    returns: int

    def __post_init__(self) -> None:
        for name in ("impressions", "clicks", "orders", "returns"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.clicks > self.impressions:
            raise ValueError("clicks cannot exceed impressions")
        if self.orders > self.clicks:
            raise ValueError("orders cannot exceed clicks")
        if self.returns > self.orders:
            raise ValueError("returns cannot exceed orders")

    @property
    def ctr(self) -> float:
        return self.clicks / self.impressions if self.impressions else 0.0

    @property
    def cvr(self) -> float:
        return self.orders / self.clicks if self.clicks else 0.0

    @property
    def return_rate(self) -> float:
        return self.returns / self.orders if self.orders else 0.0


@dataclass
class Variant:
    """One candidate version produced by the content workshop."""

    variant_id: str
    copy: str
    image_brief: str
    price: float
    expected_lift: float

    def __post_init__(self) -> None:
        if self.expected_lift < 0:
            raise ValueError("expected_lift must be non-negative")
        if self.price <= 0:
            raise ValueError("variant price must be positive")


@dataclass
class TaskOrder:
    """Supervisor -> content workshop handoff.

    .. deprecated::
        意图层（``intake/mapping.py``）已改为直接产出 ``contracts.normalize_task_order``
        的 dict（键名 ``task_id`` / ``product_id`` / ``price_low_cents`` …），不再实例化
        本类。跨 Agent 的交接形状以 ``contracts.py`` 为准，这里仅作为旧的领域对象保留，
        供既有构造代码与测试继续可用；新代码不要再往里加字段。
    """

    order_id: str
    sku_id: str
    market: Market
    problem: Problem
    hypothesis: str
    price_band: tuple[float, float]
    margin_floor: float
    success_metric: str
    budget_tokens: int
    # 新增字段一律带默认值、放末尾：既有调用方（含测试）都是位置传参。
    # "scheduled" = 定时巡检产出，"intent" = 一句话委托产出，便于在预览里分开看。
    source: str = "scheduled"
    slots: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        low, high = self.price_band
        if low <= 0 or high < low:
            raise ValueError("invalid price_band")
        if not 0 <= self.margin_floor < 1:
            raise ValueError("margin_floor must be in [0, 1)")
        if self.budget_tokens <= 0:
            raise ValueError("budget_tokens must be positive")


@dataclass
class Proposal:
    """Content workshop -> reviewer / supervisor handoff."""

    order_id: str
    variants: list[Variant]
    expected_lift: float
    rationale: str

    def __post_init__(self) -> None:
        if not 2 <= len(self.variants) <= 3:
            raise ValueError("proposal must contain 2-3 variants")


@dataclass
class ReviewResult:
    """Reviewer -> supervisor handoff."""

    variant_id: str
    verdict: Verdict
    violations: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.verdict == Verdict.FAIL and not self.violations:
            raise ValueError("a failed review must list at least one violation")


@dataclass
class Experiment:
    """A single-variable change with a control group."""

    experiment_id: str
    sku_id: str
    variant_id: str
    control_sku_ids: list[str]
    start_day: date
    end_day: date | None = None
    status: ExperimentStatus = ExperimentStatus.RUNNING
    result: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.control_sku_ids:
            raise ValueError("experiment needs at least one control SKU")


@dataclass(frozen=True)
class PlaybookEntry:
    """A reusable conclusion consolidated from repeated experiments."""

    entry_id: str
    category: str
    market: Market
    pattern: str
    evidence_count: int
    confidence: float

    def __post_init__(self) -> None:
        if self.evidence_count <= 0:
            raise ValueError("evidence_count must be positive")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be in [0, 1]")


def jsonable(value: Any) -> Any:
    """Convert dataclasses / enums / dates into JSON-safe structures."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {key: jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value
