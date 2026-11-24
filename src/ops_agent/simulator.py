"""A small, transparent store simulator.

The simulator is deliberately simple and fully deterministic for a given seed.
It is not a forecast of a real store; it exists so that the three agents can be
tested end-to-end without scraping a platform or touching production data.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from datetime import date, timedelta
from zlib import crc32

from .catalog import CATEGORY_PROFILES
from .models import Market, Performance, SKU

MARKET_FACTOR: dict[Market, float] = {
    Market.US: 1.00,
    Market.DE: 0.93,
    Market.UK: 0.97,
}

# Monday .. Sunday
WEEKDAY_FACTOR = [0.98, 1.02, 1.00, 1.01, 1.08, 1.15, 1.10]

CHANGES = {
    "scene_image",
    "title_keywords",
    "detail_size_chart",
    "price_cut",
    "faster_shipping",
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _profile(sku: SKU) -> dict:
    try:
        return CATEGORY_PROFILES[sku.category]
    except KeyError as exc:
        raise ValueError(f"unknown category: {sku.category}") from exc


def category_label(sku: SKU) -> str:
    return str(_profile(sku)["label"])


def expected_ctr(sku: SKU) -> float:
    """Expected click-through rate before daily noise."""

    profile = _profile(sku)
    price_ratio = sku.price / float(profile["ref_price"])
    price_mult = _clamp(1.12 - 0.30 * (price_ratio - 1), 0.72, 1.22)
    multiplier = (
        (0.60 + 0.70 * sku.image_score)
        * (0.74 + 0.52 * sku.title_score)
        * price_mult
        * MARKET_FACTOR[sku.market]
    )
    return _clamp(float(profile["ctr"]) * multiplier, 0.002, 0.20)


def expected_cvr(sku: SKU) -> float:
    """Expected conversion rate before daily noise."""

    profile = _profile(sku)
    price_ratio = sku.price / float(profile["ref_price"])
    price_mult = _clamp(1.18 - 0.42 * (price_ratio - 1), 0.68, 1.26)
    ship_mult = _clamp(1.12 - 0.018 * (sku.shipping_days - 3), 0.82, 1.12)
    multiplier = (
        (0.62 + 0.75 * sku.detail_score)
        * (0.72 + 0.58 * sku.review_score)
        * price_mult
        * ship_mult
    )
    return _clamp(float(profile["cvr"]) * multiplier, 0.001, 0.15)


def expected_return_rate(sku: SKU) -> float:
    return _clamp(
        0.035 + 0.12 * (1 - sku.detail_score) + 0.04 * (1 - sku.review_score),
        0.02,
        0.18,
    )


def simulate_days(
    sku: SKU,
    days: int = 28,
    seed: int = 1,
    start: date = date(2026, 10, 1),
) -> list[Performance]:
    """Simulate daily performance for one SKU."""

    if days <= 0:
        raise ValueError("days must be positive")

    rng = random.Random(seed ^ crc32(sku.sku_id.encode("utf-8")))
    profile = _profile(sku)
    ctr_base = expected_ctr(sku)
    cvr_base = expected_cvr(sku)
    return_base = expected_return_rate(sku)

    rows: list[Performance] = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        base_impressions = (
            float(profile["impressions"])
            * WEEKDAY_FACTOR[day.weekday()]
            * rng.lognormvariate(0, 0.18)
        )
        impressions = max(0, int(round(base_impressions)))

        ctr = _clamp(ctr_base * rng.lognormvariate(0, 0.12), 0.0005, 0.40)
        clicks = min(impressions, int(round(impressions * ctr)))

        cvr = _clamp(cvr_base * rng.lognormvariate(0, 0.15), 0.0002, 0.50)
        orders = min(clicks, int(round(clicks * cvr)))

        return_rate = _clamp(return_base * rng.uniform(0.7, 1.3), 0.0, 0.50)
        returns = min(orders, int(round(orders * return_rate)))

        rows.append(
            Performance(
                sku_id=sku.sku_id,
                day=day,
                impressions=impressions,
                clicks=clicks,
                orders=orders,
                returns=returns,
            )
        )

    return rows


def apply_change(sku: SKU, change: str, value: float | None = None) -> SKU:
    """Return a new SKU with exactly one lever changed."""

    if change not in CHANGES:
        raise ValueError(f"unknown change: {change}")

    if change == "scene_image":
        return sku.replace(image_score=min(1.0, sku.image_score + 0.15))
    if change == "title_keywords":
        return sku.replace(title_score=min(1.0, sku.title_score + 0.10))
    if change == "detail_size_chart":
        return sku.replace(detail_score=min(1.0, sku.detail_score + 0.12))
    if change == "faster_shipping":
        return sku.replace(shipping_days=max(1, sku.shipping_days - 2))

    # price_cut
    pct = 0.05 if value is None else value
    if not 0 < pct < 0.5:
        raise ValueError("price_cut must be between 0 and 0.5")
    new_price = round(sku.price * (1 - pct), 2)
    if new_price <= sku.cost:
        raise ValueError("price cut would go below cost")
    return sku.replace(price=new_price)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def category_benchmarks(skus: list[SKU]) -> dict[str, tuple[float, float]]:
    """Per-category median CTR/CVR, used as the comparison baseline."""

    grouped: dict[str, list[SKU]] = defaultdict(list)
    for sku in skus:
        grouped[sku.category].append(sku)

    benchmarks: dict[str, tuple[float, float]] = {}
    for category, items in grouped.items():
        benchmarks[category] = (
            _median([expected_ctr(sku) for sku in items]),
            _median([expected_cvr(sku) for sku in items]),
        )
    return benchmarks


def opportunity_score(sku: SKU, benchmarks: dict[str, tuple[float, float]]) -> float:
    """Rank SKUs by how far they are below their category median.

    High-traffic gaps score higher because a fix there moves more revenue.
    """

    profile = _profile(sku)
    ctr_bench, cvr_bench = benchmarks[sku.category]
    ctr_gap = max(0.0, (ctr_bench - expected_ctr(sku)) / ctr_bench) if ctr_bench else 0.0
    cvr_gap = max(0.0, (cvr_bench - expected_cvr(sku)) / cvr_bench) if cvr_bench else 0.0
    traffic = 1 + math.log1p(float(profile["impressions"])) / 10
    return round((0.6 * ctr_gap + 0.4 * cvr_gap) * traffic, 4)


def summarize(skus: list[SKU]) -> list[dict]:
    """Return per-SKU diagnostics sorted by opportunity score."""

    benchmarks = category_benchmarks(skus)
    rows = [
        {
            "sku": sku,
            "label": category_label(sku),
            "ctr": expected_ctr(sku),
            "cvr": expected_cvr(sku),
            "return_rate": expected_return_rate(sku),
            "score": opportunity_score(sku, benchmarks),
        }
        for sku in skus
    ]
    return sorted(rows, key=lambda row: row["score"], reverse=True)
