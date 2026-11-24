"""Per-store settings that must not be the same for every shop.

The rule for what belongs here: a value goes in the profile only when two
different shops would legitimately want different numbers. A 3C shop runs on 8%
gross margin; a home-goods shop on 40%. An industrial supplier needs a 90-day
judgement window; a fast-moving accessory needs 14. Hardcoding either way makes
the agent wrong for half its customers, and moving them all into environment
variables makes them wrong for every shop in a multi-tenant process.

Layering, tightest wins:

1. ``store_profile.json`` next to the store's data (this module);
2. environment variables (``OPS_*``) as the deployment-wide default;
3. per-run overrides such as ``--limit`` (handled by the caller).

A malformed key is ignored *and* reported in ``notes`` — a typo must not quietly
change how many products a shop edits this week.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import default_data_dir
from .contracts import MAX_WINDOW_DAYS, MIN_WINDOW_DAYS
from .quota import QuotaPolicy
from .store_metrics import MARGIN_FLOOR

PROFILE_FILE = "store_profile.json"
DEFAULT_BAND_FLOOR_RATIO = 0.5
DEFAULT_BAND_CEIL_RATIO = 3.0
# A judgement window is a business cadence, but not an unbounded one: beyond a
# year the metric has moved for reasons the copy had nothing to do with.
MAX_WINDOW_LIMIT = 365


@dataclass(frozen=True)
class StoreProfile:
    """Everything that varies by shop, resolved to concrete numbers."""

    quota: QuotaPolicy = field(default_factory=QuotaPolicy)
    margin_floor: float = MARGIN_FLOOR
    window_max_days: int = MAX_WINDOW_DAYS
    band_floor_ratio: float = DEFAULT_BAND_FLOOR_RATIO
    band_ceil_ratio: float = DEFAULT_BAND_CEIL_RATIO
    source: str = "env/default"
    notes: tuple[str, ...] = ()

    @property
    def from_file(self) -> bool:
        return self.source.startswith("profile:")

    def describe(self) -> str:
        where = self.source if self.from_file else "env/default"
        return f"{self.quota.describe()} margin>={self.margin_floor:.0%} ({where})"


def load_store_profile(
    store: Any | None = None,
    *,
    data_dir: Path | str | None = None,
) -> StoreProfile:
    """Read the store's profile, falling back to env for anything unset."""
    base = QuotaPolicy.from_env()
    directory = Path(data_dir) if data_dir else default_data_dir()
    path = directory / PROFILE_FILE
    if not path.exists():
        return StoreProfile(quota=base, source="env/default")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return StoreProfile(quota=base, source="env/default", notes=(f"{path} 读不了：{exc}",))
    if not isinstance(raw, dict):
        return StoreProfile(quota=base, source="env/default", notes=(f"{path} 不是 JSON 对象",))

    notes: list[str] = []
    slug = str(getattr(store, "slug", "") or "")
    file_slug = str(raw.get("store") or "").strip()
    if file_slug and slug and file_slug != slug:
        # A profile copied from another shop is the most likely mistake here,
        # and it would silently apply the wrong margin floor.
        notes.append(f"档案声明 store={file_slug}，本次运行的是 {slug}；已按档案执行")

    quota_raw = _section(raw, "quota", notes)
    pricing_raw = _section(raw, "pricing", notes)
    experiment_raw = _section(raw, "experiment", notes)

    quota = QuotaPolicy(
        ratio=_ratio(quota_raw, "ratio", base.ratio, notes),
        min_skus=_int(quota_raw, "min_skus", base.min_skus, 1, 999, notes),
        max_skus=_int(quota_raw, "max_skus", base.max_skus, 1, 999, notes),
        review_cap=_int(quota_raw, "review_cap", base.review_cap, 2, 9999, notes),
        variant_min=_int(quota_raw, "variant_min", base.variant_min, 1, 5, notes),
    )
    if quota.min_skus > quota.max_skus:
        notes.append(
            f"quota.min_skus={quota.min_skus} 大于 max_skus={quota.max_skus}，已对调"
        )
        quota = QuotaPolicy(
            ratio=quota.ratio,
            min_skus=quota.max_skus,
            max_skus=quota.min_skus,
            review_cap=quota.review_cap,
            variant_min=quota.variant_min,
        )

    margin_floor = _float(pricing_raw, "margin_floor", MARGIN_FLOOR, 0.0, 0.95, notes)
    band_low = _float(pricing_raw, "band_floor_ratio", DEFAULT_BAND_FLOOR_RATIO, 0.05, 1.0, notes)
    band_high = _float(pricing_raw, "band_ceil_ratio", DEFAULT_BAND_CEIL_RATIO, 1.0, 20.0, notes)
    if band_low >= band_high:
        notes.append(
            f"pricing 带 {band_low}..{band_high} 不成立，已回到 0.5..3.0"
        )
        band_low, band_high = DEFAULT_BAND_FLOOR_RATIO, DEFAULT_BAND_CEIL_RATIO

    window_max = _int(experiment_raw, "window_max_days", MAX_WINDOW_DAYS, MIN_WINDOW_DAYS, MAX_WINDOW_LIMIT, notes)

    unknown = sorted(
        key
        for section in (quota_raw, pricing_raw, experiment_raw)
        for key in section
        if key not in _KNOWN_KEYS
    )
    if unknown:
        notes.append(f"忽略未知配置项：{', '.join(unknown)}")

    return StoreProfile(
        quota=quota,
        margin_floor=margin_floor,
        window_max_days=window_max,
        band_floor_ratio=band_low,
        band_ceil_ratio=band_high,
        source=f"profile:{path}",
        notes=tuple(notes),
    )


_KNOWN_KEYS = frozenset(
    {
        "ratio",
        "min_skus",
        "max_skus",
        "review_cap",
        "variant_min",
        "margin_floor",
        "band_floor_ratio",
        "band_ceil_ratio",
        "window_max_days",
    }
)


def _section(raw: dict, key: str, notes: list[str]) -> dict:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        notes.append(f"{key} 应为对象，已忽略")
        return {}
    return value


def _ratio(section: dict, key: str, fallback: float, notes: list[str]) -> float:
    value = _float(section, key, fallback, 0.01, 1.0, notes)
    return value if 0 < value <= 1 else fallback


def _float(
    section: dict,
    key: str,
    fallback: float,
    low: float,
    high: float,
    notes: list[str],
) -> float:
    if key not in section:
        return fallback
    try:
        value = float(section[key])
    except (TypeError, ValueError):
        notes.append(f"{key}={section[key]!r} 不是数字，已用默认 {fallback}")
        return fallback
    if not (low <= value <= high):
        notes.append(f"{key}={value} 超出 {low}..{high}，已用默认 {fallback}")
        return fallback
    return value


def _int(
    section: dict,
    key: str,
    fallback: int,
    low: int,
    high: int,
    notes: list[str],
) -> int:
    if key not in section:
        return fallback
    raw = section[key]
    try:
        value = int(raw)
    except (TypeError, ValueError):
        notes.append(f"{key}={raw!r} 不是整数，已用默认 {fallback}")
        return fallback
    if not (low <= value <= high):
        notes.append(f"{key}={value} 超出 {low}..{high}，已用默认 {fallback}")
        return fallback
    return value
