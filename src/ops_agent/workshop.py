"""内容工坊：把 TaskOrder 变成 2–5 版可发布的方案。

两条路径共用同一套输出结构：

- ``planner="rule"``：确定性模板，零成本、可回归，是离线基准（Q2 的结论：保留）。
- ``planner="llm"``：交给 ``agents/workshop_agent.py``，版数、角度、文案、自评
  全部由模型决定；代码只守红线（英文、数字可溯源、价位点）与契约。

关键差异：llm 路径**不再回落规则**。旧实现里模型写错会悄悄换成模板，运营台看到
的是"成功"，实际是另一条代码路径。现在把校验错误交回模型定向重写；重写仍失败就
记录失败原因，绝不伪装成模型产出。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .agents.workshop_agent import WorkshopError, run_workshop_task
from .compliance import ComplianceChecker
from .llm import LLMClient, LLMError, LLMNotConfigured, TraceRecorder
from .site_platform import SiteProduct

# (key, 中文角度, 英文标题后缀, 英文卖点, 预期提升)
ANGLES: dict[str, list[tuple[str, str, str, str, float]]] = {
    "low_order": [
        ("tier-price", "阶梯价", "Bulk Pricing Available", "Tier pricing and a free sample for qualified buyers.", 0.12),
        ("moq-first", "低起订", "Low MOQ Trial Order", "Start with a small trial order and scale after validation.", 0.10),
        ("lead-time", "快交期", "Fast Lead Time", "Ready stock and a short lead time for repeat orders.", 0.08),
    ],
    "low_inquiry": [
        ("certification", "认证与质检", "Certified & QC'd", "CE/RoHS documentation and a batch QC report with every order.", 0.12),
        ("scenario", "应用场景", "Real Application Photos", "Real application photos for retail, hospitality and gifting.", 0.10),
        ("spec", "规格尺寸", "Full Specs & Sizes", "Full spec sheet, size chart and packing details.", 0.08),
    ],
    "low_traffic": [
        ("keyword", "关键词", "Long-Tail Keyword Coverage", "Long-tail keywords for the exact use case buyers search.", 0.12),
        ("category", "类目属性", "Complete Category Attributes", "Complete category attributes for better matching.", 0.10),
        ("benefit", "卖点前置", "Benefit-Led Title", "Lead with the one benefit buyers search for.", 0.08),
    ],
    "high_negative": [
        ("quality", "质量证据", "Batch QC Included", "Batch QC photos and material certificates with every order.", 0.12),
        ("description", "描述对齐", "Description Matched to Product", "Rewrite the description to match the actual product.", 0.10),
        ("service", "售后承诺", "After-Sales Support", "Replacement policy and a 24-hour response window.", 0.08),
    ],
    "overstock": [
        ("clearance", "清仓组合", "Clearance Bundle", "Bundle with a fast-moving SKU and test a lower price.", 0.10),
        ("value", "价值组合", "Value Bundle", "Add a free accessory instead of cutting the price.", 0.08),
    ],
    "stock_risk": [
        ("restock", "补货预告", "Restock Reserved", "Reserve stock and publish a restock date.", 0.10),
        ("preorder", "预售", "Preorder Available", "Preorder with a clear delivery window.", 0.08),
    ],
    "low_margin": [
        ("value", "价值提价", "Value-Added Packaging", "Raise price with added QC and packaging.", 0.10),
        ("cost", "成本谈判", "Cost-Optimized", "Negotiate material cost before changing the price.", 0.08),
    ],
}

ANGLE_BULLETS: dict[str, list[str]] = {
    "tier-price": ["Tier pricing from 100 pcs", "Free sample for qualified buyers"],
    "moq-first": ["Low MOQ trial order", "Scale up after sample approval"],
    "lead-time": ["Ready stock for repeat orders", "Production slot held on request"],
    "certification": ["CE/RoHS documentation", "Batch QC report included"],
    "scenario": ["Real application photos", "Retail, hospitality and gifting use"],
    "spec": ["Full spec sheet", "Size chart and packing details"],
    "keyword": ["Long-tail keyword coverage", "Exact use-case phrases"],
    "category": ["Complete category attributes", "Better matching in search"],
    "benefit": ["Benefit-led title", "One clear buying reason"],
    "quality": ["Batch QC photos", "Material certificates"],
    "description": ["Description matched to product", "No overstated claims"],
    "service": ["Replacement policy", "24-hour response window"],
    "clearance": ["Bundle with a fast mover", "Test a lower price"],
    "value": ["Free accessory included", "Keep the list price"],
    "restock": ["Reserve stock now", "Restock date published"],
    "preorder": ["Preorder window", "Clear delivery date"],
}

DEFAULT_IMAGE_BRIEF = "Product in use, natural light, no visible logos or trademarks, no faces"
DEFAULT_VIDEO_SCRIPT = "15s vertical video: problem, product in use, close-up, call to action."


@dataclass
class WorkshopVariant:
    variant_key: str
    angle: str
    title: str
    bullets: tuple[str, ...]
    description: str
    price_cents: int
    image_brief: str
    video_script: str
    expected_lift: float
    compliance_passed: bool
    violations: tuple[str, ...]
    preview: dict
    # Set by the workshop agent; empty on the rule baseline.
    material_brief: tuple[str, ...] = ()
    self_review: str = ""
    self_check: dict = field(default_factory=dict)


@dataclass
class Proposal:
    product_id: str
    title: str
    problem: str
    task: dict
    variants: list[WorkshopVariant]
    target_variant_id: str = ""
    planner: str = "rule"
    critique: dict = field(default_factory=dict)
    revisions: int = 0


def _price_points(low: int, high: int, count: int) -> list[int]:
    """Evenly spaced points, one per variant, for any count in 2..5.

    The old implementation returned at most three values, which silently capped
    a task that asked for four or five variants.
    """
    count = max(2, int(count))
    low = max(1, int(low))
    high = max(low, int(high))
    if high == low:
        return [low] * count
    step = (high - low) / (count - 1)
    return [int(round(low + step * index)) for index in range(count)]


def _price_plan(task: dict, entry, count: int) -> list[int]:
    """Price band -> concrete price points. The model never computes these."""
    explicit = task.get("price_points")
    if isinstance(explicit, list):
        cleaned = [int(item) for item in explicit if isinstance(item, (int, float)) and int(item) > 0]
        if len(cleaned) == count == len(explicit):
            return cleaned

    raw_low, raw_high = task.get("price_band_cents") or [entry.price_cents, entry.price_cents]
    band_low, band_high = int(raw_low), int(raw_high)
    if band_low > band_high:
        band_low, band_high = band_high, band_low
    band_low = max(1, band_low)
    margin_floor = task.get("margin_floor", 0.16)
    try:
        margin_floor = float(margin_floor)
    except (TypeError, ValueError):
        margin_floor = 0.16
    margin_floor = min(0.95, max(0.0, margin_floor))
    floor_price = math.ceil(entry.cost_cents / (1 - margin_floor)) if entry.cost_cents else 0
    band_low = max(band_low, floor_price)
    band_high = max(band_high, band_low)
    return _price_points(band_low, band_high, count)


_KNOWN_SUFFIXES = tuple(
    {suffix for group in ANGLES.values() for (_key, _label, suffix, _pitch, _lift) in group}
)


def _base_title(current: str) -> str:
    """Strip a suffix a previous publish already appended."""
    base = current.strip()
    for known in _KNOWN_SUFFIXES:
        if base.endswith(known):
            return base[: -len(known)].rstrip(" -—–,").strip()
    return base


def _title(current: str, suffix: str) -> str:
    """Append a suffix without stacking it on a previous run's suffix."""
    return f"{_base_title(current)} — {suffix}"[:300]


