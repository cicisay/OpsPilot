#!/usr/bin/env python3
"""Build a 500-product snapshot for the large-catalogue check.

Clones the products of an existing snapshot, gives each clone a fresh id and
perturbs its 30-day metrics deterministically. Output goes to a temp directory
by default: this script must never touch ``data/`` or replace the real snapshot.

    python scripts/make_large_snapshot.py --count 500
    python scripts/make_large_snapshot.py --count 500 --out /tmp/big/site_snapshot.json
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from zlib import crc32

DEFAULT_SOURCE = "data/tenants/globaltrade/site_snapshot.json"


def _perturb(value: int, seed: int, spread: float = 0.45) -> int:
    """Deterministic +/- spread, never negative."""
    if value <= 0:
        return value
    factor = 1 + ((seed % 1000) / 1000.0 - 0.5) * 2 * spread
    return max(1, int(round(value * factor)))


def build(source: Path, count: int) -> dict:
    payload = json.loads(source.read_text(encoding="utf-8"))
    base = [item for item in payload.get("products", []) if item]
    if not base:
        raise SystemExit(f"{source} 没有 products")
    products = []
    for index in range(count):
        template = base[index % len(base)]
        clone = json.loads(json.dumps(template))
        original = str(template.get("product_id") or "p")
        product_id = f"{original}-c{index:04d}"
        seed = crc32(product_id.encode("utf-8"))
        clone["product_id"] = product_id
        clone["slug"] = f"{template.get('slug') or original}-c{index:04d}"
        metrics = clone.get("metrics")
        if isinstance(metrics, dict):
            for key in ("pv", "uv", "inquiries", "orders", "units", "reviews", "negative_reviews"):
                if key in metrics:
                    metrics[key] = _perturb(int(metrics.get(key) or 0), seed + len(key))
            if "revenue_cents" in metrics:
                metrics["revenue_cents"] = _perturb(int(metrics["revenue_cents"] or 0), seed + 7)
            # A few products must look dead or out of stock, or the fixture
            # never exercises the flags path.
            if index % 37 == 0:
                metrics["units"] = 0
                metrics["orders"] = 0
        for position, variant in enumerate(clone.get("variants") or []):
            variant["variant_id"] = f"{variant.get('variant_id')}-c{index:04d}"
            variant["sku_code"] = f"{variant.get('sku_code') or 'SKU'}-{index:04d}-{position}"
            variant["stock"] = _perturb(int(variant.get("stock") or 0), seed + position, 0.6)
        clone["title_en"] = f"{template.get('title_en') or 'Product'} #{index:03d}"
        products.append(clone)
    result = dict(payload)
    result["products"] = products
    result["generated_by"] = "scripts/make_large_snapshot.py"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="生成大目录快照（临时目录）")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.exists():
        print(f"找不到源快照: {source}")
        return 1
    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="ops-pilot-large-")) / "site_snapshot.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = build(source, args.count)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(payload['products'])} 个商品 -> {out}")
    print(f"运行: python run.py director run --offline --runtime agent --limit 5 --snapshot {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
