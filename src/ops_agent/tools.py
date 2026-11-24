"""The nine tools exposed to the agents.

Every tool has a JSON-schema-like contract, a side-effect label and, for
write actions, an approval requirement. The registry validates arguments and
returns a uniform ToolResult so the agents never have to parse exceptions.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any
from zlib import crc32

from .approvals import ApprovalError, ApprovalService
from .catalog import CATEGORY_PROFILES
from .compliance import ComplianceChecker
from .config import DATA_DIR
from .ingest import IngestError, build_draft, read_supplier_csv, resolve_ingest_path
from .models import Market
from .platform import ListingRecord, MockPlatform
from .runtime.risk import LEVELS, infer_risk_level
from .simulator import (
    apply_change,
    category_label,
    expected_ctr,
    expected_cvr,
)
from .store_metrics import MARGIN_FLOOR, cost_floor_cents, entry_variant, stock_days

MARKETS = [market.value for market in Market]
HYPOTHESES = [
    "scene_image",
    "title_keywords",
    "detail_size_chart",
    "price_cut",
    "faster_shipping",
]
ASSET_STYLES = ["scene", "studio", "lifestyle"]


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    side_effect: str
    requires_approval: bool
    input_schema: dict
    returns: str
    # 空 = 按 rules 推导（见 runtime/risk.py）。显式填值只用于"默认规则说不清"的
    # 少数工具；即便填了，FORBIDDEN_TOOLS 里的名字仍然是 R3，覆盖不了。
    risk_level: str = ""

    def resolved_risk_level(self) -> str:
        """推导后的等级；注册时已经算过一次，这里的兜底是给手工造的 spec 用的。"""

        return self.risk_level or infer_risk_level(self.name, self.side_effect, self.requires_approval)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "side_effect": self.side_effect,
            "requires_approval": self.requires_approval,
            "input_schema": self.input_schema,
            "returns": self.returns,
            "risk_level": self.resolved_risk_level(),
        }


@dataclass
class ToolResult:
    ok: bool
    data: dict | None = None
    error: str | None = None
    code: str | None = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "code": self.code,
            "meta": self.meta,
        }


@dataclass
class ToolContext:
    """What a tool is allowed to read.

    The three legacy fields keep their names and order so every positional
    construction still works; the research fields are optional and default to
    empty. A research tool must never reach for a global file: whatever it can
    see is injected here, which is also what makes the 500-SKU case testable.
    """

    platform: MockPlatform | None = None
    approvals: ApprovalService | None = None
    compliance: ComplianceChecker | None = None
    products: list = field(default_factory=list)
    memory: Any = None
    benchmarks: dict = field(default_factory=dict)
    # Review text is product detail, not part of SiteProduct. It is injected
    # instead of fetched so an offline run never silently invents a review.
    reviews: dict = field(default_factory=dict)
    # Step 7：上新链路。``ingest_rows`` 是 file_parse 的产物，listing_draft 从里面
    # 取行；``ingest_root`` 只给测试换目录用，生产固定 data/ingest。
    ingest_rows: list = field(default_factory=list)
    ingest_root: Any = None


def _check_type(name: str, value, spec: dict) -> None:
    expected = spec.get("type")
    if expected == "string" and not isinstance(value, str):
        raise ToolError("invalid_arguments", f"{name} must be a string")
    if expected == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise ToolError("invalid_arguments", f"{name} must be a number")
    if expected == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        raise ToolError("invalid_arguments", f"{name} must be an integer")
    if expected == "boolean" and not isinstance(value, bool):
        raise ToolError("invalid_arguments", f"{name} must be a boolean")
    if expected == "array" and not isinstance(value, list):
        raise ToolError("invalid_arguments", f"{name} must be an array")
    if expected == "object" and not isinstance(value, dict):
        raise ToolError("invalid_arguments", f"{name} must be an object")
    if "enum" in spec and value not in spec["enum"]:
        raise ToolError("invalid_arguments", f"{name} must be one of {spec['enum']}")
    if expected in ("number", "integer"):
        if "minimum" in spec and value < spec["minimum"]:
            raise ToolError("invalid_arguments", f"{name} must be >= {spec['minimum']}")
        if "maximum" in spec and value > spec["maximum"]:
            raise ToolError("invalid_arguments", f"{name} must be <= {spec['maximum']}")
    if expected == "array":
        if "minItems" in spec and len(value) < spec["minItems"]:
            raise ToolError("invalid_arguments", f"{name} needs at least {spec['minItems']} items")
        if "maxItems" in spec and len(value) > spec["maxItems"]:
            raise ToolError("invalid_arguments", f"{name} allows at most {spec['maxItems']} items")


def validate_arguments(schema: dict, arguments: dict) -> None:
    if not isinstance(arguments, dict):
        raise ToolError("invalid_arguments", "arguments must be an object")
    properties = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in arguments:
            raise ToolError("invalid_arguments", f"missing required argument: {key}")
    if schema.get("additionalProperties") is False:
        unknown = set(arguments) - set(properties)
        if unknown:
            raise ToolError("invalid_arguments", f"unknown arguments: {sorted(unknown)}")
    for key, value in arguments.items():
        spec = properties.get(key)
        if spec:
            _check_type(key, value, spec)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, callable]] = {}

    def register(self, spec: ToolSpec, function) -> None:
        if spec.side_effect not in ("read", "write", "compute"):
            raise ValueError("side_effect must be read, write or compute")
        level = spec.resolved_risk_level()
        if level not in LEVELS:
            raise ValueError(f"{spec.name}: 非法风险等级 {level!r}")
        if level != spec.risk_level:
            # 冻结 dataclass：注册时把推导结果固化下来，之后 specs() 读到的就是
            # 这一份，避免"每次调用都重算、两处算出不同答案"。
            spec = replace(spec, risk_level=level)
        self._tools[spec.name] = (spec, function)

    def specs(self) -> list[dict]:
        return [spec.to_dict() for spec, _ in self._tools.values()]

    def call(self, name: str, arguments: dict, context: ToolContext) -> ToolResult:
        entry = self._tools.get(name)
        if entry is None:
            return ToolResult(False, code="unknown_tool", error=f"unknown tool: {name}")
        spec, function = entry
        try:
            validate_arguments(spec.input_schema, arguments)
            data = function(context, **arguments)
            return ToolResult(True, data=data, meta={"tool": name, "side_effect": spec.side_effect})
        except ToolError as exc:
            return ToolResult(False, code=exc.code, error=exc.message, meta={"tool": name})
        except ApprovalError as exc:
            return ToolResult(False, code="invalid_token", error=str(exc), meta={"tool": name})
        except Exception as exc:  # noqa: BLE001 - tool boundary must not leak
            return ToolResult(False, code="tool_error", error=str(exc), meta={"tool": name})


# ---------------------------------------------------------------------------
# read tools
# ---------------------------------------------------------------------------

COMPETITOR_REFERENCE: dict[str, list[dict]] = {
    "home-fragrance": [
        {"title": "Luxury Soy Candle", "price": 24.9, "rating": 4.6, "top_claim": "45-hour burn"},
        {"title": "Reed Diffuser Set", "price": 19.9, "rating": 4.4, "top_claim": "Alcohol-free base"},
        {"title": "Wax Melt Gift Box", "price": 16.5, "rating": 4.7, "top_claim": "6 scents"},
    ],
    "kitchen-storage": [
        {"title": "Bamboo Drawer Divider", "price": 21.0, "rating": 4.5, "top_claim": "Expandable"},
        {"title": "Spice Rack Organizer", "price": 18.9, "rating": 4.3, "top_claim": "12 jars"},
        {"title": "Under-Sink Caddy", "price": 23.5, "rating": 4.6, "top_claim": "Rust-proof"},
    ],
    "garden": [
        {"title": "Ceramic Planter Trio", "price": 28.0, "rating": 4.4, "top_claim": "Drainage hole"},
        {"title": "Self-Watering Pot", "price": 22.0, "rating": 4.2, "top_claim": "7-day reservoir"},
        {"title": "Garden Tool Set", "price": 31.0, "rating": 4.5, "top_claim": "Stainless steel"},
    ],
    "bedroom-storage": [
        {"title": "Cotton Rope Basket", "price": 26.0, "rating": 4.6, "top_claim": "Handwoven"},
        {"title": "Under-Bed Bag", "price": 17.9, "rating": 4.1, "top_claim": "Dust-proof"},
        {"title": "Closet Divider Set", "price": 20.5, "rating": 4.4, "top_claim": "6 pieces"},
    ],
    "lighting": [
        {"title": "Rattan Pendant Lamp", "price": 39.0, "rating": 4.5, "top_claim": "Handmade shade"},
        {"title": "LED Table Lamp", "price": 29.0, "rating": 4.3, "top_claim": "3 color modes"},
        {"title": "Wall Sconce", "price": 35.0, "rating": 4.4, "top_claim": "Easy install"},
    ],
    "pet": [
        {"title": "Pet Grooming Brush", "price": 14.9, "rating": 4.6, "top_claim": "Self-cleaning"},
        {"title": "Cat Tunnel Toy", "price": 12.9, "rating": 4.4, "top_claim": "Collapsible"},
        {"title": "Dog Feeding Mat", "price": 16.9, "rating": 4.5, "top_claim": "Non-slip"},
    ],
    "outdoor": [
        {"title": "Camping Lantern", "price": 34.0, "rating": 4.5, "top_claim": "Rechargeable"},
        {"title": "Folding Chair", "price": 42.0, "rating": 4.2, "top_claim": "Supports 120kg"},
        {"title": "Picnic Blanket", "price": 36.0, "rating": 4.4, "top_claim": "Water-resistant"},
    ],
}


def get_shop_snapshot(context: ToolContext, days: int = 7, sku_id: str | None = None) -> dict:
    if sku_id is not None and sku_id not in context.platform.skus:
        raise ToolError("unknown_sku", f"unknown SKU: {sku_id}")
    items = context.platform.snapshot(days=days, sku_id=sku_id)
    return {
        "as_of": datetime.now().date().isoformat(),
        "days": days,
        "items": items,
        "source": "own_store",
        "license": "own",
    }


def get_competitors(context: ToolContext, category: str, market: str, limit: int = 5) -> dict:
    if category not in CATEGORY_PROFILES:
        raise ToolError("unknown_category", f"unknown category: {category}")
    items = COMPETITOR_REFERENCE.get(category, [])[:limit]
    return {
        "category": category,
        "market": market,
        "items": items,
        "source": "authorized_reference",
        "license": "authorized",
        "note": "客户授权的第三方参考数据，不是页面抓取",
    }


def get_trends(context: ToolContext, keyword: str, market: str) -> dict:
    seed = crc32(keyword.encode("utf-8"))
    base = 58 + seed % 25
    points = [
        {"month": f"2026-{month:02d}", "index": base + (month * 7 + seed % 11) % 35}
        for month in range(5, 11)
    ]
    first_index = points[0]["index"]
    last_index = points[-1]["index"]
    if last_index > first_index:
        seasonality = "Q4 上升"
    elif last_index < first_index:
        seasonality = "Q4 回落"
    else:
        seasonality = "平稳"
    return {
        "keyword": keyword,
        "market": market,
        "points": points,
        "seasonality": seasonality,
        "source": "public_trend_index",
        "license": "public",
    }


def price_calc(context: ToolContext, sku_id: str, price: float) -> dict:
    sku = context.platform.skus.get(sku_id)
    if sku is None:
        raise ToolError("unknown_sku", f"unknown SKU: {sku_id}")
    if price <= 0:
        raise ToolError("invalid_price", "price must be positive")
    margin = (price - sku.cost) / price
    floor = 0.16
    band_low = round(sku.cost / (1 - 0.40), 2)
    band_high = round(sku.cost / (1 - 0.25), 2)
    return {
        "sku_id": sku_id,
        "price": round(price, 2),
        "cost": sku.cost,
        "margin": round(margin, 4),
        "margin_floor": floor,
        "below_floor": margin < floor,
        "band": [band_low, band_high],
        "within_band": band_low <= price <= band_high,
    }


# ---------------------------------------------------------------------------
# content tools
# ---------------------------------------------------------------------------

ANGLE_BULLETS = {
    "gift-ready": "Gift-ready packaging, no extra wrapping needed",
    "everyday storage": "Fits daily routines, easy to put away",
    "small-space": "Compact footprint for small apartments",
}


def _change_for_hypothesis(hypothesis: str) -> tuple[str, float | None]:
    if hypothesis == "price_cut":
        return "price_cut", 0.05
    return hypothesis, None


def generate_listing(
    context: ToolContext,
    sku_id: str,
    market: str,
    hypothesis: str,
    variants: int = 2,
) -> dict:
    sku = context.platform.skus.get(sku_id)
    if sku is None:
        raise ToolError("unknown_sku", f"unknown SKU: {sku_id}")

    change, value = _change_for_hypothesis(hypothesis)
    after = apply_change(sku, change, value)
    lift = (expected_ctr(after) - expected_ctr(sku)) / expected_ctr(sku)
    angles = ["gift-ready", "everyday storage", "small-space"][:variants]

    items: list[dict] = []
    for index, angle in enumerate(angles, start=1):
        title = f"{sku.title} · {angle} | {market}"
        bullets = [
            f"{category_label(sku)} designed for {market} homes",
            ANGLE_BULLETS[angle],
            "Ships from a local warehouse within 3 days",
            "30-day return window",
        ]
        description = (
            f"{sku.title} in a {angle} setup. "
            f"Built for {market} customers who want a practical, compact solution."
        )
        image_brief = (
            f"{angle} scene in a {market} home, natural light, "
            "no visible logos or trademarks, no faces"
        )
        items.append(
            {
                "variant_id": f"{sku_id}-V{index}",
                "title": title,
                "bullets": bullets,
                "description": description,
                "price": after.price,
                "image_brief": image_brief,
                "expected_lift": round(lift, 4),
                "hypothesis": hypothesis,
            }
        )

    return {
        "sku_id": sku_id,
        "market": market,
        "hypothesis": hypothesis,
        "variants": items,
        "expected_lift": round(lift, 4),
        "note": "rule-based stub; replaced by the LLM workshop in a later step",
    }


def generate_assets(
    context: ToolContext,
    sku_id: str,
    style: str,
    count: int = 3,
) -> dict:
    sku = context.platform.skus.get(sku_id)
    if sku is None:
        raise ToolError("unknown_sku", f"unknown SKU: {sku_id}")

    assets: list[dict] = []
    for index in range(1, count + 1):
        assets.append(
            {
                "asset_id": f"{sku_id}-IMG{index}",
                "kind": "image_brief",
                "brief": (
                    f"{style} shot of {sku.title}, {sku.market.value} home context, "
                    f"angle {index}, no visible logos or trademarks, no faces"
                ),
                "usage_rights": "ai_generated",
            }
        )
    assets.append(
        {
            "asset_id": f"{sku_id}-VID1",
            "kind": "video_script",
            "brief": "15s vertical video: problem, product in use, close-up, call to action",
            "usage_rights": "ai_generated",
        }
    )
    return {"sku_id": sku_id, "style": style, "assets": assets}


def compliance_check(
    context: ToolContext,
    text: str,
    image_brief: str,
    category: str,
    market: str,
) -> dict:
    report = context.compliance.check(text, image_brief, category, market)
    payload = report.to_dict()
    payload.update({"category": category, "market": market})
    return payload


# ---------------------------------------------------------------------------
# write tools
# ---------------------------------------------------------------------------

APPROVAL_PRICE_THRESHOLD = 0.10


def publish_listing(
    context: ToolContext,
    sku_id: str,
    variant: dict,
    approval_token: str | None = None,
) -> dict:
    sku = context.platform.skus.get(sku_id)
    if sku is None:
        raise ToolError("unknown_sku", f"unknown SKU: {sku_id}")

    title = str(variant.get("title", "")).strip()
    bullets = variant.get("bullets") or []
    description = str(variant.get("description", "")).strip()
    image_brief = str(variant.get("image_brief", "")).strip()
    price = float(variant.get("price", 0))

    if not title or not bullets or not description or price <= 0:
        raise ToolError("invalid_variant", "variant needs title, bullets, description and price")

    text = " ".join([title, *bullets, description])
    report = context.compliance.check(text, image_brief, sku.category, sku.market.value)
    if not report.passed:
        raise ToolError(
            "compliance_failed",
            json.dumps(report.to_dict(), ensure_ascii=False),
        )

    current = context.platform.current(sku_id)
    price_change = abs(price - current.price) / current.price if current.price else 0.0
    high_risk = price_change > APPROVAL_PRICE_THRESHOLD

    if high_risk and not approval_token:
        raise ToolError(
            "approval_required",
            f"price change {price_change:.1%} exceeds {APPROVAL_PRICE_THRESHOLD:.0%}, human approval required",
        )
    approved_by = "auto"
    if approval_token:
        context.approvals.consume(approval_token, sku_id, "publish")
        approved_by = "human"

    version = current.version + 1
    record = ListingRecord(
        version=version,
        sku_id=sku_id,
        title=title,
        bullets=[str(item) for item in bullets],
        description=description,
        price=round(price, 2),
        image_brief=image_brief,
        published_at=datetime.now(),
        status="published",
        approval_token=approval_token,
    )
    context.platform.publish(sku_id, record)

    return {
        "sku_id": sku_id,
        "listing_id": f"{sku_id}-v{version}",
        "version": version,
        "status": "published",
        "price": record.price,
        "requires_approval": high_risk,
        "approved_by": approved_by,
    }


def rollback(context: ToolContext, sku_id: str, approval_token: str | None = None) -> dict:
    if sku_id not in context.platform.skus:
        raise ToolError("unknown_sku", f"unknown SKU: {sku_id}")
    try:
        record = context.platform.rollback(sku_id)
    except ValueError as exc:
        raise ToolError("nothing_to_rollback", str(exc)) from exc
    return {
        "sku_id": sku_id,
        "restored_version": record.version,
        "title": record.title,
        "price": record.price,
        "status": "rolled_back",
    }


# ---------------------------------------------------------------------------
# research tools (read-only, for the tool-using director)
#
# These are registered in their own registry. ``build_registry()`` must keep
# exposing exactly the nine legacy tools: the workshop and the publish path
# still drive them, and a test pins the count.
# ---------------------------------------------------------------------------

MAX_VARIANTS_PER_PRODUCT = 6
MAX_BULLETS_PER_LISTING = 4
MAX_DESCRIPTION_CHARS = 300
MAX_BULLET_CHARS = 100
MAX_REVIEWS_PER_CALL = 20
MAX_REVIEW_CHARS = 200
MAX_LISTING_TITLE_CHARS = 120
MAX_MEMORY_MATCHES = 20
MAX_MEMORY_TEXT_CHARS = 220
MEMORY_KINDS = ("lesson", "decision", "experiment")


def _research_products(context: ToolContext) -> list:
    return list(getattr(context, "products", None) or [])


def _find_product(context: ToolContext, product_id: str):
    wanted = str(product_id or "")
    for product in _research_products(context):
        if str(getattr(product, "product_id", "")) == wanted:
            return product
    raise ToolError("unknown_product", f"unknown product: {wanted}")


def _margin(entry) -> float | None:
    value = getattr(entry, "margin", None)
    return round(value, 4) if value is not None else None


def _safe_days(value) -> float | None:
    """Stock cover can be infinite; JSON has no infinity."""
    if value is None:
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number, 1)


# ---------------------------------------------------------------------------
# catalogue projection: one row shape, two consumers (seed + list_products)
# ---------------------------------------------------------------------------

# A row is a pointer, not a dossier. These six fields are what decides
# "should I open this one"; everything deeper is a tool call away.
SEED_ROW_FIELDS = ("id", "title", "category", "pv", "margin_pct", "flags")
SEED_PAGE_ROWS = 20
SEED_TITLE_CHARS = 20
# Every category costs ~85 characters in the seed. A store with twenty of them
# would blow the budget, so the tail is folded into one summary row. Six named
# categories is already more than the agent can act on in one run.
MAX_SEED_CATEGORIES = 6
LIST_LIMIT_DEFAULT = 10
LIST_LIMIT_MAX = 20
# A page is shaped to stay under this, so the ledger's 1500-char backstop never
# has to cut a row in half. Fewer rows and a next_offset beat a truncated blob.
LIST_CHARS_BUDGET = 1200
LIST_SORTS = ("pv_asc", "pv_desc", "order_rate_asc", "stock_days_desc", "margin_asc")
LIST_FLAGS = ("dead_stock", "below_margin_floor", "out_of_stock", "no_metrics")


def product_flags(product) -> list[str]:
    """Facts, not judgements: they say what is odd, not what to do about it."""
    metrics = getattr(product, "metrics", None)
    if metrics is None:
        return ["no_metrics"]
    flags: list[str] = []
    entry = entry_variant(product)
    if metrics.units <= 0 and product.stock_total > 0:
        flags.append("dead_stock")
    if entry is not None and entry.margin is not None and entry.margin < MARGIN_FLOOR:
        flags.append("below_margin_floor")
    if product.stock_total <= 0:
        flags.append("out_of_stock")
    return flags


def catalogue_row(product, days: int = 30) -> dict:
    """The named form. Both the seed and list_products derive from this one."""
    metrics = getattr(product, "metrics", None)
    entry = entry_variant(product)
    margin = entry.margin if entry is not None else None
    cover = _safe_days(stock_days(product, days)) if metrics else None
    row = {
        "id": str(product.product_id),
        "title": str(getattr(product, "title_en", ""))[:SEED_TITLE_CHARS],
        "category": str(getattr(product, "category_slug", "") or ""),
        "pv": int(metrics.pv) if metrics else 0,
        "inquiry_rate_pct": round(metrics.inquiry_rate * 100, 2) if metrics else 0.0,
        "order_rate_pct": round(metrics.order_rate * 100, 2) if metrics else 0.0,
        "margin_pct": round(margin * 100, 1) if margin is not None else None,
        "stock_days": int(cover) if cover is not None else None,
    }
    # An empty list on every healthy row is pure token cost.
    flags = product_flags(product)
    if flags:
        row["flags"] = flags
    return row


def _row_value(row: dict, name: str):
    if name == "flags":
        return list(row.get("flags") or [])
    return row.get(name)


def seed_row(product, days: int = 30) -> list:
    """The published row: a list, because twenty named objects do not fit."""
    row = catalogue_row(product, days)
    return [_row_value(row, name) for name in SEED_ROW_FIELDS]


def category_summary(products: list, max_categories: int = MAX_SEED_CATEGORIES) -> list[dict]:
    """Per-category counts and the same median ``get_benchmarks`` would return.

    Categories past ``max_categories`` are folded into one row without a median:
    the count still tells the agent where the mass is, and a missing median says
    "not computed" rather than pretending to be one.
    """
    document = benchmark_doc(products)
    grouped: dict[str, dict] = {}
    for product in products:
        name = str(getattr(product, "category_slug", "") or "_")
        bucket = grouped.setdefault(
            name, {"name": name, "count": 0, "dead_stock": 0, "below_floor": 0}
        )
        bucket["count"] += 1
        flags = product_flags(product)
        if "dead_stock" in flags:
            bucket["dead_stock"] += 1
        if "below_margin_floor" in flags:
            bucket["below_floor"] += 1
    for name, bucket in grouped.items():
        entry = (document.get("by_category") or {}).get(name) or {}
        # A thin category already borrows the store median inside benchmark_doc;
        # reading it from there keeps the seed and the tool in agreement.
        bucket["median_pv"] = int(round(float(entry.get("pv") or 0)))
    ordered = sorted(grouped.values(), key=lambda item: (-item["count"], item["name"]))
    limit = max(1, int(max_categories))
    if len(ordered) <= limit:
        return ordered
    head = ordered[: limit - 1]
    tail = ordered[limit - 1 :]
    head.append(
        {
            "name": f"+{len(tail)} more categories",
            "count": sum(item["count"] for item in tail),
            "dead_stock": sum(item["dead_stock"] for item in tail),
            "below_floor": sum(item["below_floor"] for item in tail),
        }
    )
    return head


def _sort_value(row: dict, sort: str):
    """A single named metric, never a blend. Ties fall back to id for stability."""
    if sort == "pv_asc":
        return (0, row["pv"])
    if sort == "pv_desc":
        return (0, -row["pv"])
    if sort == "order_rate_asc":
        return (0, row["order_rate_pct"])
    if sort == "stock_days_desc":
        cover = row.get("stock_days")
        return (1, 0) if cover is None else (0, -cover)
    if sort == "margin_asc":
        margin = row.get("margin_pct")
        return (1, 0) if margin is None else (0, margin)
    raise ToolError("bad_sort", f"sort must be one of {list(LIST_SORTS)}")


def list_products(
    context: ToolContext,
    category: str | None = None,
    flag: str | None = None,
    sort: str = "pv_asc",
    limit: int = LIST_LIMIT_DEFAULT,
    offset: int = 0,
) -> dict:
    """分页读取商品目录：按单一指标排序，没有"机会分"这种综合排序。"""
    if sort not in LIST_SORTS:
        raise ToolError("bad_sort", f"sort must be one of {list(LIST_SORTS)}")
    if flag is not None and flag not in LIST_FLAGS:
        raise ToolError("bad_flag", f"flag must be one of {list(LIST_FLAGS)}")
    rows = [catalogue_row(product) for product in _research_products(context)]
    if category:
        rows = [row for row in rows if row["category"] == str(category)]
    if flag:
        rows = [row for row in rows if flag in (row.get("flags") or [])]
    rows.sort(key=lambda row: (_sort_value(row, sort), row["id"]))
    total = len(rows)
    start = max(0, int(offset if offset is not None else 0))
    # Clamp, do not fall back: limit=0 means "as few as allowed", and `or` would
    # quietly turn that into the default page size.
    raw_limit = LIST_LIMIT_DEFAULT if limit is None else int(limit)
    want = max(1, min(raw_limit, LIST_LIMIT_MAX))
    page: list[list] = []
    for row in rows[start : start + want]:
        candidate = page + [[_row_value(row, name) for name in SEED_ROW_FIELDS]]
        if page and len(
            json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        ) > LIST_CHARS_BUDGET:
            break
        page = candidate
    result: dict = {
        "total": total,
        "returned": len(page),
        "fields": list(SEED_ROW_FIELDS),
        "rows": page,
    }
    if start + len(page) < total:
        result["next_offset"] = start + len(page)
    return result


def benchmark_doc(products: list) -> dict:
    """Per-category medians plus the sample size behind each one.

    Shared by ``build_seed`` and ``get_benchmarks`` so the seed and the tool can
    never disagree about what "the category median" is. A category with fewer
    than three measured products keeps the store-wide median and says so via
    ``scope``, which is what makes ``trustworthy`` mean something.
    """

    def row(items: list) -> dict:
        with_metrics = [item for item in items if getattr(item, "metrics", None)]
        if not with_metrics:
            return {
                "pv": 0.0,
                "inquiry_rate_pct": 0.0,
                "order_rate_pct": 0.0,
                "margin_pct": None,
                "sample_size": 0,
            }
        pv = sorted(item.metrics.pv for item in with_metrics)
        inquiry = sorted(item.metrics.inquiry_rate for item in with_metrics)
        orders = sorted(item.metrics.order_rate for item in with_metrics)
        margins = sorted(
            variant.margin
            for item in with_metrics
            for variant in getattr(item, "variants", ())
            if getattr(variant, "margin", None) is not None
        )

        def middle(values: list, scale: float = 1.0):
            if not values:
                return None
            index = len(values) // 2
            if len(values) % 2:
                return round(values[index] * scale, 2)
            return round((values[index - 1] + values[index]) / 2 * scale, 2)

        return {
            "pv": middle(pv) or 0.0,
            "inquiry_rate_pct": middle(inquiry, 100) or 0.0,
            "order_rate_pct": middle(orders, 100) or 0.0,
            "margin_pct": middle(margins, 100),
            "sample_size": len(with_metrics),
        }

    store_row = row(products)
    grouped: dict[str, list] = {}
    for product in products:
        if not getattr(product, "metrics", None):
            continue
        key = str(getattr(product, "category_slug", "") or "_")
        grouped.setdefault(key, []).append(product)

    by_category: dict[str, dict] = {}
    for category, items in grouped.items():
        entry = row(items)
        if entry["sample_size"] >= 3:
            entry["scope"] = "category"
        else:
            # Thin category: borrow the store median, but never pretend it is
            # the category's own baseline. ``sample_size`` stays the real one.
            entry = dict(store_row)
            entry["sample_size"] = len([item for item in items if getattr(item, "metrics", None)])
            entry["scope"] = "store"
        by_category[category] = entry
    return {"all": store_row, "by_category": by_category}


def _benchmarks(context: ToolContext) -> dict:
    """Injected when available; otherwise derived from the injected products."""
    document = getattr(context, "benchmarks", None)
    if isinstance(document, dict) and document.get("by_category"):
        return document
    return benchmark_doc(_research_products(context))


def _current_listing(context: ToolContext, product) -> dict:
    """Current copy, from the platform when it has one, never invented."""
    platform = getattr(context, "platform", None)
    record = None
    if platform is not None and hasattr(platform, "current"):
        try:
            record = platform.current(product.product_id)
        except (KeyError, IndexError, AttributeError):
            record = None
    if record is not None:
        bullets = [str(item)[:MAX_BULLET_CHARS] for item in list(record.bullets or [])[:MAX_BULLETS_PER_LISTING]]
        return {
            "title": str(record.title)[:MAX_LISTING_TITLE_CHARS],
            "bullets": bullets,
            "description": str(record.description)[:MAX_DESCRIPTION_CHARS],
            "version": getattr(record, "version", None),
            "source": "platform",
        }
    entry = entry_variant(product)
    return {
        "title": str(product.title_en)[:MAX_LISTING_TITLE_CHARS],
        "bullets": [],
        "description": "",
        "version": None,
        "price_cents": entry.price_cents if entry else 0,
        "source": "site_catalog",
        "note": "站点快照不含刊登正文，bullets/description 为空",
    }


def get_product(context: ToolContext, product_id: str) -> dict:
    product = _find_product(context, product_id)
    variants = []
    for variant in list(getattr(product, "variants", ()) or [])[:MAX_VARIANTS_PER_PRODUCT]:
        variants.append(
            {
                "variant_id": str(variant.variant_id),
                "sku_code": str(getattr(variant, "sku_code", "")),
                "price_cents": int(getattr(variant, "price_cents", 0) or 0),
                "cost_cents": getattr(variant, "cost_cents", None),
                "stock": int(getattr(variant, "stock", 0) or 0),
                "margin_pct": round(variant.margin * 100, 1)
                if getattr(variant, "margin", None) is not None
                else None,
            }
        )
    return {
        "product_id": str(product.product_id),
        "title": str(getattr(product, "title_en", ""))[:MAX_LISTING_TITLE_CHARS],
        "category": str(getattr(product, "category_slug", "") or ""),
        "status": str(getattr(product, "status", "") or ""),
        "moq": getattr(product, "moq", None),
        "lead_time_days": getattr(product, "lead_time_days", None),
        "incoterm": getattr(product, "incoterm", ""),
        "variants": variants,
        "current": _current_listing(context, product),
    }


def get_product_metrics(context: ToolContext, product_id: str, days: int = 30) -> dict:
    product = _find_product(context, product_id)
    metrics = getattr(product, "metrics", None)
    if metrics is None:
        return {
            "product_id": str(product.product_id),
            "window_days": days,
            "available": False,
            "note": "该商品没有 30 天指标，无法与类目比较",
        }
    entry = entry_variant(product)
    category = str(getattr(product, "category_slug", "") or "")
    benches = _benchmarks(context)
    bench = (benches.get("by_category") or {}).get(category) or benches.get("all") or {}
    notes = []
    if int(days) != 30:
        notes.append("仅 30 天聚合可用，按 30 天口径返回")
    notes.append("数据源无退货字段")
    margin = _margin(entry)
    return {
        "product_id": str(product.product_id),
        "window_days": 30,
        "available": True,
        "pv": int(metrics.pv),
        "inquiries": int(metrics.inquiries),
        "orders": int(metrics.orders),
        "returns": None,
        "inquiry_rate_pct": round(metrics.inquiry_rate * 100, 2),
        "order_rate_pct": round(metrics.order_rate * 100, 2),
        "margin_pct": round(margin * 100, 1) if margin is not None else None,
        "stock_days": _safe_days(stock_days(product, 30)),
        "vs": {
            "scope": bench.get("scope", "store"),
            "sample_size": bench.get("sample_size", 0),
            "median": {
                "pv": bench.get("pv"),
                "inquiry_rate_pct": bench.get("inquiry_rate_pct"),
                "order_rate_pct": bench.get("order_rate_pct"),
                "margin_pct": bench.get("margin_pct"),
            },
        },
        "note": "；".join(notes),
    }


def get_benchmarks_tool(context: ToolContext, category: str) -> dict:
    benches = _benchmarks(context)
    known = benches.get("by_category") or {}
    bench = known.get(category)
    if bench is None:
        store_row = benches.get("all") or {}
        return {
            "category": str(category),
            "scope": "store",
            "sample_size": store_row.get("sample_size", 0),
            "trustworthy": False,
            "median": {
                "pv": store_row.get("pv"),
                "inquiry_rate_pct": store_row.get("inquiry_rate_pct"),
                "order_rate_pct": store_row.get("order_rate_pct"),
                "margin_pct": store_row.get("margin_pct"),
            },
            "note": "未知类目，回退到全店中位数；不要把它当作该类目的基准",
        }
    trustworthy = str(bench.get("scope")) == "category" and int(bench.get("sample_size") or 0) >= 3
    note = "" if trustworthy else "该类目样本 < 3，中位数借自全店；换用全店基准或放弃比较"
    return {
        "category": str(category),
        "scope": bench.get("scope", "store"),
        "sample_size": bench.get("sample_size", 0),
        "trustworthy": trustworthy,
        "median": {
            "pv": bench.get("pv"),
            "inquiry_rate_pct": bench.get("inquiry_rate_pct"),
            "order_rate_pct": bench.get("order_rate_pct"),
            "margin_pct": bench.get("margin_pct"),
        },
        "note": note,
    }


def get_product_reviews(context: ToolContext, product_id: str, limit: int = 5) -> dict:
    product = _find_product(context, product_id)
    limit = max(1, min(MAX_REVIEWS_PER_CALL, int(limit or 5)))
    source = getattr(context, "reviews", None) or {}
    raw = source.get(str(product.product_id)) if isinstance(source, dict) else None
    if raw is None:
        raw = getattr(product, "reviews", None)
    items = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        rating = item.get("rating")
        items.append({"rating": rating, "text": text[:MAX_REVIEW_CHARS]})
    # Negative first: a 5-star "love it" is not a reason to change a listing.
    items.sort(key=lambda item: (float(item["rating"]) if isinstance(item["rating"], (int, float)) else 99))
    if not items:
        return {
            "product_id": str(product.product_id),
            "reviews": [],
            "note": "没有评价正文可用（离线快照不含评价），不要引用或编造评价",
        }
    return {"product_id": str(product.product_id), "reviews": items[:limit], "count": len(items)}


def price_calc_cents(context: ToolContext, product_id: str, price_cents: int) -> dict:
    product = _find_product(context, product_id)
    entry = entry_variant(product)
    if entry is None:
        raise ToolError("unknown_product", f"product has no variant: {product_id}")
    price = int(price_cents)
    cost = getattr(entry, "cost_cents", None)
    floor_cents = cost_floor_cents(entry, MARGIN_FLOOR)
    if not cost:
        return {
            "product_id": str(product.product_id),
            "price_cents": price,
            "cost_cents": None,
            "margin_pct": None,
            "cost_floor_cents": 0,
            # Unknown is not "safe". True here would read as an approval.
            "within_floor": None,
            "note": "成本未知，无法判断毛利底线",
        }
    margin = (price - cost) / price if price > 0 else 0.0
    # A band, not a pair of unrelated quotes: 25% gross margin is the floor of
    # the band and 40% is its ceiling. Dividing by (1 - 0.40) first is how the
    # legacy tool ended up printing a "low" above its "high".
    band_low = math.ceil(cost / (1 - 0.25))
    band_high = math.ceil(cost / (1 - 0.40))
    return {
        "product_id": str(product.product_id),
        "price_cents": price,
        "cost_cents": int(cost),
        "margin_pct": round(margin * 100, 1),
        "cost_floor_cents": floor_cents,
        "band_low_cents": band_low,
        "band_high_cents": band_high,
        "within_floor": price >= floor_cents,
        "within_band": band_low <= price <= band_high,
        "current_price_cents": int(entry.price_cents),
    }


def search_memory(context: ToolContext, query: str, kinds: list | None = None, limit: int = 5) -> dict:
    """Case-insensitive substring search over memory; no embeddings, on purpose."""
    wanted = [str(item) for item in (kinds or ["lesson"])]
    unknown = [item for item in wanted if item not in MEMORY_KINDS]
    if unknown:
        raise ToolError("invalid_arguments", f"unknown memory kind: {unknown[0]}")
    limit = max(1, min(MAX_MEMORY_MATCHES, int(limit or 5)))
    memory = getattr(context, "memory", None)
    if memory is None:
        return {"query": str(query), "matches": [], "count": 0, "note": "本次运行未启用记忆"}

    haystack: list[tuple[str, str, dict]] = []
    if "lesson" in wanted:
        for item in memory.read_lessons() or []:
            haystack.append(("lesson", str(item.get("lesson") or item.get("text") or item), item))
    if "decision" in wanted:
        for item in memory.read_decisions(limit=50) or []:
            haystack.append(("decision", str(item.get("reason") or json.dumps(item, ensure_ascii=False)), item))
    if "experiment" in wanted:
        for item in memory.read_experiments(limit=50, include_open=True) or []:
            haystack.append(
                (
                    "experiment",
                    str(item.get("hypothesis") or item.get("verdict") or item.get("product_id") or ""),
                    item,
                )
            )

    needle = str(query or "").strip().lower()
    matches = []
    for kind, text, item in haystack:
        if needle and needle not in text.lower():
            continue
        matches.append(
            {
                "kind": kind,
                "ref": str(
                    item.get("lesson_id")
                    or item.get("decision_id")
                    or item.get("experiment_id")
                    or ""
                )[:40],
                "text": text[:MAX_MEMORY_TEXT_CHARS],
            }
        )
        if len(matches) >= limit:
            break
    return {
        "query": str(query),
        "count": len(matches),
        "matches": matches,
        "note": "记忆是上下文，不是本次观察；不能作为 evidence_refs",
    }


# ---------------------------------------------------------------------------
# ingest tools (Step 7): 供应商资料 → Listing 草稿
#
# 和 research 一样单独一个 registry：``build_registry()`` 的九个工具是工坊的
# 既有契约、有测试写死名单，上新工具既不该混进去，也不该被任何既有 Agent 看见。
# ---------------------------------------------------------------------------

MAX_INGEST_FACT_CHARS = 200


def _ingest_root(context: ToolContext):
    return getattr(context, "ingest_root", None) or (DATA_DIR / "ingest")


def file_parse(context: ToolContext, path: str, kind: str = "csv") -> dict:
    """解析 data/ingest 下的供应商资料，只读。

    路径策略在 ``ingest.resolve_ingest_path``：**绝对路径、``..``、符号链接越界**
    三种都拒。这里不自己拼路径，避免两处判断漂移。
    """

    if str(kind or "").strip().lower() != "csv":
        raise ToolError("bad_kind", f"kind 只支持 csv：{kind!r}")
    try:
        target, relative = resolve_ingest_path(path, _ingest_root(context))
        parsed = read_supplier_csv(target)
    except IngestError as exc:
        raise ToolError(exc.code, exc.message) from exc

    # 解析结果留在内存里给 listing_draft 用，不落盘、不进 ledger。
    context.ingest_rows = list(parsed.rows)
    return {
        "path": relative,
        "kind": "csv",
        "count": len(parsed.rows),
        "columns": list(parsed.columns),
        "ignored_columns": list(parsed.ignored_columns),
        "rows": list(parsed.rows),
        "notes": list(parsed.notes),
    }


def listing_draft(
    context: ToolContext,
    sku: str,
    market: str,
    language: str = "en",
    facts: list | None = None,
) -> dict:
    """按供应商资料生成**一条** Listing 草稿。产出对象不落库、不发布。

    ``side_effect="write" + requires_approval=True`` 是刻意的：草稿一旦被采纳就是
    一次写入，所以它按 R1 报级、需要人工确认；真正的写入路径不在这里。
    """

    rows = list(getattr(context, "ingest_rows", None) or [])
    if not rows:
        raise ToolError("no_ingest_rows", "还没有供应商资料：先调用 file_parse")
    wanted = str(sku or "").strip().casefold()
    row = next(
        (item for item in rows if str(item.get("sku") or "").strip().casefold() == wanted),
        None,
    )
    if row is None:
        raise ToolError("unknown_sku", f"供应商资料里没有 {sku}")
    for fact in facts or []:
        if len(str(fact)) > MAX_INGEST_FACT_CHARS:
            raise ToolError("invalid_arguments", f"单条 fact 不得超过 {MAX_INGEST_FACT_CHARS} 字")

    draft = build_draft(
        row,
        market=market,
        language=language,
        facts=facts,
        checker=getattr(context, "compliance", None),
    )
    return draft.to_dict()


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def build_registry() -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(
        ToolSpec(
            name="get_shop_snapshot",
            description="读取自家店铺最近 N 天的曝光、点击、转化、退货与毛利",
            side_effect="read",
            requires_approval=False,
            input_schema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "minimum": 1, "maximum": 90},
                    "sku_id": {"type": "string"},
                },
                "required": [],
                "additionalProperties": False,
            },
            returns="items[]: sku_id, ctr, cvr, return_rate, margin, price",
        ),
        get_shop_snapshot,
    )

    registry.register(
        ToolSpec(
            name="get_competitors",
            description="读取客户授权的同类目参考数据（非抓取）",
            side_effect="read",
            requires_approval=False,
            input_schema={
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "market": {"type": "string", "enum": MARKETS},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                "required": ["category", "market"],
                "additionalProperties": False,
            },
            returns="items[]: title, price, rating, top_claim; source=authorized_reference",
        ),
        get_competitors,
    )

    registry.register(
        ToolSpec(
            name="get_trends",
            description="读取公开趋势指数，用于判断季节性",
            side_effect="read",
            requires_approval=False,
            input_schema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "market": {"type": "string", "enum": MARKETS},
                },
                "required": ["keyword", "market"],
                "additionalProperties": False,
            },
            returns="points[]: month, index; source=public_trend_index",
        ),
        get_trends,
    )

    registry.register(
        ToolSpec(
            name="price_calc",
            description="计算毛利、价格带与毛利底线",
            side_effect="read",
            requires_approval=False,
            input_schema={
                "type": "object",
                "properties": {
                    "sku_id": {"type": "string"},
                    "price": {"type": "number", "minimum": 0.01},
                },
                "required": ["sku_id", "price"],
                "additionalProperties": False,
            },
            returns="price, margin, margin_floor, below_floor, band, within_band",
        ),
        price_calc,
    )

    registry.register(
        ToolSpec(
            name="generate_listing",
            description="按假设生成 2-3 版多语种 Listing 草稿（当前为规则 stub）",
            side_effect="compute",
            requires_approval=False,
            input_schema={
                "type": "object",
                "properties": {
                    "sku_id": {"type": "string"},
                    "market": {"type": "string", "enum": MARKETS},
                    "hypothesis": {"type": "string", "enum": HYPOTHESES},
                    "variants": {"type": "integer", "minimum": 2, "maximum": 3},
                },
                "required": ["sku_id", "market", "hypothesis"],
                "additionalProperties": False,
            },
            returns="variants[]: title, bullets, description, price, image_brief, expected_lift",
        ),
        generate_listing,
    )

    registry.register(
        ToolSpec(
            name="generate_assets",
            description="生成图片 brief 与短视频脚本，标注 AI 生成版权",
            side_effect="compute",
            requires_approval=False,
            input_schema={
                "type": "object",
                "properties": {
                    "sku_id": {"type": "string"},
                    "style": {"type": "string", "enum": ASSET_STYLES},
                    "count": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                "required": ["sku_id", "style"],
                "additionalProperties": False,
            },
            returns="assets[]: asset_id, kind, brief, usage_rights",
        ),
        generate_assets,
    )

    registry.register(
        ToolSpec(
            name="compliance_check",
            description="检查禁售、广告法、品牌词、版权四类风险",
            side_effect="read",
            requires_approval=False,
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "image_brief": {"type": "string"},
                    "category": {"type": "string"},
                    "market": {"type": "string", "enum": MARKETS},
                },
                "required": ["text", "image_brief", "category", "market"],
                "additionalProperties": False,
            },
            returns="passed, violations[], lexicon_version",
        ),
        compliance_check,
    )

    registry.register(
        ToolSpec(
            name="publish_listing",
            description="发布 Listing；价格变动超过 10% 时必须提供人工审批令牌",
            side_effect="write",
            requires_approval=True,
            input_schema={
                "type": "object",
                "properties": {
                    "sku_id": {"type": "string"},
                    "variant": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "bullets": {"type": "array", "minItems": 1},
                            "description": {"type": "string"},
                            "price": {"type": "number"},
                            "image_brief": {"type": "string"},
                        },
                        "required": ["title", "bullets", "description", "price"],
                    },
                    "approval_token": {"type": "string"},
                },
                "required": ["sku_id", "variant"],
                "additionalProperties": False,
            },
            returns="listing_id, version, status, requires_approval, approved_by",
        ),
        publish_listing,
    )

    registry.register(
        ToolSpec(
            name="rollback",
            description="回滚到上一个已发布版本",
            side_effect="write",
            requires_approval=False,
            input_schema={
                "type": "object",
                "properties": {
                    "sku_id": {"type": "string"},
                    "approval_token": {"type": "string"},
                },
                "required": ["sku_id"],
                "additionalProperties": False,
            },
            returns="restored_version, title, price, status",
        ),
        rollback,
    )

    return registry


def build_research_registry() -> ToolRegistry:
    """Seven read-only tools for the tool-using director.

    Deliberately separate from ``build_registry()``: that one is the workshop's
    contract and a test pins it at nine tools. Nothing here can write, and the
    gateway filters by side effect anyway, so a model cannot talk its way into
    ``publish_listing`` through this registry — it does not exist here.
    """
    registry = ToolRegistry()

    registry.register(
        ToolSpec(
            name="get_product",
            description="读取一个商品的完整资料、变体价格/成本/库存与当前刊登文案",
            side_effect="read",
            requires_approval=False,
            input_schema={
                "type": "object",
                "properties": {"product_id": {"type": "string"}},
                "required": ["product_id"],
                "additionalProperties": False,
            },
            returns="product_id, title, category, variants[], current{title,bullets,description}",
        ),
        get_product,
    )

    registry.register(
        ToolSpec(
            name="get_metrics",
            description="读取一个商品近 30 天的流量、询盘、订单、毛利、库存天数，并给出类目中位数对比",
            side_effect="read",
            requires_approval=False,
            input_schema={
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "days": {"type": "integer", "minimum": 7, "maximum": 90},
                },
                "required": ["product_id"],
                "additionalProperties": False,
            },
            returns="pv, inquiries, orders, inquiry_rate_pct, order_rate_pct, margin_pct, stock_days, vs.median",
        ),
        get_product_metrics,
    )

    registry.register(
        ToolSpec(
            name="get_benchmarks",
            description="读取一个类目的中位数与样本量；样本少于 3 时 trustworthy=false",
            side_effect="read",
            requires_approval=False,
            input_schema={
                "type": "object",
                "properties": {"category": {"type": "string"}},
                "required": ["category"],
                "additionalProperties": False,
            },
            returns="category, median{pv,inquiry_rate_pct,order_rate_pct,margin_pct}, sample_size, trustworthy",
        ),
        get_benchmarks_tool,
    )

    registry.register(
        ToolSpec(
            name="get_reviews",
            description="读取一个商品的评价原文，差评优先；没有正文时返回空数组而不是编造",
            side_effect="read",
            requires_approval=False,
            input_schema={
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["product_id"],
                "additionalProperties": False,
            },
            returns="reviews[]: rating, text; 无数据时 reviews=[] 并附 note",
        ),
        get_product_reviews,
    )

    registry.register(
        ToolSpec(
            name="price_calc",
            description="按整数分计算毛利率、成本底线与价格带",
            side_effect="read",
            requires_approval=False,
            input_schema={
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "price_cents": {"type": "integer", "minimum": 0},
                },
                "required": ["product_id", "price_cents"],
                "additionalProperties": False,
            },
            returns="margin_pct, cost_floor_cents, band_low_cents, band_high_cents, within_floor",
        ),
        price_calc_cents,
    )

    registry.register(
        ToolSpec(
            name="search_memory",
            description="在本店记忆里做大小写不敏感的子串检索（经验/决策/实验），结果不算证据",
            side_effect="read",
            requires_approval=False,
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(MEMORY_KINDS)},
                        "minItems": 1,
                        "maxItems": 3,
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            returns="matches[]: kind, ref, text; note 说明它是上下文不是证据",
        ),
        search_memory,
    )

    registry.register(
        ToolSpec(
            name="list_products",
            description=(
                "分页读取商品目录，按单一指标排序（pv_asc/pv_desc/order_rate_asc/"
                "stock_days_desc/margin_asc），可按类目或异常标记过滤；"
                "没有综合分排序，返回 total/returned/fields/rows"
            ),
            side_effect="read",
            requires_approval=False,
            input_schema={
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "flag": {"type": "string", "enum": list(LIST_FLAGS)},
                    "sort": {"type": "string", "enum": list(LIST_SORTS)},
                    "limit": {"type": "integer", "minimum": 1, "maximum": LIST_LIMIT_MAX},
                    "offset": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            },
            returns="total, returned, fields, rows[[id,title,category,pv,margin_pct,flags]], next_offset",
        ),
        list_products,
    )

    return registry


def build_ingest_registry() -> ToolRegistry:
    """两个上新工具：``file_parse``（R0）与 ``listing_draft``（R1）。

    单独一个 registry 而不是并进 ``build_registry()``：后者是工坊九个工具的既有
    契约，``tests/test_tools.py`` 按名字写死了名单。把上新工具塞进去，要么改那条
    断言（PRD 明令禁止），要么让工坊多看见两个不该它管的工具。两条都不选。
    """

    registry = ToolRegistry()

    registry.register(
        ToolSpec(
            name="file_parse",
            description="解析 data/ingest 下的供应商 CSV；绝对路径、..、越界软链一律拒绝",
            side_effect="read",
            requires_approval=False,
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "kind": {"type": "string", "enum": ["csv"]},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            returns="path, count, columns, rows[], ignored_columns[], notes[]",
        ),
        file_parse,
    )

    registry.register(
        ToolSpec(
            name="listing_draft",
            description="按已解析的供应商资料生成一条 Listing 草稿；只出草稿，不落库不发布",
            side_effect="write",
            requires_approval=True,
            input_schema={
                "type": "object",
                "properties": {
                    "sku": {"type": "string"},
                    "market": {"type": "string", "enum": MARKETS},
                    "language": {"type": "string"},
                    "facts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 20,
                    },
                },
                "required": ["sku", "market"],
                "additionalProperties": False,
            },
            returns="draft_id, status, title, bullets[], specs{}, missing[], flags[], compliance{}, writes=0",
        ),
        listing_draft,
    )

    return registry
