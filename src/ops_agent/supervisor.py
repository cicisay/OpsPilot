"""LEGACY rule engine: thresholds, weights and the 7-problem playbook.

As of the three-agent rewrite this module is **no longer on the model path**.
The weekly decision lives in ``agents/director.py``; this file survives only as
the offline regression baseline selected by ``--planner rule`` (decision Q2), so
we can diff "what rules would have chosen" against "what the director chose".

Do not add behaviour here. Aggregation helpers moved to ``store_metrics.py`` so
the director never imports this module and no threshold can leak into a
model-driven week.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from .agents import StrategistAgent, WorkContext
from .llm import LLMClient, LLMError, TraceRecorder
from .site_platform import SiteProduct, SiteVariant
from .store_metrics import (  # re-exported for the rule baseline and its tests
    MARGIN_FLOOR,
    Benchmark,
    compute_benchmarks,
    entry_variant,
    price_band,
    stock_days,
)

PROBLEM_LABELS = {
    "low_traffic": "流量低",
    "low_inquiry": "询盘低",
    "low_order": "下单低",
    "high_negative": "差评高",
    "stock_risk": "库存风险",
    "overstock": "库存积压",
    "low_margin": "毛利低",
}

PLAYBOOK = {
    "low_traffic": ("标题 / 关键词覆盖不足", "重写标题与关键词，补齐类目属性", "PV +20%"),
    "low_inquiry": ("详情页 / 价格 / 信任信息不足", "补认证、MOQ、交期、实拍与应用场景", "询盘率 +15%"),
    "low_order": ("MOQ / 交期 / 价格阻碍成交", "给阶梯价与样品政策，缩短交期", "下单率 +10%"),
    "high_negative": ("质量或描述不符", "改描述、加实拍、复盘差评原因", "差评率 -3pt"),
    "stock_risk": ("库存快断，需求仍在", "补货或提价保毛利", "库存天数 +30%"),
    "overstock": ("库存积压，动销不足", "清仓 / 捆绑 / 降价测试", "库存天数 -20%"),
    "low_margin": ("售价低于毛利线", "提价或谈成本", "毛利率 +2pt"),
}

BUDGET_TOKENS = {
    "low_traffic": 60000,
    "low_inquiry": 120000,
    "low_order": 60000,
    "high_negative": 120000,
    "stock_risk": 30000,
    "overstock": 30000,
    "low_margin": 30000,
}


@dataclass
class Diagnosis:
    product: SiteProduct
    problem: str
    severity: float
    score: float
    evidence: tuple[str, ...]
    hypothesis: str
    action: str
    success_metric: str
    price_band_cents: tuple[int, int]
    margin_floor: float
    budget_tokens: int
    candidates: tuple[str, ...] = ()
    candidate_evidence: dict[str, tuple[str, ...]] = field(default_factory=dict)
    planner: str = "rule"
    strategy: dict = field(default_factory=dict)


@dataclass
class WeeklyPlan:
    generated_at: str
    store_slug: str
    scanned: int
    matched: int
    selected: list[Diagnosis]
    notes: list[str]


def _entry_variant(product: SiteProduct) -> SiteVariant | None:
    return entry_variant(product)


def diagnose(product: SiteProduct, benchmark: Benchmark, days: int = 30) -> Diagnosis | None:
    metrics = product.metrics
    entry = _entry_variant(product)
    if metrics is None or entry is None:
        return None

    margin = entry.margin
    stock = stock_days(product, days)
    traffic_weight = 1.0
    if benchmark.pv > 0:
        traffic_weight = min(2.0, max(0.5, math.log1p(metrics.pv) / math.log1p(benchmark.pv)))

    common = [
        f"PV {metrics.pv:,}（类目中位 {benchmark.pv:,.0f}）",
        f"询盘率 {metrics.inquiry_rate * 100:.2f}%（中位 {benchmark.inquiry_rate * 100:.2f}%）",
        f"下单率 {metrics.order_rate * 100:.2f}%（中位 {benchmark.order_rate * 100:.2f}%）",
    ]
    candidates: list[tuple[float, float, str, tuple[str, ...]]] = []

    if metrics.negative_reviews >= 2 and (
        metrics.negative_reviews / max(metrics.reviews, 1) > 0.15
        or (metrics.avg_rating or 5.0) < 4.0
    ):
        severity = max(
            metrics.negative_reviews / max(metrics.reviews, 1) / 0.15,
            (4.5 - (metrics.avg_rating or 4.5)) / 4.5,
            1.0,
        )
        evidence = (
            f"差评 {metrics.negative_reviews}/{metrics.reviews} · 均分 {metrics.avg_rating or '—'}",
            common[0],
            common[2],
        )
        candidates.append((severity * 1.3 * traffic_weight, 1.3, "high_negative", evidence))

    if benchmark.order_rate and metrics.order_rate < benchmark.order_rate * 0.70 and (
        metrics.inquiry_rate >= benchmark.inquiry_rate * 0.70
    ):
        gap = (benchmark.order_rate - metrics.order_rate) / benchmark.order_rate
        evidence = (common[2], common[1], common[0])
        candidates.append((max(0.1, gap) * 1.2 * traffic_weight, 1.2, "low_order", evidence))

    if benchmark.inquiry_rate and metrics.pv >= benchmark.pv * 0.65 and (
        metrics.inquiry_rate < benchmark.inquiry_rate * 0.70
    ):
        gap = (benchmark.inquiry_rate - metrics.inquiry_rate) / benchmark.inquiry_rate
        evidence = (common[1], common[0], common[2])
        candidates.append((max(0.1, gap) * 1.1 * traffic_weight, 1.1, "low_inquiry", evidence))

    if benchmark.pv and metrics.pv < benchmark.pv * 0.65:
        gap = (benchmark.pv - metrics.pv) / benchmark.pv
        evidence = (common[0], common[1])
        candidates.append((max(0.1, gap) * 1.0 * traffic_weight, 1.0, "low_traffic", evidence))

    if stock < 14 and metrics.order_rate >= benchmark.order_rate:
        severity = (14 - stock) / 14
        evidence = (
            f"库存 {entry.stock:,} / 日均 {metrics.units / days:.2f} 件 → {stock:.0f} 天",
            common[2],
        )
        candidates.append((severity * 1.0 * traffic_weight, 1.0, "stock_risk", evidence))

    if stock > 120 and metrics.pv >= benchmark.pv * 0.65 and metrics.pv < benchmark.pv:
        severity = min(1.0, (stock - 120) / 120)
        stock_text = "无销量" if math.isinf(stock) else f"约 {stock:.0f} 天"
        evidence = (
            f"库存 {entry.stock:,} → {stock_text}；PV 低于中位",
            common[0],
        )
        candidates.append((severity * 0.9 * traffic_weight, 0.9, "overstock", evidence))

    if margin is not None and margin < 0.20:
        severity = (0.20 - margin) / 0.20
        evidence = (f"毛利率 {margin * 100:.1f}%（底线 {MARGIN_FLOOR * 100:.0f}%）", common[2])
        candidates.append((severity * 0.8 * traffic_weight, 0.8, "low_margin", evidence))

    if not candidates:
        return None

    score, _, problem, evidence = max(candidates, key=lambda item: (item[0], item[1]))
    hypothesis, action, success_metric = PLAYBOOK[problem]
    low, high = price_band(entry, problem)
    return Diagnosis(
        product=product,
        problem=problem,
        severity=round(min(1.0, score / max(traffic_weight, 0.5)), 4),
        score=round(score, 4),
        evidence=evidence,
        hypothesis=hypothesis,
        action=action,
        success_metric=success_metric,
        price_band_cents=(low, high),
        margin_floor=MARGIN_FLOOR,
        budget_tokens=BUDGET_TOKENS[problem],
        candidates=tuple(item[2] for item in candidates),
        candidate_evidence={item[2]: item[3] for item in candidates},
    )


def build_plan(products: list[SiteProduct], store_slug: str, limit: int = 5, days: int = 30) -> WeeklyPlan:
    active = [product for product in products if product.status == "active" and product.metrics]
    by_category, global_benchmark = compute_benchmarks(active)

    diagnoses: list[Diagnosis] = []
    for product in active:
        benchmark = by_category.get(product.category_slug or "_", global_benchmark)
        diagnosis = diagnose(product, benchmark, days)
        if diagnosis is not None:
            diagnoses.append(diagnosis)
    diagnoses.sort(key=lambda item: item.score, reverse=True)

    selected: list[Diagnosis] = []
    counts: Counter[str] = Counter()
    for diagnosis in diagnoses:
        if counts[diagnosis.problem] >= 2:
            continue
        selected.append(diagnosis)
        counts[diagnosis.problem] += 1
        if len(selected) >= limit:
            break

    if len(selected) < limit:
        chosen = {id(item) for item in selected}
        for diagnosis in diagnoses:
            if id(diagnosis) in chosen:
                continue
            selected.append(diagnosis)
            if len(selected) >= limit:
                break

    notes = [
        f"扫描 {len(active)} 个在售商品，命中 {len(diagnoses)} 个异常，取前 {len(selected)} 个；同类问题最多 2 个。",
        "诊断先用规则，不接 LLM；TaskOrder 结构已为 LLM planner 预留。",
    ]
    return WeeklyPlan(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        store_slug=store_slug,
        scanned=len(active),
        matched=len(diagnoses),
        selected=selected,
        notes=notes,
    )


def _three_price_points(low: int, high: int) -> list[int]:
    if low == high:
        return [low, low, low]
    return [low, round((low + high) / 2), high]


def diagnosis_to_dict(diagnosis: Diagnosis) -> dict:
    """Stable TaskOrder contract; strategy metadata is additive."""
    return {
        "product_id": diagnosis.product.product_id,
        "title": diagnosis.product.title_en,
        "category": diagnosis.product.category_slug,
        "problem": diagnosis.problem,
        "problem_label": PROBLEM_LABELS[diagnosis.problem],
        "score": diagnosis.score,
        "severity": diagnosis.severity,
        "evidence": list(diagnosis.evidence),
        "hypothesis": diagnosis.hypothesis,
        "action": diagnosis.action,
        "success_metric": diagnosis.success_metric,
        "price_band_cents": list(diagnosis.price_band_cents),
        "margin_floor": diagnosis.margin_floor,
        "budget_tokens": diagnosis.budget_tokens,
        "planner": diagnosis.planner,
        "strategy": diagnosis.strategy,
    }


def refine_plan(
    plan: WeeklyPlan,
    *,
    client: LLMClient,
    trace: TraceRecorder | None = None,
    reviews: dict[str, list[dict]] | None = None,
) -> WeeklyPlan:
    """Let the strategist refine rule-shortlisted products, one at a time.

    Candidate selection and price bands remain deterministic. The model chooses
    only among problems that actually fired in ``diagnose``; any invalid reply
    leaves that product on the original rule diagnosis.
    """
    reviews = reviews or {}
    agent = StrategistAgent(client, trace)
    refined = 0
    failed = 0
    for diagnosis in plan.selected:
        product = diagnosis.product
        entry = _entry_variant(product)
        if entry is None:
            continue
        problems = diagnosis.candidates or (diagnosis.problem,)
        options: list[dict] = []
        bands: dict[str, tuple[int, int]] = {}
        for problem in problems:
            band = price_band(entry, problem, diagnosis.margin_floor)
            bands[problem] = band
            hypothesis, action, success_metric = PLAYBOOK[problem]
            options.append(
                {
                    "problem": problem,
                    "problem_label": PROBLEM_LABELS[problem],
                    "hypothesis": hypothesis,
                    "action": action,
                    "success_metric": success_metric,
                    "evidence": list(diagnosis.candidate_evidence.get(problem, diagnosis.evidence)),
                    "price_band_cents": list(band),
                    "price_points": [
                        {"index": index, "cents": cents}
                        for index, cents in enumerate(_three_price_points(*band))
                    ],
                }
            )
        task = diagnosis_to_dict(diagnosis)
        task["strategy_options"] = options
        ctx = WorkContext(
            product=product,
            task=task,
            metrics=product.metrics,
            reviews=list(reviews.get(product.product_id, []))[:5],
        )
        try:
            strategy = agent.run(ctx)
        except LLMError:
            diagnosis.planner = "rule (fallback)"
            diagnosis.strategy = {"planner": "rule (fallback)"}
            failed += 1
            continue

        problem = str(strategy["problem"])
        diagnosis.problem = problem
        diagnosis.hypothesis = str(strategy["hypothesis"] or PLAYBOOK[problem][0])
        diagnosis.action = str(strategy["action"] or PLAYBOOK[problem][1])
        diagnosis.success_metric = str(strategy["success_metric"] or PLAYBOOK[problem][2])
        diagnosis.evidence = tuple(strategy.get("evidence") or diagnosis.evidence)
        diagnosis.price_band_cents = bands[problem]
        diagnosis.budget_tokens = BUDGET_TOKENS[problem]
        diagnosis.planner = "llm"
        diagnosis.strategy = strategy
        refined += 1

    plan.notes = [note for note in plan.notes if not note.startswith("诊断先用规则")]
    plan.notes.append(
        f"策略 Agent：{refined} 个商品由模型确定问题、角度与价格档位；{failed} 个回落规则。"
    )
    plan.notes.append("候选商品与价格带由规则计算；模型不能新增问题类型、价格金额或写入操作。")
    return plan