def _bullets(product: SiteProduct, angle_key: str) -> tuple[str, ...]:
    base = [
        f"MOQ {product.moq} pcs · lead time {product.lead_time_days} days",
        f"{product.incoterm} terms · sample available",
        "Real product photos and full size chart",
        "Batch QC report before shipment",
    ]
    specific = ANGLE_BULLETS.get(angle_key, [])
    return tuple((specific + base)[:4])


def _build_variant(
    *,
    product: SiteProduct,
    current_title: str,
    current_bullets: list[str],
    base_price: int,
    variant_key: str,
    angle_label: str,
    title: str,
    bullets: list[str],
    description: str,
    price_cents: int,
    image_brief: str,
    video_script: str,
    expected_lift: float,
    checker: ComplianceChecker,
    market: str,
) -> WorkshopVariant:
    text = " ".join([title, *bullets, description])
    report = checker.check(text, image_brief, product.category_slug or "", market)
    return WorkshopVariant(
        variant_key=variant_key,
        angle=angle_label,
        title=title,
        bullets=tuple(bullets),
        description=description,
        price_cents=price_cents,
        image_brief=image_brief,
        video_script=video_script,
        expected_lift=expected_lift,
        compliance_passed=report.passed,
        violations=tuple(item.message for item in report.violations),
        preview={
            "title": {"before": current_title, "after": title},
            "bulletsEn": {"before": current_bullets, "after": list(bullets)},
            "priceCents": {"before": base_price, "after": price_cents},
        },
    )


