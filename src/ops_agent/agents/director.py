"""运营主管 Agent：一次调用完成体检、选品、诊断、假设、价格带与派活。

和旧的 ``supervisor.py`` 的根本区别：这里没有任何阈值、权重或问题分类表。
模型看到的是全店数据、类目中位数、评价原文和自己的历史记忆，由它决定
"这周改几个、改哪几个、为什么、改成什么目标、允许多少版"。

代码只做四件事，且都不参与业务判断：

1. **取数聚合**：算中位数、库存天数、成本底线，把 50 个商品压成模型能读的摘要。
2. **契约校验**：任务单字段是否齐全、价格是否低于成本、版数是否越界。不合格先
   让模型自己修一次；修不好就丢掉该商品，绝不替它编一个方案。
3. **配额裁剪**：模型说"改 40 个"时按店铺规模与人工审批容量裁到上限，并记录
   ``guarded``，让运营台能解释"模型要 12 个，我们只允许 5 个"。
4. **记忆落盘**：只有主管能写画像与决策日志，写前先冻结当时的记忆快照。

调用失败时整批中止（FR-510）：不产出规则兜底方案，避免"看起来成功了，其实是
模板"。要规则基线请显式使用 ``--planner rule``。
"""

from __future__ import annotations

import json
import math
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..contracts import (
    MAX_WINDOW_DAYS,
    ContractError,
    normalize_task_order,
    validate_task_order,
)
from ..llm import LLMClient, LLMError, TraceRecorder, parse_json_object
from ..memory import MemoryStore
from ..quota import QuotaPolicy
from ..site_platform import SiteProduct
from ..store_metrics import (
    MARGIN_FLOOR,
    entry_variant,
    cost_floor_cents,
    compute_benchmarks,
    stock_days,
)

MAX_REVIEWS_PER_PRODUCT = 2
MAX_EVIDENCE = 6
COMPACT_CATALOGUE_THRESHOLD = 120
# Five task orders with Chinese prose plus a reasoning trace do not fit in the
# default 4096; asking for 8192 up front avoids one wasted truncated call.
DIRECTOR_MAX_TOKENS = 8192
# A band this wide is not a strategy, it is a typo. Clamp to something a buyer
# would recognise as "same product, different positioning".
BAND_FLOOR_RATIO = 0.5
BAND_CEIL_RATIO = 3.0

