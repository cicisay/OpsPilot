"""Aggregation shared by the rule baseline and the director agent.

These functions compute numbers; they do not decide anything. Keeping them out
of ``supervisor.py`` matters because the director must never import the legacy
rule engine: the whole point of the three-agent rewrite is that no threshold in
that file can influence a model-driven week.

``math`` only, no I/O, so both callers stay cheap and testable.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from statistics import median

from .site_platform import SiteProduct, SiteVariant

# A product may not be priced below cost / (1 - floor). 16% is the accounting
# floor the store already uses; the model is told the number, never asked for it.
MARGIN_FLOOR = 0.16


@dataclass(frozen=True)
class Benchmark:
    pv: float
    inquiry_rate: float
    order_rate: float
    revenue_cents: float
    margin: float


def median_value(values) -> float:
    clean = [float(value) for value in values if value is not None]
    return float(median(clean)) if clean else 0.0


def entry_variant(product: SiteProduct) -> SiteVariant | None:
    """The cheapest variant: the one the storefront shows and the band anchors to."""
    return min(product.variants, key=lambda variant: variant.price_cents) if product.variants else None


def compute_benchmarks(products: list[SiteProduct]) -> tuple[dict[str, Benchmark], Benchmark]:
    """Per-category medians, falling back to the store median for thin categories.

    A category with fewer than three measured products has no meaningful median,
    so it borrows the store-wide one instead of pretending to be its own baseline.
    """
    with_metrics = [product for product in products if product.metrics]

    def make(items: list[SiteProduct]) -> Benchmark:
        return Benchmark(
            pv=median_value([item.metrics.pv for item in items]),
            inquiry_rate=median_value([item.metrics.inquiry_rate for item in items]),
            order_rate=median_value([item.metrics.order_rate for item in items]),
            revenue_cents=median_value([item.metrics.revenue_cents for item in items]),
            margin=median_value(
                [variant.margin for item in items for variant in item.variants if variant.margin is not None]
            ),
        )

    global_benchmark = make(with_metrics)
    grouped: dict[str, list[SiteProduct]] = defaultdict(list)
    for product in with_metrics:
        grouped[product.category_slug or "_"].append(product)

    by_category = {
        category: make(items) if len(items) >= 3 else global_benchmark
        for category, items in grouped.items()
    }
    return by_category, global_benchmark


def stock_days(product: SiteProduct, days: int) -> float:
    """Days of cover at the current sell-through rate.

    ``inf`` means stock exists but nothing sold in the window. That is a real
    signal (dead stock), so it is returned rather than flattened to zero; callers
    that serialise to JSON must convert it.
    """
    if not product.metrics:
        return 0.0
    entry = entry_variant(product)
    if entry is None:
        return 0.0
    daily_units = product.metrics.units / days if days > 0 else 0.0
    if daily_units <= 0:
        return float("inf") if entry.stock > 0 else 0.0
    return entry.stock / daily_units


def cost_floor_cents(entry: SiteVariant | None, margin_floor: float = MARGIN_FLOOR) -> int:
    """Lowest price that still clears the margin floor. Zero when cost is unknown."""
    if entry is None or not entry.cost_cents:
        return 0
    floor = min(0.95, max(0.0, float(margin_floor)))
    return math.ceil(entry.cost_cents / (1 - floor))


def price_band(entry: SiteVariant, problem: str, margin_floor: float = MARGIN_FLOOR) -> tuple[int, int]:
    """Legacy rule-path band: +/-10% around the current price, floored at cost.

    The director no longer uses this; it stays because the rule baseline and its
    tests still describe the old behaviour.
    """
    base_low = int(round(entry.price_cents * 0.9))
    base_high = int(round(entry.price_cents * 1.1))
    floor_price = cost_floor_cents(entry, margin_floor)
    low = max(base_low, floor_price)
    if problem == "low_margin":
        low = max(low, entry.price_cents)
        high = max(entry.price_cents, base_high)
    else:
        high = max(low, base_high)
    return low, high