def _rule_variants(
    *,
    product: SiteProduct,
    task: dict,
    entry,
    current_title: str,
    current_bullets: list[str],
    prices: list[int],
    checker: ComplianceChecker,
    market: str,
) -> list[WorkshopVariant]:
    angles = ANGLES.get(str(task.get("problem")), ANGLES["low_inquiry"])
    subject = _base_title(current_title) or product.title_en
    variants: list[WorkshopVariant] = []
    for (angle_key, angle_label, suffix, pitch, lift), price in zip(angles, prices):
        title = _title(current_title, suffix)
        bullets = list(_bullets(product, angle_key))
        description = (
            f"{subject} is built for {product.category_slug or 'wholesale'} buyers. "
            f"{pitch} MOQ, lead time and QC documents are available on request."
        )
        variants.append(
            _build_variant(
                product=product,
                current_title=current_title,
                current_bullets=current_bullets,
                base_price=entry.price_cents,
                variant_key=f"{product.product_id}-{angle_key}",
                angle_label=angle_label,
                title=title,
                bullets=bullets,
                description=description,
                price_cents=price,
                image_brief=f"Scene for {suffix}: {DEFAULT_IMAGE_BRIEF}",
                video_script=DEFAULT_VIDEO_SCRIPT,
                expected_lift=lift,
                checker=checker,
                market=market,
            )
        )
    return variants


def _agent_variants(
    *,
    client: LLMClient,
    trace: TraceRecorder | None,
    product: SiteProduct,
    task: dict,
    entry,
    current_title: str,
    current_bullets: list[str],
    current_description: str,
    reviews: list[dict],
    detail: dict | None,
    market: str,
    checker: ComplianceChecker,
) -> tuple[list[WorkshopVariant], dict, int]:
    """Delegate to the workshop agent; no rule fallback on failure."""
    outcome = run_workshop_task(
        task=task,
        product=product,
        client=client,
        market=market,
        detail=detail,
        reviews=reviews,
        trace=trace,
    )

    variants: list[WorkshopVariant] = []
    for index, item in enumerate(outcome.variants, start=1):
        brief = list(item.get("material_brief") or [])
        image_brief = "；".join(brief) if brief else DEFAULT_IMAGE_BRIEF
        variant = _build_variant(
            product=product,
            current_title=current_title,
            current_bullets=current_bullets,
            base_price=entry.price_cents,
            variant_key=str(item.get("variant_key") or f"{product.product_id}-v{index}"),
            angle_label=str(item.get("angle") or f"方案 {index}"),
            title=str(item["title"]),
            bullets=list(item["bullets"]),
            description=str(item["description"]),
            price_cents=int(item["price_cents"]),
            image_brief=image_brief,
            video_script=DEFAULT_VIDEO_SCRIPT,
            expected_lift=float(item.get("expected_lift") or 0.05),
            checker=checker,
            market=market,
        )
        # Keep the agent's own audit trail on the variant, not just the summary.
        variant.material_brief = tuple(brief)
        variant.self_review = str(item.get("self_review") or "")
        variant.self_check = dict(item.get("self_check") or {})
        variants.append(variant)

    critique = {
        "planner": "workshop-agent",
        "attempts": outcome.attempts,
        "issues": list(outcome.issues),
        "variant_reason": str(outcome.proposal.get("variant_reason") or ""),
    }
    return variants, critique, max(0, outcome.attempts - 1)


