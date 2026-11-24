"""Adapter for the real store's Agent API v1.1.

The adapter speaks HTTP only; it never imports store code. Tests inject a fake
transport, so no network is required in CI.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

DEFAULT_BASE_URL = "http://localhost:3000/api/agent"

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _safe_id(value: str, label: str) -> str:
    text = str(value)
    if not _ID_PATTERN.fullmatch(text):
        raise ValueError(f"invalid {label}: {value!r}")
    return text


class SiteAPIError(Exception):
    def __init__(self, status: int, code: str | None, message: str) -> None:
        super().__init__(f"{status} {code or ''}: {message}".strip())
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SiteStore:
    store_id: str
    slug: str
    name: str
    country: str | None
    currency: str
    timezone: str | None


@dataclass(frozen=True)
class SiteVariant:
    variant_id: str
    sku_code: str
    price_cents: int
    cost_cents: int | None
    stock: int
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def price(self) -> float:
        return self.price_cents / 100

    @property
    def margin(self) -> float | None:
        if not self.cost_cents or self.price_cents <= 0:
            return None
        return (self.price_cents - self.cost_cents) / self.price_cents


@dataclass(frozen=True)
class SiteMetrics:
    days: int
    pv: int
    uv: int
    inquiries: int
    orders: int
    units: int
    revenue_cents: int
    reviews: int
    avg_rating: float | None
    negative_reviews: int

    @property
    def inquiry_rate(self) -> float:
        return self.inquiries / self.pv if self.pv else 0.0

    @property
    def order_rate(self) -> float:
        return self.orders / self.pv if self.pv else 0.0

    @property
    def revenue(self) -> float:
        return self.revenue_cents / 100


@dataclass(frozen=True)
class SiteProduct:
    product_id: str
    slug: str
    title_en: str
    status: str
    category_id: str | None
    category_slug: str | None
    moq: int
    lead_time_days: int
    incoterm: str
    variants: tuple[SiteVariant, ...]
    metrics: SiteMetrics | None

    @property
    def min_price_cents(self) -> int:
        return min((v.price_cents for v in self.variants), default=0)

    @property
    def max_price_cents(self) -> int:
        return max((v.price_cents for v in self.variants), default=0)

    @property
    def stock_total(self) -> int:
        return sum(v.stock for v in self.variants)

    @property
    def min_margin(self) -> float | None:
        margins = [v.margin for v in self.variants if v.margin is not None]
        return min(margins) if margins else None


def _metrics_from_api(raw: dict | None) -> SiteMetrics | None:
    if not raw:
        return None
    return SiteMetrics(
        days=int(raw.get("days", 0)),
        pv=int(raw.get("pv", 0)),
        uv=int(raw.get("uv", 0)),
        inquiries=int(raw.get("inquiries", 0)),
        orders=int(raw.get("orders", 0)),
        units=int(raw.get("units", 0)),
        revenue_cents=int(raw.get("revenueCents", 0)),
        reviews=int(raw.get("reviews", 0)),
        avg_rating=raw.get("avgRating"),
        negative_reviews=int(raw.get("negativeReviews", 0)),
    )


def _variant_from_api(raw: dict) -> SiteVariant:
    return SiteVariant(
        variant_id=str(raw["variantId"]),
        sku_code=str(raw.get("skuCode", "")),
        price_cents=int(raw.get("priceCents", 0)),
        cost_cents=int(raw["costCents"]) if raw.get("costCents") is not None else None,
        stock=int(raw.get("stock", 0)),
        attrs=raw.get("attrs") or {},
    )


def _product_from_api(raw: dict) -> SiteProduct:
    return SiteProduct(
        product_id=str(raw["productId"]),
        slug=str(raw.get("slug", "")),
        title_en=str(raw.get("titleEn", "")),
        status=str(raw.get("status", "")),
        category_id=raw.get("categoryId"),
        category_slug=raw.get("categorySlug"),
        moq=int(raw.get("moq", 1)),
        lead_time_days=int(raw.get("leadTimeDays", 15)),
        incoterm=str(raw.get("incoterm", "FOB")),
        variants=tuple(_variant_from_api(item) for item in raw.get("variants", [])),
        metrics=_metrics_from_api(raw.get("metrics")),
    )


class SitePlatform:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: str | None = None,
        timeout: float = 10.0,
        transport: Transport | None = None,
        token_provider: Callable[[], str] | None = None,
    ) -> None:
        provider = token_provider or (lambda: token or os.environ.get("SITE_AGENT_TOKEN", ""))
        if not provider():
            raise ValueError("SITE_AGENT_TOKEN is required")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport or self._urllib_transport
        self._token_provider = provider

    @classmethod
    def from_env(cls, base_url: str | None = None) -> "SitePlatform":
        resolved = base_url or os.environ.get("SITE_AGENT_BASE_URL", DEFAULT_BASE_URL)
        token_file = os.environ.get("SITE_AGENT_TOKEN_FILE")
        if token_file:
            def provider() -> str:
                try:
                    return Path(token_file).read_text(encoding="utf-8").strip()
                except OSError:
                    return ""

            return cls(resolved, token_provider=provider)
        return cls(resolved)

    def _urllib_transport(
        self, method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        token = self._token_provider()
        if not token:
            raise SiteAPIError(0, "missing_token", "SITE_AGENT_TOKEN is empty")
        request_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})

        try:
            status, _, raw = self._transport(method, self.base_url + path, request_headers, data)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SiteAPIError(0, "network_error", str(exc)) from exc
        text = raw.decode("utf-8", errors="replace")
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {}
        if status >= 400:
            raise SiteAPIError(
                status,
                payload.get("code"),
                str(payload.get("message") or text[:200]),
            )
        if not isinstance(payload, dict):
            raise SiteAPIError(status, "invalid_response", "expected a JSON object")
        return payload

    def health(self) -> dict:
        return self._request("GET", "/health")

    def store(self) -> SiteStore:
        payload = self._request("GET", "/store")
        raw = payload.get("store") or {}
        if not raw:
            raise SiteAPIError(200, "missing_store", "store payload is empty")
        return SiteStore(
            store_id=str(raw.get("storeId", "")),
            slug=str(raw.get("slug", "")),
            name=str(raw.get("name", "")),
            country=raw.get("country"),
            currency=str(raw.get("currency", "USD")),
            timezone=raw.get("timezone"),
        )

    def products(
        self,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SiteProduct]:
        query = [f"limit={max(1, min(limit, 100))}", f"offset={max(0, offset)}"]
        if status:
            query.append(f"status={status}")
        payload = self._request("GET", "/products?" + "&".join(query))
        return [_product_from_api(item) for item in payload.get("items", [])]

    def all_products(self, status: str | None = None) -> list[SiteProduct]:
        items: list[SiteProduct] = []
        offset = 0
        while True:
            page = self.products(status=status, limit=100, offset=offset)
            items.extend(page)
            if len(page) < 100:
                break
            offset += len(page)
        return items

    def product(self, product_id: str) -> dict:
        product_id = _safe_id(product_id, "product_id")
        return self._request("GET", f"/products/{product_id}")

    def metrics(self, days: int = 30, product_id: str | None = None) -> list[dict]:
        if not 1 <= days <= 90:
            raise ValueError("days must be between 1 and 90")
        query = [f"days={days}"]
        if product_id:
            query.append(f"productId={product_id}")
        payload = self._request("GET", "/metrics?" + "&".join(query))
        return list(payload.get("items", []))

    def metrics_total(self, days: int, product_id: str) -> dict:
        """One product's totals over a rolling window, as plain numbers.

        The agent API counts pv / inquiries / orders but has no click counter, so
        ``clicks`` stays 0 rather than being invented from pv.
        """
        rows = [
            item
            for item in self.metrics(days=days, product_id=product_id)
            if str(item.get("productId") or "") == str(product_id)
        ]
        if not rows:
            return {}
        row = rows[0]
        return {
            "days": int(days),
            "impressions": int(row.get("pv") or 0),
            "inquiries": int(row.get("inquiries") or 0),
            "orders": int(row.get("orders") or 0),
            "units": int(row.get("units") or 0),
            "clicks": 0,
        }

    def window_totals(self, product_id: str, *, days_total: int, days_cut: int = 0) -> dict:
        """Totals for the slice between two rolling windows.

        The API only knows "last N days", never "day X to day Y". Subtracting
        the shorter read from the longer one leaves the slice in between, which
        is how a window that closed a few days ago is still measurable.
        """
        total = self.metrics_total(days_total, product_id)
        if not total:
            return {}
        if days_cut <= 0:
            return total
        older = self.metrics_total(days_cut, product_id)
        if not older:
            return {}
        older_views = int(older.get("impressions") or 0)
        if older_views <= 0 and int(total.get("impressions") or 0) > 0:
            # The short window is blank while the long one is not: all traffic
            # sits before the slice we want. Plain subtraction would hand the
            # whole period back as if it belonged to the window, which is worse
            # than admitting the window cannot be measured.
            return {}
        delta = {
            key: max(0, int(total.get(key) or 0) - int(older.get(key) or 0))
            for key in ("impressions", "inquiries", "orders", "units", "clicks")
        }
        delta["days"] = max(1, days_total - days_cut)
        return delta

    def versions(self, product_id: str, limit: int = 20) -> list[dict]:
        product_id = _safe_id(product_id, "product_id")
        payload = self._request("GET", f"/products/{product_id}/versions?limit={max(1, min(limit, 50))}")
        return list(payload.get("items", []))

    def changes(self, limit: int = 20, since: str | None = None) -> list[dict]:
        query = [f"limit={max(1, min(limit, 50))}"]
        if since:
            query.append(f"since={since}")
        payload = self._request("GET", "/changes?" + "&".join(query))
        return list(payload.get("items", []))

    def patch_variant(
        self,
        variant_id: str,
        price_cents: int | None = None,
        stock: int | None = None,
        reason: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        if price_cents is None and stock is None:
            raise ValueError("price_cents or stock is required")
        if price_cents is not None and (not isinstance(price_cents, int) or price_cents <= 0):
            raise ValueError("price_cents must be a positive integer")
        if stock is not None and (not isinstance(stock, int) or stock < 0):
            raise ValueError("stock must be a non-negative integer")
        variant_id = _safe_id(variant_id, "variant_id")

        body: dict[str, Any] = {}
        if price_cents is not None:
            body["priceCents"] = price_cents
        if stock is not None:
            body["stock"] = stock
        if reason:
            body["reason"] = reason

        path = f"/variants/{variant_id}"
        if dry_run:
            path += "?dryRun=true"
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("PATCH", path, body, headers)

    def patch_product(
        self,
        product_id: str,
        fields: dict[str, Any],
        reason: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        if not fields:
            raise ValueError("fields must not be empty")
        product_id = _safe_id(product_id, "product_id")
        body = dict(fields)
        if reason:
            body["reason"] = reason
        path = f"/products/{product_id}"
        if dry_run:
            path += "?dryRun=true"
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("PATCH", path, body, headers)

    def rollback(
        self,
        product_id: str,
        version_id: str,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        product_id = _safe_id(product_id, "product_id")
        version_id = _safe_id(version_id, "version_id")
        body: dict[str, Any] = {"versionId": version_id}
        if reason:
            body["reason"] = reason
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("POST", f"/products/{product_id}/rollback", body, headers)


def load_snapshot(path: Path | str) -> tuple[SiteStore, list[SiteProduct]]:
    """Load a snapshot produced by ``run.py site sync`` (snake_case JSON)."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_store = data.get("store") or {}
    store = SiteStore(
        store_id=str(raw_store.get("store_id", "")),
        slug=str(raw_store.get("slug", "")),
        name=str(raw_store.get("name", "")),
        country=raw_store.get("country"),
        currency=str(raw_store.get("currency", "USD")),
        timezone=raw_store.get("timezone"),
    )

    products: list[SiteProduct] = []
    for raw in data.get("products", []):
        variants = tuple(
            SiteVariant(
                variant_id=str(item["variant_id"]),
                sku_code=str(item.get("sku_code", "")),
                price_cents=int(item.get("price_cents", 0)),
                cost_cents=int(item["cost_cents"]) if item.get("cost_cents") is not None else None,
                stock=int(item.get("stock", 0)),
                attrs=item.get("attrs") or {},
            )
            for item in raw.get("variants", [])
        )
        metrics_raw = raw.get("metrics")
        metrics = SiteMetrics(**metrics_raw) if metrics_raw else None
        products.append(
            SiteProduct(
                product_id=str(raw["product_id"]),
                slug=str(raw.get("slug", "")),
                title_en=str(raw.get("title_en", "")),
                status=str(raw.get("status", "")),
                category_id=raw.get("category_id"),
                category_slug=raw.get("category_slug"),
                moq=int(raw.get("moq", 1)),
                lead_time_days=int(raw.get("lead_time_days", 15)),
                incoterm=str(raw.get("incoterm", "FOB")),
                variants=variants,
                metrics=metrics,
            )
        )
    return store, products