SYSTEM_PROMPT = """You are the operations director of a B2B export store.
Once a week you decide which products to change, why, and what the change must achieve.

You are given the whole active catalogue with its 30-day metrics, per-category
medians, recent reviews, the store's memory of previous weeks, and the number of
products the human reviewer can absorb. Nothing else. Do not assume facts that
are not in the payload.

Decide in this order:
1. Store health: one short paragraph, a 0-100 score, and the 1-3 categories where
   the problem concentrates. If the data cannot support a conclusion, say so
   instead of inventing one.
2. How many products to touch this week, between 1 and may_select_high.
   may_select_low is the usual size for a store this size — a reference, not a
   quota to fill. Choose from how many products are actually broken, not from
   the number you are allowed. A week with one genuinely broken product is a
   one-product week: pick one and explain in underfill_reason why the week is
   smaller than usual. Padding the list with a weak pick is worse than a small
   week, because the reviewer spends the same hour on a worse change.
3. Which products, in priority order. Every pick needs at least two concrete
   pieces of evidence taken from the payload: a metric against its category
   median, a review quote, or stock cover. "Performance is poor" is not evidence.
4. For each pick: one primary problem (name it yourself, no fixed taxonomy), a
   falsifiable hypothesis, the metric that proves it, a minimum lift, a judgement
   window, a price band, and how many copy variants the workshop should produce.

Hard rules:
- All money fields are integer cents. price_low_cents <= price_high_cents.
- price_low_cents must be >= cost_floor_cents for that product.
- price_low_cents must be >= price_band_low_ratio * current price and
  price_high_cents <= price_band_high_ratio * current price. This is a price
  test, not a re-pricing.
- variant_min >= variant_low, variant_max <= variant_high, variant_min <= variant_max.
- min_lift is a fraction: 0.12 means +12%. It must be > 0 and <= 1.
- window_days is an integer between 7 and window_high.
- Only use product_id values that appear in catalogue. Never invent a product.
- Never invent specifications, certifications, sales figures or review text.
- Never add a product to reach a number. Every sku must stand on its own evidence.
- selected_count must equal the number of items in skus.
- If selected_count < may_select_low, underfill_reason is required and must say
  what the store looks like this week; otherwise leave it empty.

Output JSON only, no markdown fence:
{
  "store_health": {
    "score": 0-100,
    "summary": "Chinese, 2-3 sentences",
    "weak_categories": ["category_slug"],
    "top_problem": "Chinese, one line"
  },
  "selected_count": 4,
  "selection_reason": "Chinese, why this many and why these",
  "underfill_reason": "Chinese, only when selected_count < may_select_low",
  "skus": [
    {
      "product_id": "from catalogue",
      "problem": "short_snake_case",
      "problem_label": "Chinese label",
      "problem_detail": "Chinese, what exactly is wrong",
      "priority": 0.0-1.0,
      "evidence": ["Chinese, metric vs median", "Chinese, review or stock fact"],
      "hypothesis": "Chinese, falsifiable: if we change X then Y moves by Z",
      "action": "Chinese, the concrete change",
      "success_metric": "Chinese, observable metric name",
      "min_lift": 0.12,
      "window_days": 14,
      "price_low_cents": 1899,
      "price_high_cents": 2299,
      "price_reason": "Chinese, why this band",
      "variant_min": 2,
      "variant_max": 3,
      "variant_reason": "Chinese, why this many versions",
      "must_include": ["Chinese, element the copy must contain"],
      "forbidden": ["Chinese, claim the copy must avoid"]
    }
  ]
}"""


def new_run_id() -> str:
    """Sortable by time, unique within a second."""
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


def _int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def price_points(low: int, high: int, count: int) -> list[int]:
    """Evenly spaced price points across the band, one per variant.

    Zero-width bands repeat the same price on purpose: two variants may differ by
    angle rather than by price, and the workshop still needs one index each.
    """
    count = max(2, int(count))
    low = max(1, int(low))
    high = max(low, int(high))
    if high == low:
        return [low] * count
    step = (high - low) / (count - 1)
    return [int(round(low + step * index)) for index in range(count)]


def _review_digest(items: list[dict] | None) -> list[dict]:
    digest: list[dict] = []
    for item in items or []:
        text = _text(item.get("text"))
        if not text:
            continue
        digest.append(
            {
                "rating": item.get("rating"),
                "text": text[:180],
                "country": _text(item.get("country") or item.get("locale")),
            }
        )
        if len(digest) >= MAX_REVIEWS_PER_PRODUCT:
            break
    return digest


def _json_number(value: float | None) -> float | None:
    """JSON has no infinity; stock cover with zero sales must not crash dumps."""
    if value is None:
        return None
    if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
        return None
    return round(float(value), 2)


