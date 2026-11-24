"""Deterministic synthetic catalog used by the whole project.

The catalog is generated, not scraped: it mirrors the shape of a cross-border
store (category, market, price, content scores, stock, shipping) so the agents
can be evaluated offline without touching real platform data.
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

from .models import Market, SKU, jsonable

START_DATE = date(2026, 10, 29)

CATEGORY_PROFILES: dict[str, dict] = {
    "home-fragrance": {
        "label": "家居香氛",
        "ctr": 0.041,
        "cvr": 0.023,
        "ref_price": 22.0,
        "impressions": 900,
        "titles": ["Scented Candle Gift Set", "Reed Diffuser", "Wax Melt Trio"],
    },
    "kitchen-storage": {
        "label": "厨房收纳",
        "ctr": 0.038,
        "cvr": 0.026,
        "ref_price": 19.0,
        "impressions": 1000,
        "titles": ["Bamboo Drawer Organizer", "Stackable Spice Rack", "Under-Sink Caddy"],
    },
    "garden": {
        "label": "园艺",
        "ctr": 0.033,
        "cvr": 0.019,
        "ref_price": 26.0,
        "impressions": 700,
        "titles": ["Ceramic Planter Set", "Self-Watering Pot", "Garden Tool Kit"],
    },
    "bedroom-storage": {
        "label": "卧室收纳",
        "ctr": 0.036,
        "cvr": 0.024,
        "ref_price": 24.0,
        "impressions": 800,
        "titles": ["Cotton Rope Basket", "Under-Bed Storage Bag", "Closet Divider Set"],
    },
    "lighting": {
        "label": "灯具",
        "ctr": 0.030,
        "cvr": 0.017,
        "ref_price": 34.0,
        "impressions": 600,
        "titles": ["Rattan Pendant Lamp", "LED Table Lamp", "Wall Sconce"],
    },
    "pet": {
        "label": "宠物用品",
        "ctr": 0.044,
        "cvr": 0.030,
        "ref_price": 15.0,
        "impressions": 1100,
        "titles": ["Pet Grooming Brush", "Cat Tunnel Toy", "Dog Feeding Mat"],
    },
    "outdoor": {
        "label": "户外",
        "ctr": 0.027,
        "cvr": 0.015,
        "ref_price": 39.0,
        "impressions": 500,
        "titles": ["Camping Lantern", "Folding Chair", "Picnic Blanket"],
    },
}

MARKETS = [Market.US, Market.DE, Market.UK]
ADJECTIVES = ["Premium", "Minimalist", "Eco-Friendly", "Handmade", "Compact"]


def generate_catalog(count: int = 50, seed: int = 42) -> list[SKU]:
    """Generate a reproducible catalog of ``count`` SKUs."""

    if count <= 0:
        raise ValueError("count must be positive")

    rng = random.Random(seed)
    categories = list(CATEGORY_PROFILES)
    skus: list[SKU] = []

    for index in range(count):
        category = categories[index % len(categories)]
        profile = CATEGORY_PROFILES[category]
        market = MARKETS[index % len(MARKETS)]

        title = f"{rng.choice(ADJECTIVES)} {rng.choice(profile['titles'])}"
        cost = round(float(profile["ref_price"]) * rng.uniform(0.35, 0.55), 2)
        margin = rng.uniform(0.30, 0.55)
        price = round(cost / (1 - margin), 2)

        skus.append(
            SKU(
                sku_id=f"SKU-{1001 + index}",
                title=title,
                category=category,
                market=market,
                cost=cost,
                price=price,
                stock_days=rng.randint(5, 90),
                image_score=round(rng.uniform(0.45, 0.88), 3),
                title_score=round(rng.uniform(0.50, 0.90), 3),
                detail_score=round(rng.uniform(0.45, 0.90), 3),
                review_score=round(rng.uniform(0.40, 0.85), 3),
                shipping_days=rng.randint(3, 14),
                created_at=START_DATE - timedelta(days=rng.randint(5, 240)),
            )
        )

    return skus


def save_catalog(path: Path, skus: list[SKU]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [jsonable(sku) for sku in skus]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_catalog(path: Path) -> list[SKU]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    skus: list[SKU] = []
    for item in raw:
        data = dict(item)
        data["market"] = Market(data["market"])
        data["created_at"] = date.fromisoformat(data["created_at"])
        skus.append(SKU(**data))
    return skus
