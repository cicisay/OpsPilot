"""Shared contracts for the multi-agent layer.

One rule shapes this whole package: **the model proposes, the code decides.**
Everything the model must not touch is declared here, and every reply passes
through ``RedLineGuard`` before it can reach the site.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# The site stores titleEn / bulletsEn / descEn. Copy that is not English would
# ship Chinese text to an English storefront, so it is rejected outright.
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# Any digit group in copy must also exist in the facts we handed the model.
# "600D fabric" or "sets up in 10 seconds" is a hallucinated spec; on a live
# storefront it is a false advertising claim, so the code refuses it.
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")

# What counts as a spec claim worth blocking: a number with a unit, a decimal,
# a material grade such as 600D / 7075, or any 3+ digit figure. Plain counters
# ("Angle 1", "3-Person") are not claims and must not trip the guard.
#
# Percentages live in their own pattern. A trailing ``\b`` after ``%`` can never
# match — ``%`` is not a word character — so keeping it in the alternation below
# silently dropped every two-digit claim ("up 30%", "99% cotton") while three
# digits still tripped ``_SPEC_LARGE``. That gap is exactly the shape of a
# fabricated marketing number, so it gets its own rule with no trailing boundary.
_SPEC_WITH_UNIT = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:kg|g|lbs?|oz|cm|mm|inch|in|ft|mah|wh|w|v|hours?|hrs?|minutes?|mins?|seconds?|secs?|days?|pcs|units?|years?)\b",
    re.IGNORECASE,
)
_SPEC_PERCENT = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:%|(?:percent|pct)\b)",
    re.IGNORECASE,
)
_SPEC_GRADE = re.compile(r"\b\d{2,4}[A-Z]{1,3}\b")
_SPEC_LARGE = re.compile(r"\b\d{3,}\b")
_SPEC_DECIMAL = re.compile(r"\b\d+\.\d+\b")


def number_tokens(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKC", str(text or ""))
    return {token.replace(",", "").rstrip(".") for token in _NUMBER.findall(folded)}


def spec_numbers(text: str) -> set[str]:
    """Numbers in copy that read as product facts rather than counters."""
    # Full-width digits and percent signs are the same claim to a reader.
    text = unicodedata.normalize("NFKC", str(text or ""))
    # "5,000" must stay one number; otherwise the 000 tail looks like a spec.
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    found: set[str] = set()
    for pattern in (_SPEC_WITH_UNIT, _SPEC_PERCENT, _SPEC_GRADE, _SPEC_LARGE, _SPEC_DECIMAL):
        for match in pattern.findall(text):
            found |= number_tokens(match)
    return found


def known_facts_text(ctx: "WorkContext") -> str:
    """Everything the model was told, flattened for numeric traceability."""
    product = ctx.product
    parts = [
        ctx.current_title,
        *ctx.current_bullets,
        ctx.current_description,
        str(getattr(product, "title_en", "")),
        str(getattr(product, "category_slug", "") or ""),
        str(getattr(product, "moq", "") or ""),
        str(getattr(product, "lead_time_days", "") or ""),
        str(getattr(product, "incoterm", "") or ""),
    ]
    return " ".join(part for part in parts if part)

# Fields on a product the model may rewrite. Anything else is read-only.
EDITABLE_PRODUCT_FIELDS = ("titleEn", "bulletsEn", "descEn")

# Identity, pricing floor, commercial terms: never writable by a model.
FORBIDDEN_FIELDS = frozenset(
    {
        "id",
        "storeId",
        "slug",
        "status",
        "categoryId",
        "categorySlug",
        "moq",
        "leadTimeDays",
        "incoterm",
        "originPort",
        "hsCode",
        "certifications",
        "seoTitle",
        "seoDesc",
        "samplePriceCents",
        "deletedAt",
        "createdAt",
        "costCents",
        "skuCode",
        "stock",
        "variantId",
        "productId",
    }
)

# Tool-level boundary: agents may read and compute, never write.
ALLOWED_TOOL_SIDE_EFFECTS = frozenset({"read", "compute"})
FORBIDDEN_TOOLS = frozenset({"publish_listing", "rollback"})

MAX_TITLE_CHARS = 300
MAX_BULLETS = 5
MAX_BULLET_CHARS = 200
MAX_DESCRIPTION_CHARS = 2000


@dataclass
class GuardResult:
    variants: list[dict] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.variants)


class RedLineGuard:
    """Cleans model output down to what the code is willing to accept.

    Violations never raise: the offending field is dropped, the rest survives,
    and the reason is recorded for the audit trail.
    """

    def __init__(self, price_points: list[int], limit: int = 3, facts_text: str = "") -> None:
        if not price_points:
            raise ValueError("price_points must not be empty")
        self.price_points = [int(item) for item in price_points]
        self.limit = max(1, limit)
        self.allowed_numbers = number_tokens(facts_text) if facts_text else None

    def check_variants(self, raw: Any) -> GuardResult:
        result = GuardResult()
        if not isinstance(raw, list):
            result.violations.append("variants is not a list")
            return result

        for index, item in enumerate(raw):
            if len(result.variants) >= self.limit:
                break
            cleaned, violations = self._clean(item, index)
            result.violations.extend(violations)
            if cleaned is not None:
                result.variants.append(cleaned)
        return result

    def _clean(self, item: Any, index: int) -> tuple[dict | None, list[str]]:
        prefix = f"variant[{index}]"
        violations: list[str] = []
        if not isinstance(item, dict):
            return None, [f"{prefix}: not an object"]

        for key in FORBIDDEN_FIELDS:
            if key in item:
                violations.append(f"{prefix}: forbidden field dropped: {key}")

        title = str(item.get("title") or "").strip()
        description = str(item.get("description") or "").strip()
        angle = str(item.get("angle") or "").strip()

        bullets_raw = item.get("bullets")
        bullets: list[str] = []
        if isinstance(bullets_raw, list):
            for bullet in bullets_raw:
                text = str(bullet or "").strip()
                if text:
                    bullets.append(text[:MAX_BULLET_CHARS])
        if len(bullets) > MAX_BULLETS:
            violations.append(f"{prefix}: bullets trimmed to {MAX_BULLETS}")
            bullets = bullets[:MAX_BULLETS]

        if self.allowed_numbers is not None:
            supported = [
                bullet
                for bullet in bullets
                if not (spec_numbers(bullet) - self.allowed_numbers)
            ]
            if len(supported) != len(bullets):
                dropped = sorted({token for b in bullets for token in spec_numbers(b) - self.allowed_numbers})
                violations.append(f"{prefix}: bullets with unsupported numbers dropped: {dropped}")
                bullets = supported
            invented = (spec_numbers(title) | spec_numbers(description)) - self.allowed_numbers
            if invented:
                violations.append(f"{prefix}: unsupported numeric claim(s) {sorted(invented)}, dropped")
                return None, violations

        if not title or not description or not bullets:
            violations.append(f"{prefix}: missing title/description/bullets, dropped")
            return None, violations

        if _CJK.search(" ".join([title, *bullets, description])):
            violations.append(f"{prefix}: copy is not English (CJK characters), dropped")
            return None, violations

        if len(title) > MAX_TITLE_CHARS:
            violations.append(f"{prefix}: title truncated")
            title = title[:MAX_TITLE_CHARS]
        if len(description) > MAX_DESCRIPTION_CHARS:
            description = description[:MAX_DESCRIPTION_CHARS]

        price_index = item.get("price_index", 0)
        if isinstance(price_index, bool) or not isinstance(price_index, int):
            violations.append(f"{prefix}: price_index is not an int, forced to 0")
            price_index = 0
        if not 0 <= price_index < len(self.price_points):
            violations.append(f"{prefix}: price_index out of range, forced to 0")
            price_index = 0

        lift = item.get("expected_lift", 0.05)
        try:
            lift_value = float(lift)
        except (TypeError, ValueError):
            lift_value = 0.05
        lift_value = min(0.5, max(0.0, lift_value))

        return (
            {
                "angle": angle or f"angle-{index + 1}",
                "title": title,
                "bullets": bullets,
                "description": description,
                "price_index": price_index,
                "price_cents": self.price_points[price_index],
                "expected_lift": round(lift_value, 4),
                "rationale": str(item.get("rationale") or "").strip()[:600],
                "image_brief": str(item.get("image_brief") or "").strip()[:400],
                # Not editable copy, but part of the proposal contract: the
                # designer brief and the workshop's own checklist must survive
                # the guard, otherwise every guarded variant ships without them.
                "material_brief": [
                    str(entry).strip()[:200]
                    for entry in (item.get("material_brief") or [])
                    if str(entry).strip()
                ][:3]
                if isinstance(item.get("material_brief"), (list, tuple))
                else [],
                "self_review": str(item.get("self_review") or "").strip()[:600],
                "self_check": item.get("self_check")
                if isinstance(item.get("self_check"), dict)
                else {},
            },
            violations,
        )


def allowed_tools(registry, *, forbidden_side_effects: bool = True) -> list[str]:
    """Names of tools an agent may call: read/compute only, never publish."""
    names: list[str] = []
    for spec in registry.specs():
        name = str(spec.get("name", ""))
        if not name or name in FORBIDDEN_TOOLS:
            continue
        if forbidden_side_effects and spec.get("side_effect") not in ALLOWED_TOOL_SIDE_EFFECTS:
            continue
        names.append(name)
    return names


@dataclass
class WorkContext:
    """The blackboard: every agent reads the same facts, writes its own section."""

    product: Any
    task: dict
    market: str = "US"
    metrics: Any = None
    reviews: list[dict] = field(default_factory=list)
    benchmark: Any = None
    price_points: list[int] = field(default_factory=list)
    current_title: str = ""
    current_bullets: list[str] = field(default_factory=list)
    current_description: str = ""
    drafts: list[dict] = field(default_factory=list)
    critique: dict = field(default_factory=dict)
    rounds: int = 0


@runtime_checkable
class Agent(Protocol):
    role: str

    def run(self, ctx: WorkContext) -> dict:  # pragma: no cover - protocol
        ...