def build_catalogue(
    products: list[SiteProduct],
    *,
    days: int,
    reviews: dict[str, list[dict]] | None = None,
    compact: bool = False,
) -> list[dict]:
    """Turn dataclasses into the smallest payload that still supports a decision."""
    reviews = reviews or {}
    catalogue: list[dict] = []
    for product in products:
        metrics = product.metrics
        entry = entry_variant(product)
        record: dict[str, Any] = {
            "product_id": product.product_id,
            "title": product.title_en,
            "category": product.category_slug or "",
            "price_cents": entry.price_cents if entry else 0,
            "cost_floor_cents": cost_floor_cents(entry),
            "stock_units": product.stock_total,
            "lead_time_days": product.lead_time_days,
            "metrics": None,
        }
        if metrics:
            record["metrics"] = {
                "pv": metrics.pv,
                "inquiries": metrics.inquiries,
                "orders": metrics.orders,
                "units": metrics.units,
                "inquiry_rate_pct": round(metrics.inquiry_rate * 100, 2),
                "order_rate_pct": round(metrics.order_rate * 100, 2),
                "reviews": metrics.reviews,
                "avg_rating": metrics.avg_rating,
                "negative_reviews": metrics.negative_reviews,
                "stock_days": _json_number(stock_days(product, days)),
            }
            if not compact:
                record["metrics"]["revenue_cents"] = metrics.revenue_cents
                record["metrics"]["uv"] = metrics.uv
                record["moq"] = product.moq
                record["incoterm"] = product.incoterm
                record["margin_pct"] = (
                    round(entry.margin * 100, 1) if entry and entry.margin is not None else None
                )
        sample = _review_digest(reviews.get(product.product_id))
        if sample and not compact:
            record["reviews"] = sample
        catalogue.append(record)
    return catalogue


def build_digest(
    store,
    products: list[SiteProduct],
    *,
    days: int,
    reviews: dict[str, list[dict]] | None,
    memory_context: dict,
    policy: QuotaPolicy,
    limit_override: int = 0,
    window_max_days: int = MAX_WINDOW_DAYS,
    band_floor_ratio: float = BAND_FLOOR_RATIO,
    band_ceil_ratio: float = BAND_CEIL_RATIO,
) -> dict:
    """Everything the director is allowed to know, and nothing more."""
    active = [product for product in products if product.status == "active"]
    compact = len(active) > COMPACT_CATALOGUE_THRESHOLD
    low, high = policy.sku_bounds(len(active), limit_override)
    variant_low, variant_high = policy.variant_bounds(max(1, high))
    by_category, global_benchmark = compute_benchmarks(
        [product for product in active if product.metrics]
    )

    def bench(item) -> dict:
        return {
            "pv": round(item.pv, 1),
            "inquiry_rate_pct": round(item.inquiry_rate * 100, 2),
            "order_rate_pct": round(item.order_rate * 100, 2),
            "margin_pct": round(item.margin * 100, 1) if item.margin else None,
        }

    return {
        "store": {
            "slug": store.slug,
            "name": store.name,
            "country": store.country,
            "currency": store.currency,
        },
        "window_days": days,
        "catalogue_size": len(active),
        "review_capacity_versions": policy.review_cap,
        "may_select_low": low,
        "may_select_high": high,
        "may_select_note": "low is the usual week for a store this size, not a quota to fill",
        "variant_low": variant_low,
        "variant_high": variant_high,
        "window_high": window_max_days,
        "price_band_low_ratio": band_floor_ratio,
        "price_band_high_ratio": band_ceil_ratio,
        "compact_catalogue": compact,
        "benchmarks": {
            "all": bench(global_benchmark),
            "by_category": {name: bench(value) for name, value in by_category.items()},
        },
        "catalogue": build_catalogue(active, days=days, reviews=reviews, compact=compact),
        "memory": memory_context,
        "instruction": (
            "Decide the week. may_select_low is the usual size, may_select_high the "
            "ceiling; a smaller week needs underfill_reason, never a filler SKU. "
            "Cite at least two evidence items per SKU from this payload."
        ),    }


@dataclass
class DirectorResult:
    run_id: str
    store_slug: str
    generated_at: str
    scanned: int
    selected: list[dict]
    store_health: dict
    quota: dict
    notes: list[str]
    violations: list[str] = field(default_factory=list)
    requested_count: int = 0
    model: str = ""
    planner: str = "llm"
    repairs: int = 0
    repair_issues: list[str] = field(default_factory=list)
    underfill_reason: str = ""

    def as_payload(self) -> dict:
        return {
            "store": self.store_slug,
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "scanned": self.scanned,
            "matched": len(self.selected),
            "selected": self.selected,
            "store_health": self.store_health,
            "quota": self.quota,
            "notes": self.notes,
            "violations": self.violations,
            "repair_issues": self.repair_issues,
            "underfill_reason": self.underfill_reason,
            "planner": self.planner,
            "model": self.model,
        }


