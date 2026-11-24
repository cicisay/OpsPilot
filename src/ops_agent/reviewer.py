"""合规审查 Agent：只拿成品与规则，不看创作理由。

独立于内容工坊：输入是 variant + 商品事实，输出 pass/fail、违规项和建议。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .compliance import ComplianceChecker
from .site_platform import SiteProduct

MARGIN_FLOOR = 0.16


@dataclass(frozen=True)
class ReviewDecision:
    variant_key: str
    passed: bool
    violations: tuple[str, ...]
    suggestions: tuple[str, ...]
    rule_version: str


class ComplianceReviewer:
    def __init__(self, checker: ComplianceChecker | None = None) -> None:
        self.checker = checker or ComplianceChecker()

    def review(self, variant: dict, product: SiteProduct, market: str = "US") -> ReviewDecision:
        title = str(variant.get("title", ""))
        bullets = [str(item) for item in (variant.get("bullets") or [])]
        description = str(variant.get("description", ""))
        image_brief = str(variant.get("image_brief", ""))
        price_cents = int(variant.get("price_cents", 0))

        violations: list[str] = []
        suggestions: list[str] = []

        text = " ".join([title, *bullets, description])
        report = self.checker.check(text, image_brief, product.category_slug or "", market)
        violations.extend(item.message for item in report.violations)
        suggestions.extend(item.suggestion for item in report.violations)

        if not 1 <= len(title) <= 300:
            violations.append("标题长度必须在 1–300 字符之间")
            suggestions.append("把标题压到 300 字符以内")
        if not 1 <= len(bullets) <= 20:
            violations.append("卖点数量必须在 1–20 条之间")
            suggestions.append("保留 4 条最关键的卖点")
        if any(len(item) > 500 for item in bullets):
            violations.append("单条卖点不能超过 500 字符")
            suggestions.append("拆分过长卖点")
        if len(description) > 20000:
            violations.append("描述不能超过 20000 字符")
            suggestions.append("精简描述")

        entry = min(product.variants, key=lambda item: item.price_cents) if product.variants else None
        if price_cents <= 0:
            violations.append("价格必须大于 0")
            suggestions.append("给一个正数价格")
        elif entry and entry.cost_cents:
            floor_price = math.ceil(entry.cost_cents / (1 - MARGIN_FLOOR))
            if price_cents < floor_price:
                violations.append(
                    f"价格 {price_cents / 100:.2f} 低于毛利底线 {floor_price / 100:.2f}"
                )
                suggestions.append("把价格抬到毛利底线以上，或先谈成本")

        return ReviewDecision(
            variant_key=str(variant.get("variant_key", "")),
            passed=not violations,
            violations=tuple(violations),
            suggestions=tuple(suggestions),
            rule_version=report.lexicon_version,
        )
