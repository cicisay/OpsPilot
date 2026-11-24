"""How many SKUs to touch this week, and how many variants each gets.

The model decides the actual number; this module decides what is *possible*.
That split matters: a model free to pick any number will eventually pick 200,
and the human approval queue is the real bottleneck. So the bounds are computed
from store size and review capacity, and the model only chooses inside them.

``low`` is a *reference*, not a quota to fill. A store this size usually has
enough real problems to justify that many picks; when it does not, a smaller
week is the honest answer. Raising the number would only teach the model to pad
the list with weak picks, so under-filling is accepted and the caller asks for a
reason instead (``underfill_reason``).

Only a real ceiling — the model asked for more than capacity — is reported with
``guarded=True``, so the console can show "asked for 12, allowed 5" and still
count trims honestly.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

from .config import load_env

DEFAULT_RATIO = 0.10
DEFAULT_MIN_SKUS = 3
DEFAULT_MAX_SKUS = 20
DEFAULT_REVIEW_CAP = 24
MIN_VARIANTS = 2
MAX_VARIANTS = 5

@dataclass(frozen=True)
class QuotaDecision:
    """One clamped number plus the reasoning needed to explain it later."""

    value: int
    low: int
    high: int
    requested: int
    guarded: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "value": self.value,
            "low": self.low,
            "high": self.high,
            "requested": self.requested,
            "guarded": self.guarded,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class QuotaPolicy:
    """Bounds are configurable; defaults match the PRD.

    ``variant_min`` belongs here rather than in a module constant because a shop
    that ships one copy per product instead of an A/B pair is a business fact,
    not a bug to correct.
    """

    ratio: float = DEFAULT_RATIO
    min_skus: int = DEFAULT_MIN_SKUS
    max_skus: int = DEFAULT_MAX_SKUS
    review_cap: int = DEFAULT_REVIEW_CAP
    variant_min: int = MIN_VARIANTS

    @classmethod
    def from_env(cls) -> "QuotaPolicy":
        load_env()
        ratio = _float("OPS_QUOTA_RATIO", DEFAULT_RATIO)
        if not (0 < ratio <= 1):
            ratio = DEFAULT_RATIO
        return cls(
            ratio=ratio,
            min_skus=max(1, _int("OPS_QUOTA_MIN_SKUS", DEFAULT_MIN_SKUS)),
            max_skus=max(1, _int("OPS_QUOTA_MAX_SKUS", DEFAULT_MAX_SKUS)),
            review_cap=max(2, _int("OPS_REVIEW_CAP", DEFAULT_REVIEW_CAP)),
            variant_min=max(1, _int("OPS_VARIANT_MIN", MIN_VARIANTS)),
        )

    def sku_bounds(self, active_products: int, limit: int = 0) -> tuple[int, int]:
        """How many products may be touched, given store size and review capacity.

        Three ceilings apply and the tightest wins:
        1. store size, so we never plan to edit more products than exist;
        2. a share of the catalogue, so a 500-SKU store does not get 50 tasks;
        3. review capacity, assuming each product needs at least two variants,
           because the operator has to read every variant.

        ``limit`` is the human override (``--limit``): a hard ceiling, applied
        here so the digest, the tool schema and the validator cannot disagree
        about what "below the usual week" means.

        The floor is ``min_skus`` bounded by two things: the ceiling above, and
        the same weekly share used to build that ceiling. A 3-product shop does
        not have a "usual week" of 3 — that is its whole catalogue — and the old
        ``2 if active < 20`` was the same intuition with a magic number in it.
        A store that genuinely wants a different rhythm says so in its profile.
        """
        active = max(0, int(active_products))
        if active == 0:
            return (0, 0)

        by_ratio = max(self.min_skus, math.ceil(active * self.ratio))
        # Capacity is per *variant*, not per product: a shop that wants three
        # versions each cannot also absorb the same number of products.
        by_capacity = max(1, self.review_cap // max(1, self.variant_min))
        high = min(by_ratio, self.max_skus, by_capacity, active)

        by_share = max(1, math.ceil(active * self.ratio))
        low = min(self.min_skus, high, by_share)

        ceiling = _int_value(limit)
        if ceiling > 0:
            high = max(1, min(high, ceiling))
            low = min(low, high)
        return (max(1, low), high)

    def clamp_skus(self, requested: int, active_products: int, limit: int = 0) -> QuotaDecision:
        low, high = self.sku_bounds(active_products, limit)
        if high == 0:
            return QuotaDecision(0, 0, 0, _int_value(requested), False, "no active products")
        requested = _int_value(requested)
        if requested <= 0:
            # No number and no list: there is nothing to clamp. Inventing the
            # floor here is how a quiet week became three fabricated picks.
            return QuotaDecision(0, low, high, 0, False, "no selection")
        if requested > high:
            return QuotaDecision(high, low, high, requested, True, f"capped at {high}")
        if requested < low:
            # Below the usual week, but the model saw the store and we did not.
            # Accept it; the caller asks for ``underfill_reason`` so the smaller
            # week is explained rather than silently accepted.
            return QuotaDecision(requested, low, high, requested, False, f"below usual floor {low}")
        return QuotaDecision(requested, low, high, requested, False, "within bounds")

    def variant_bounds(self, sku_count: int) -> tuple[int, int]:
        """Variants per product, limited by what is left of the review budget."""
        skus = max(0, int(sku_count))
        if skus == 0:
            return (self.variant_min, self.variant_min)
        per_sku = max(self.variant_min, self.review_cap // skus)
        high = min(MAX_VARIANTS, per_sku)
        return (self.variant_min, max(self.variant_min, high))

    def clamp_variants(self, requested: int, sku_count: int) -> QuotaDecision:
        low, high = self.variant_bounds(sku_count)
        requested = _int_value(requested)
        if requested <= 0:
            requested = low
        if requested < low:
            return QuotaDecision(low, low, high, requested, True, f"raised to floor {low}")
        if requested > high:
            return QuotaDecision(high, low, high, requested, True, f"capped at {high}")
        return QuotaDecision(requested, low, high, requested, False, "within bounds")

    def total_versions(self, sku_count: int, variants_per_sku: int) -> int:
        return max(0, int(sku_count)) * max(0, int(variants_per_sku))

    def describe(self) -> str:
        return (
            f"ratio={self.ratio:.2f} skus={self.min_skus}..{self.max_skus} "
            f"variants>={self.variant_min} review_cap={self.review_cap}"
        )


def _int(value: str, fallback: int) -> int:
    raw = (os.environ.get(value) or "").strip()
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


def _float(name: str, fallback: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return fallback
    try:
        return float(raw)
    except ValueError:
        return fallback


def _int_value(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