def build_proposals(
    tasks: list[dict],
    products: dict[str, SiteProduct],
    details: dict[str, dict] | None = None,
    limit: int = 3,
    checker: ComplianceChecker | None = None,
    market: str = "US",
    planner: str = "rule",
    client: LLMClient | None = None,
    trace: TraceRecorder | None = None,
    reviews: dict[str, list[dict]] | None = None,
    failures: list[dict] | None = None,
) -> list[Proposal]:
    """Build one proposal per task. ``failures`` collects tasks the model could not do.

    A failed task is reported, never replaced by a rule-built proposal: a human
    must be able to tell "the model wrote this" from "the template wrote this".
    """
    checker = checker or ComplianceChecker()
    details = details or {}
    reviews = reviews or {}
    proposals: list[Proposal] = []
    use_llm = planner == "llm" and client is not None and client.enabled

    for task in tasks[:limit]:
        # 意图层产出的是计划草案（status="draft"）：没有 evidence、价格带只是预演值。
        # 它可以被预览、被人工确认，但不能直接开工坊——否则"看一眼"就变成了"开始写文案"。
        if str(task.get("status") or "").strip().lower() == "draft":
            if failures is not None:
                failures.append(
                    {
                        "product_id": str(task.get("product_id")),
                        "reason": "草稿单（status=draft）未定稿，不进入内容工坊；先确认计划再执行",
                    }
                )
            continue

        product = products.get(str(task.get("product_id")))
        if product is None or not product.variants:
            if failures is not None:
                failures.append(
                    {
                        "product_id": str(task.get("product_id")),
                        "reason": "商品不在快照中或没有变体",
                    }
                )
            continue

        entry = min(product.variants, key=lambda variant: variant.price_cents)
        current_title = product.title_en
        current_bullets: list[str] = []
        current_description = ""
        detail = details.get(product.product_id)
        if detail:
            english = (detail.get("i18n") or {}).get("En") or {}
            current_title = english.get("title") or current_title
            current_bullets = list(english.get("bullets") or [])
            current_description = str(english.get("desc") or "")

        angles = ANGLES.get(str(task.get("problem")), ANGLES["low_inquiry"])
        variants: list[WorkshopVariant] = []
        planner_used = "rule"
        critique: dict = {}
        revisions = 0

        if use_llm:
            try:
                variants, critique, revisions = _agent_variants(
                    client=client,
                    trace=trace,
                    product=product,
                    task=task,
                    entry=entry,
                    current_title=current_title,
                    current_bullets=current_bullets,
                    current_description=current_description,
                    reviews=reviews.get(product.product_id, []),
                    detail=detail,
                    checker=checker,
                    market=market,
                )
                planner_used = "llm"
            except (LLMError, LLMNotConfigured, WorkshopError) as exc:
                issues = getattr(exc, "issues", []) or [str(exc)]
                if failures is not None:
                    failures.append(
                        {
                            "product_id": product.product_id,
                            "reason": str(exc),
                            "issues": list(issues)[:8],
                        }
                    )
                continue

        if not variants:
            variants = _rule_variants(
                product=product,
                task=task,
                entry=entry,
                current_title=current_title,
                current_bullets=current_bullets,
                prices=_price_plan(task, entry, len(angles)),
                checker=checker,
                market=market,
            )
            planner_used = "rule"

        proposals.append(
            Proposal(
                product_id=product.product_id,
                title=current_title,
                problem=str(task.get("problem", "")),
                task=task,
                variants=variants,
                target_variant_id=entry.variant_id,
                planner=planner_used,
                critique=critique,
                revisions=revisions,
            )
        )

    return proposals


def proposals_to_dict(store_slug: str, proposals: list[Proposal]) -> dict:
    return {
        "store": store_slug,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "proposals": [
            {
                "product_id": proposal.product_id,
                "title": proposal.title,
                "problem": proposal.problem,
                "target_variant_id": proposal.target_variant_id,
                "task": proposal.task,
                "planner": proposal.planner,
                "critique": proposal.critique,
                "revisions": proposal.revisions,
                "variants": [asdict(variant) for variant in proposal.variants],
            }
            for proposal in proposals
        ],
    }