class DirectorAgent:
    """One model call, plus at most one self-repair call."""

    role = "director"

    def __init__(self, client: LLMClient, trace: TraceRecorder | None = None) -> None:
        self.client = client
        self.trace = trace

    def run(self, digest: dict) -> tuple[dict, str]:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(digest, ensure_ascii=False)},
        ]
        try:
            reply = self.client.chat(messages, temperature=0.3, max_tokens=DIRECTOR_MAX_TOKENS)
        except LLMError as exc:
            self._record(messages, error=str(exc))
            raise
        try:
            data = parse_json_object(reply.text)
        except LLMError as exc:
            # Record the truncated text: without it the next debugging session
            # only sees "not valid JSON" and has to guess why.
            self._record(messages, reply=reply, error=str(exc))
            raise
        self._record(messages, reply=reply)
        return data, reply.text

    def repair(self, digest: dict, previous: str, issues: list[str]) -> tuple[dict, str]:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(digest, ensure_ascii=False)},
            {"role": "assistant", "content": (previous or "")[:16000]},
            {
                "role": "user",
                "content": (
                    "Your previous reply failed validation:\n- "
                    + "\n- ".join(issues[:12])
                    + "\nFix only these problems. Keep every SKU that was already valid, "
                    "keep selected_count equal to the number of skus, and reply with the "
                    "complete JSON object again."
                ),
            },
        ]
        try:
            reply = self.client.chat(messages, temperature=0.2, max_tokens=DIRECTOR_MAX_TOKENS)
        except LLMError as exc:
            self._record(messages, error=str(exc))
            raise
        try:
            data = parse_json_object(reply.text)
        except LLMError as exc:
            self._record(messages, reply=reply, error=str(exc))
            raise
        self._record(messages, reply=reply)
        return data, reply.text

    def _record(self, messages: list[dict], *, reply=None, error: str = "") -> None:
        if self.trace is None:
            return
        self.trace.record(
            agent=self.role,
            ok=error == "",
            model=reply.model if reply is not None else self.client.settings.model,
            messages=messages,
            response=reply.text if reply is not None else "",
            error=error,
            prompt_tokens=reply.prompt_tokens if reply is not None else 0,
            completion_tokens=reply.completion_tokens if reply is not None else 0,
            latency_ms=reply.latency_ms if reply is not None else 0,
            meta={"agent": self.role},
        )


