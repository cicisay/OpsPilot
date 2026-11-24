"""Read-only snapshot adapter for offline demos and interviews.

Preview works from the synced snapshot; any real write is refused.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..site_platform import SiteAPIError, SiteProduct, load_snapshot


class SnapshotSite:
    def __init__(self, snapshot_path: Path | str) -> None:
        self.snapshot_path = Path(snapshot_path)
        self._products: list[SiteProduct] | None = None
        self._raw: dict[str, dict] = {}
        self._details: dict[str, dict] = {}

    def _load(self) -> None:
        if self._products is not None:
            return
        _, products = load_snapshot(self.snapshot_path)
        self._products = products
        payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        for raw in payload.get("products", []):
            product_id = str(raw["product_id"])
            self._raw[product_id] = raw
            variants = [
                {
                    "variantId": str(item["variant_id"]),
                    "priceCents": int(item["price_cents"]),
                    "costCents": item.get("cost_cents"),
                    "stock": int(item.get("stock", 0)),
                }
                for item in raw.get("variants", [])
            ]
            self._details[product_id] = {
                "productId": product_id,
                "i18n": {"En": {"title": raw.get("title_en", ""), "bullets": []}},
                "variants": variants,
            }

    def all_products(self) -> list[SiteProduct]:
        self._load()
        return list(self._products or [])

    def product(self, product_id: str) -> dict:
        self._load()
        detail = self._details.get(str(product_id))
        if detail is None:
            raise SiteAPIError(404, "not_found", "product not found in snapshot")
        return detail

    def patch_product(self, product_id, fields, reason=None, idempotency_key=None, dry_run=False):
        if not dry_run:
            raise SiteAPIError(403, "demo_read_only", "演示模式只读，不能写入站点")
        self._load()
        detail = self._details.get(str(product_id))
        if detail is None:
            raise SiteAPIError(404, "not_found", "product not found in snapshot")
        english = detail["i18n"]["En"]
        diff = {}
        if "titleEn" in fields and fields["titleEn"] != english.get("title"):
            diff["titleEn"] = {"before": english.get("title", ""), "after": fields["titleEn"]}
        if "bulletsEn" in fields and fields["bulletsEn"] != english.get("bullets"):
            diff["bulletsEn"] = {"before": english.get("bullets", []), "after": fields["bulletsEn"]}
        if "descEn" in fields and fields["descEn"]:
            diff["descEn"] = {"before": "", "after": fields["descEn"]}
        return {"ok": True, "dryRun": True, "diff": diff}

    def patch_variant(self, variant_id, price_cents=None, stock=None, reason=None, idempotency_key=None, dry_run=False):
        if not dry_run:
            raise SiteAPIError(403, "demo_read_only", "演示模式只读，不能写入站点")
        self._load()
        for detail in self._details.values():
            for variant in detail["variants"]:
                if variant["variantId"] == str(variant_id):
                    diff = {}
                    if price_cents is not None and int(price_cents) != variant["priceCents"]:
                        diff["priceCents"] = {"before": variant["priceCents"], "after": int(price_cents)}
                    return {"ok": True, "dryRun": True, "diff": diff}
        raise SiteAPIError(404, "not_found", "variant not found in snapshot")

    def rollback(self, product_id, version_id, reason=None):
        raise SiteAPIError(403, "demo_read_only", "演示模式只读，不能回滚")
