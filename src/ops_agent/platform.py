"""A local mock of the store platform.

It owns the catalog, the simulated performance data and the listing versions.
Everything is file-backed and deterministic; no platform API or scraping is
involved.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .catalog import load_catalog
from .models import Performance, SKU, jsonable
from .simulator import simulate_days


@dataclass(frozen=True)
class ListingRecord:
    version: int
    sku_id: str
    title: str
    bullets: list[str]
    description: str
    price: float
    image_brief: str
    published_at: datetime
    status: str = "published"
    approval_token: str | None = None


def _baseline_record(sku: SKU) -> ListingRecord:
    return ListingRecord(
        version=1,
        sku_id=sku.sku_id,
        title=sku.title,
        bullets=[
            f"{sku.title} for {sku.market.value} customers",
            "Compact design, easy to store",
            "Ships from a local warehouse",
        ],
        description=f"Baseline listing for {sku.sku_id}.",
        price=sku.price,
        image_brief="Baseline studio photo, no logo, no people",
        published_at=datetime(2026, 10, 1, 9, 0, 0),
        status="baseline",
    )


class MockPlatform:
    def __init__(
        self,
        skus: list[SKU],
        performance: dict[str, list[Performance]] | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.skus: dict[str, SKU] = {sku.sku_id: sku for sku in skus}
        self.performance: dict[str, list[Performance]] = performance or {}
        self.state_path = state_path
        self.listings: dict[str, list[ListingRecord]] = {
            sku.sku_id: [_baseline_record(sku)] for sku in skus
        }

    @classmethod
    def from_data(cls, data_dir: Path) -> "MockPlatform":
        data_dir = Path(data_dir)
        skus = load_catalog(data_dir / "skus.json")
        performance = _load_performance(data_dir / "performance.csv")
        if not performance:
            performance = {
                sku.sku_id: simulate_days(sku, days=28, seed=42) for sku in skus
            }
        return cls(skus, performance, state_path=data_dir / "platform_state.json")

    def current(self, sku_id: str) -> ListingRecord:
        return self.listings[sku_id][-1]

    def snapshot(self, days: int = 7, sku_id: str | None = None) -> list[dict]:
        ids = [sku_id] if sku_id else list(self.skus)
        result: list[dict] = []
        for current_id in ids:
            sku = self.skus[current_id]
            rows = sorted(self.performance.get(current_id, []), key=lambda row: row.day)[-days:]
            impressions = sum(row.impressions for row in rows)
            clicks = sum(row.clicks for row in rows)
            orders = sum(row.orders for row in rows)
            returns = sum(row.returns for row in rows)
            result.append(
                {
                    "sku_id": sku.sku_id,
                    "title": sku.title,
                    "category": sku.category,
                    "market": sku.market.value,
                    "price": sku.price,
                    "margin": round(sku.margin, 4),
                    "impressions": impressions,
                    "clicks": clicks,
                    "orders": orders,
                    "returns": returns,
                    "ctr": round(clicks / impressions, 4) if impressions else 0.0,
                    "cvr": round(orders / clicks, 4) if clicks else 0.0,
                    "return_rate": round(returns / orders, 4) if orders else 0.0,
                    "days": days,
                }
            )
        return result

    def publish(self, sku_id: str, record: ListingRecord) -> ListingRecord:
        self.listings[sku_id].append(record)
        self.skus[sku_id] = self.skus[sku_id].replace(price=record.price)
        self._save()
        return record

    def rollback(self, sku_id: str) -> ListingRecord:
        records = self.listings[sku_id]
        if len(records) < 2:
            raise ValueError("no published version to roll back")
        records.pop()
        current = records[-1]
        self.skus[sku_id] = self.skus[sku_id].replace(price=current.price)
        self._save()
        return current

    def _save(self) -> None:
        if not self.state_path:
            return
        payload = {
            "listings": {
                sku_id: [jsonable(record) for record in records]
                for sku_id, records in self.listings.items()
            }
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _load_performance(path: Path) -> dict[str, list[Performance]]:
    if not path.exists():
        return {}
    performance: dict[str, list[Performance]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            performance.setdefault(row["sku_id"], []).append(
                Performance(
                    sku_id=row["sku_id"],
                    day=date.fromisoformat(row["day"]),
                    impressions=int(row["impressions"]),
                    clicks=int(row["clicks"]),
                    orders=int(row["orders"]),
                    returns=int(row["returns"]),
                )
            )
    return performance