def normalize_store_health(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    score = _int(raw.get("score"), 50)
    return {
        "score": max(0, min(100, score)),
        "summary": _text(raw.get("summary"))[:800],
        "weak_categories": _str_list(raw.get("weak_categories"))[:5],
        "top_problem": _text(raw.get("top_problem"))[:200],
    }


def _clamp_band(
    raw_low: int,
    raw_high: int,
    *,
    current_cents: int,
    floor_cents: int,
    band_floor_ratio: float = BAND_FLOOR_RATIO,
    band_ceil_ratio: float = BAND_CEIL_RATIO,
) -> tuple[int, int, list[str]]:
    """Keep the model's intent but refuse bands that are typos or margin suicide."""
    issues: list[str] = []
    low = max(1, int(raw_low))
    high = max(1, int(raw_high))
    if low > high:
        low, high = high, low
        issues.append("price band was inverted, swapped")
    if floor_cents and low < floor_cents:
        issues.append(f"price low raised to cost floor {floor_cents}")
        low = floor_cents
    if current_cents:
        hard_low = int(round(current_cents * band_floor_ratio))
        hard_high = int(round(current_cents * band_ceil_ratio))
        if low < hard_low:
            issues.append(
                f"price low raised to {hard_low} (floor is {band_floor_ratio:.0%} of current)"
            )
            low = hard_low
        high = max(low, min(high, max(low, hard_high)))
    return low, max(low, high), issues


def normalize_sku(
    raw: dict,
    product: SiteProduct,
    *,
    run_id: str,
    variant_low: int,
    variant_high: int,
    margin_floor: float,
    window_max_days: int = MAX_WINDOW_DAYS,
    band_floor_ratio: float = BAND_FLOOR_RATIO,
    band_ceil_ratio: float = BAND_CEIL_RATIO,
) -> tuple[dict | None, list[str]]:
    """One SKU from model prose to an executable TaskOrder, or a list of reasons."""
    issues: list[str] = []
    prefix = f"sku[{product.product_id[:8]}]"
    entry = entry_variant(product)
    current = entry.price_cents if entry else 0

    low, high, band_issues = _clamp_band(
        _int(raw.get("price_low_cents"), current),
        _int(raw.get("price_high_cents"), current),
        current_cents=current,
        floor_cents=cost_floor_cents(entry, margin_floor),
        band_floor_ratio=band_floor_ratio,
        band_ceil_ratio=band_ceil_ratio,
    )
    issues.extend(f"{prefix}: {item}" for item in band_issues)

    variant_max = _int(raw.get("variant_max"), 3)
    variant_max = max(variant_low, min(variant_high, variant_max))
    variant_min = _int(raw.get("variant_min"), variant_low)
    variant_min = max(variant_low, min(variant_max, variant_min))

    points = price_points(low, high, variant_max)
    if len(set(points)) != len(points):
        # The contract rejects duplicate price points. A zero-width band cannot
        # produce distinct ones, so widen by one cent per slot rather than
        # inventing a price the model never asked for.
        high = low + (variant_max - 1)
        points = price_points(low, high, variant_max)
        issues.append(f"{prefix}: band too narrow for {variant_max} distinct price points, widened by cents")
    points = [max(low, min(high, value)) for value in points]
    if len(points) < 2:
        issues.append(f"{prefix}: price band yields fewer than 2 price points")
        return None, issues

    priority = _float(raw.get("priority"), 0.5)
    priority = max(0.0, min(1.0, priority))

    doc = normalize_task_order(
        {
            "task_id": f"{run_id}:{product.product_id}",
            "run_id": run_id,
            "product_id": product.product_id,
            "title": product.title_en,
            "category": product.category_slug or "",
            "problem": _text(raw.get("problem")),
            "problem_detail": _text(raw.get("problem_detail")),
            "evidence": _str_list(raw.get("evidence"))[:MAX_EVIDENCE],
            "hypothesis": _text(raw.get("hypothesis")),
            "success_metric": _text(raw.get("success_metric")),
            "min_lift": _float(raw.get("min_lift"), 0.1),
            "window_days": max(7, min(window_max_days, _int(raw.get("window_days"), 14))),
            "price_low_cents": low,
            "price_high_cents": high,
            "price_points": points,
            "variant_min": variant_min,
            "variant_max": variant_max,
            "must_include": _str_list(raw.get("must_include"))[:5],
            "forbidden": _str_list(raw.get("forbidden"))[:5],
        }
    )
    errors = validate_task_order(doc, max_window_days=window_max_days)
    if errors:
        return None, [f"{prefix}: {item}" for item in errors]

    score = round(priority, 3)
    doc.update(
        {
            # Two spellings on purpose: the contract uses low/high, the workshop
            # and the console have always read the tuple. Keeping both avoids a
            # silent fallback to "current price" in the price planner.
            "price_band_cents": [doc["price_low_cents"], doc["price_high_cents"]],
            "problem_label": _text(raw.get("problem_label")) or doc["problem"],
            "score": score,
            "severity": score,
            "action": _text(raw.get("action")) or doc["problem_detail"],
            "price_reason": _text(raw.get("price_reason"))[:400],
            "variant_reason": _text(raw.get("variant_reason"))[:400],
            "margin_floor": margin_floor,
            "budget_tokens": 60000,
            "planner": "llm",
            "strategy": {
                "planner": "llm",
                "source": "director",
                "problem": doc["problem"],
                "problem_label": _text(raw.get("problem_label")) or doc["problem"],
                "hypothesis": doc["hypothesis"],
                "action": _text(raw.get("action")) or doc["problem_detail"],
                "success_metric": doc["success_metric"],
                "reason": _text(raw.get("problem_detail"))[:600],
                "confidence": score,
                "evidence": doc["evidence"],
            },
        }
    )
    return doc, issues


def normalize_selection(
    data: dict,
    *,
    products_by_id: dict[str, SiteProduct],
    run_id: str,
    policy: QuotaPolicy,
    active_count: int,
    margin_floor: float,
    limit_override: int = 0,
    window_max_days: int = MAX_WINDOW_DAYS,
    band_floor_ratio: float = BAND_FLOOR_RATIO,
    band_ceil_ratio: float = BAND_CEIL_RATIO,
) -> tuple[list[dict], list[str], int, dict]:
    """Apply the contract and the quota, keeping the model's own ordering."""
    issues: list[str] = []
    raw_skus = data.get("skus")
    if not isinstance(raw_skus, list):
        return [], ["skus is not a list"], 0, {}
    requested = _int(data.get("selected_count"), len(raw_skus))
    decision = policy.clamp_skus(requested, active_count, limit_override)
    if decision.guarded:
        issues.append(
            f"selected_count {decision.requested} adjusted to {decision.value} ({decision.reason})"
        )

    accepted: list[tuple[dict, list[str]]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_skus):
        if len(accepted) >= decision.value:
            break
        if not isinstance(raw, dict):
            issues.append(f"skus[{index}] is not an object, dropped")
            continue
        product_id = _text(raw.get("product_id"))
        product = products_by_id.get(product_id)
        if product is None:
            issues.append(f"skus[{index}]: unknown product_id {product_id!r}, dropped")
            continue
        if product_id in seen:
            issues.append(f"skus[{index}]: duplicate product_id {product_id[:8]}, dropped")
            continue
        seen.add(product_id)
        # Variant capacity depends on how many SKUs may enter review this week,
        # not on how many the model happened to list, so the quota decides.
        variant_low, variant_high = policy.variant_bounds(max(decision.value, 1))
        doc, sku_issues = normalize_sku(
            raw,
            product,
            run_id=run_id,
            variant_low=variant_low,
            variant_high=variant_high,
            margin_floor=margin_floor,
            window_max_days=window_max_days,
            band_floor_ratio=band_floor_ratio,
            band_ceil_ratio=band_ceil_ratio,
        )
        issues.extend(sku_issues)
        if doc is not None:
            accepted.append((doc, sku_issues))

    selected = [doc for doc, _ in accepted]
    if len(selected) > decision.value:
        selected = selected[: decision.value]

    # A smaller week is allowed, but it has to be explained. Without this the
    # only signal a reviewer gets is a number that looks low, and the cheapest
    # way for the model to avoid that is to add a SKU nobody needs. The issue is
    # blocking, so it can trigger the single repair pass — not to raise the
    # count, but to make the model say what it saw.
    underfill_reason = _text(data.get("underfill_reason"))
    underfill = bool(selected) and len(selected) < decision.low
    if underfill and not underfill_reason:
        issues.append(
            f"only {len(selected)} product(s) selected, below the usual {decision.low}; "
            "add underfill_reason explaining why this week is smaller "
            "(do not add a weak SKU to reach the number)"
        )

    # FR-503: no single problem type may dominate the week. The code detects the
    # imbalance but does not drop a genuinely broken product to fix it; it asks
    # the model to rebalance, and records a warning if the model insists.
    if selected:
        cap = max(2, math.ceil(len(selected) * 0.5))
        counts = Counter(item["problem"] for item in selected)
        over = {name: count for name, count in counts.items() if count > cap}
        if over:
            issues.append(
                f"problem concentration {over} exceeds {cap} of {len(selected)}; "
                "diversify the week or justify why one problem dominates"
            )

    quota_doc = {
        **decision.as_dict(),
        "underfill": underfill,
        "underfill_reason": underfill_reason,
    }
    return selected, issues, requested, quota_doc


def _choose_better(
    first: tuple[list[dict], list[str], int, dict],
    second: tuple[list[dict], list[str], int, dict],
) -> tuple[list[dict], list[str], int, dict]:
    """Prefer the reply that yields more usable SKUs, then the cleaner one."""
    if len(second[0]) != len(first[0]):
        return second if len(second[0]) > len(first[0]) else first
    return second if len(second[1]) <= len(first[1]) else first


def _write_memory(
    memory: MemoryStore,
    *,
    run_id: str,
    store_slug: str,
    scanned: int,
    result: DirectorResult,
    digest: dict,
) -> None:
    """Profile first, decision second: a crash between them leaves a sane state."""
    profile = memory.read_profile() or {}
    profile.update(
        {
            "store_slug": store_slug,
            "active_products": scanned,
            "catalogue_categories": sorted(
                {item.get("category") for item in digest.get("catalogue", []) if item.get("category")}
            ),
            "median_order_rate_pct": digest.get("benchmarks", {}).get("all", {}).get("order_rate_pct"),
            "median_inquiry_rate_pct": digest.get("benchmarks", {}).get("all", {}).get("inquiry_rate_pct"),
            "last_run_id": run_id,
            "last_selected_count": len(result.selected),
            "store_health": result.store_health,
        }
    )
    memory.write_profile(profile, run_id=run_id)
    memory.append_decision(
        {
            "decision_id": run_id,
            "run_id": run_id,
            "store_slug": store_slug,
            "scanned": scanned,
            "selected_count": len(result.selected),
            "requested_count": result.requested_count,
            "quota": result.quota,
            "store_health": result.store_health,
            "reason": result.notes[0] if result.notes else "",
            "product_ids": [item["product_id"] for item in result.selected],
            "problems": {item["product_id"]: item["problem"] for item in result.selected},
            "violations": result.violations[:20],
            "model": result.model,
        }
    )


def run_director(
    *,
    store,
    products: list[SiteProduct],
    client: LLMClient,
    reviews: dict[str, list[dict]] | None = None,
    trace: TraceRecorder | None = None,
    memory: MemoryStore | None = None,
    policy: QuotaPolicy | None = None,
    days: int = 30,
    run_id: str = "",
    limit_override: int = 0,
    margin_floor: float = MARGIN_FLOOR,
    window_max_days: int = MAX_WINDOW_DAYS,
    band_floor_ratio: float = BAND_FLOOR_RATIO,
    band_ceil_ratio: float = BAND_CEIL_RATIO,
) -> DirectorResult:
    """Run the weekly decision. Raises ContractError when no usable SKU survives."""
    if not client.enabled:
        raise LLMError(f"model not configured: {client.settings.describe()}")

    policy = policy or QuotaPolicy.from_env()
    run_id = run_id or new_run_id()
    active = [product for product in products if product.status == "active"]
    products_by_id = {product.product_id: product for product in active}

    memory_context = memory.context(run_id=run_id) if memory is not None else {}
    if memory is not None:
        memory.snapshot(run_id)

    digest = build_digest(
        store,
        products,
        days=days,
        reviews=reviews,
        memory_context=memory_context,
        policy=policy,
        limit_override=limit_override,
        window_max_days=window_max_days,
        band_floor_ratio=band_floor_ratio,
        band_ceil_ratio=band_ceil_ratio,
    )

    agent = DirectorAgent(client, trace)
    raw, text = agent.run(digest)
    selected, issues, requested, quota_doc = normalize_selection(
        raw,
        products_by_id=products_by_id,
        run_id=run_id,
        policy=policy,
        active_count=len(active),
        margin_floor=margin_floor,
        limit_override=limit_override,
        window_max_days=window_max_days,
        band_floor_ratio=band_floor_ratio,
        band_ceil_ratio=band_ceil_ratio,
    )
    repairs = 0
    first_issues = list(issues)

    # One repair attempt, triggered only by problems the model can actually fix.
    blocking = [item for item in issues if "adjusted to" not in item]
    # "only 1 product selected…" asks for a sentence, not for another product.
    # Keeping that apart lets the repair fix the sentence without the count
    # silently growing, which is how padding would re-enter through the back door.
    underfill_only = bool(blocking) and all(item.startswith("only ") for item in blocking)
    if issues and blocking:
        try:
            raw_second, text_second = agent.repair(digest, text, issues)
        except LLMError:
            raw_second = None
        if raw_second is not None:
            second = normalize_selection(
                raw_second,
                products_by_id=products_by_id,
                run_id=run_id,
                policy=policy,
                active_count=len(active),
                margin_floor=margin_floor,
                limit_override=limit_override,
                window_max_days=window_max_days,
                band_floor_ratio=band_floor_ratio,
                band_ceil_ratio=band_ceil_ratio,
            )
            if underfill_only and len(second[0]) > len(selected):
                # The week was smaller than usual and the model answered by
                # adding products. Keep the smaller week; take only the reason.
                answer = _text(raw_second.get("underfill_reason"))
                if answer:
                    quota_doc = {**quota_doc, "underfill_reason": answer}
                    issues = [item for item in issues if not item.startswith("only ")]
                repairs = 1
            else:
                selected, issues, requested, quota_doc = _choose_better(
                    (selected, issues, requested, quota_doc), second
                )
                repairs = 1

    if not selected:
        raise ContractError(
            "director",
            issues or ["the model produced no usable SKU; refusing to fabricate one"],
        )

    health = normalize_store_health(raw.get("store_health"))
    result = DirectorResult(
        run_id=run_id,
        store_slug=store.slug,
        generated_at=datetime.now().isoformat(timespec="seconds"),
        scanned=len(active),
        selected=selected,
        store_health=health,
        quota=quota_doc,
        notes=[],
        violations=issues,
        requested_count=requested,
        model=client.settings.model,
        repairs=repairs,
        repair_issues=first_issues if repairs else [],
        underfill_reason=_text(quota_doc.get("underfill_reason")),
    )
    result.notes.append(
        f"扫描 {len(active)} 个在售商品；模型决定改 {len(selected)} 个"
        f"（请求 {requested}，常态 {quota_doc.get('low', '?')}..上限 {quota_doc.get('high', '?')}）。"
    )
    if quota_doc.get("guarded"):
        result.notes.append(f"数量被代码裁剪：{quota_doc.get('reason', '')}。")
    elif quota_doc.get("underfill"):
        reason = result.underfill_reason or "未说明原因"
        result.notes.append(
            f"本周减量：只改 {len(selected)} 个，低于常态 {quota_doc.get('low')}。{reason}"
        )
    if repairs:
        reason = "；".join(first_issues[:3]) or "字段不完整"
        result.notes.append(f"首版任务单未通过校验，已按错误清单自修一次：{reason}")
    if issues:
        result.notes.append(f"仍有 {len(issues)} 条校验提示，见 violations。")

    if memory is not None:
        _write_memory(
            memory,
            run_id=run_id,
            store_slug=store.slug,
            scanned=len(active),
            result=result,
            digest=digest,
        )

    return result
