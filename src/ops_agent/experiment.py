"""实验台账：发布即登记，窗口到期判定，结论沉淀成经验。

分工只有一条：**算术归代码，解释归模型**。

"这次改动到底有没有用"是一个计数问题：把窗口内的订单和曝光加起来，和发布前
28 天的基线比一比，再看这个差距是不是大到不像噪声。让模型算这个，它会把数字
编得很像真的；所以 ``judge()`` 只做算术，模型只负责把若干条**已经判定完**的
实验归纳成可复用的经验（见 ``lessons.py`` 的调用方）。

三条不变量：

* **窗口没满不给结论。** 未到期一律 ``observing``，只报告进度。
* **样本不够不给"成立"。** 天数、曝光、点击任一不达标就是 ``insufficient``。
* **基线只算一次。** 登记时写进 ``baseline``，之后判定只读它，保证结论可回放。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import NormalDist
from typing import Iterable

DEFAULT_WINDOW_DAYS = 14
BASELINE_DAYS = 28

# Below any of these the ratio is too noisy to call a winner.
MIN_OBSERVE_DAYS = 7
MIN_IMPRESSIONS = 300
MIN_CLICKS = 30

SIGNIFICANCE = 0.05
MAX_WINDOW_DAYS = 90

VERDICTS = ("supported", "rejected", "inconclusive", "insufficient")
VERDICT_CN = {
    "supported": "成立",
    "rejected": "不成立",
    "inconclusive": "不确定",
    "insufficient": "样本不足",
}

# Task orders carry the metric as free text ("order_rate_pct从当前0.07%提升至…"),
# so the name is parsed out rather than demanded as an enum. The prompt asks the
# director for a Chinese metric name, so the table has to accept Chinese: an
# English-only list silently fell through to order_rate for "点击率", and at a
# B2B order rate of ~0.07% that verdict can never leave "insufficient".
_METRIC_HINTS: tuple[tuple[str, str], ...] = (
    ("点击率", "click_rate"),
    ("点击量", "click_rate"),
    ("点击", "click_rate"),
    ("ctr", "click_rate"),
    ("click", "click_rate"),
    ("询盘率", "inquiry_rate"),
    ("询盘量", "inquiry_rate"),
    ("询盘", "inquiry_rate"),
    ("inquiry_rate", "inquiry_rate"),
    ("inquiry", "inquiry_rate"),
    ("订单率", "order_rate"),
    ("下单率", "order_rate"),
    ("成交率", "order_rate"),
    ("转化率", "order_rate"),
    ("order_rate", "order_rate"),
)


def _hint_index(text: str, hint: str) -> int:
    """Position of ``hint`` in ``text``, ignoring hits inside a longer word.

    ``electric`` contains ``ctr``; without a boundary check an English product
    name would out-vote the Chinese metric that comes after it.
    """
    start = 0
    while True:
        index = text.find(hint, start)
        if index < 0:
            return -1
        if hint.isascii():
            before = text[index - 1] if index > 0 else ""
            end = index + len(hint)
            after = text[end] if end < len(text) else ""
            if (before and before.isascii() and before.isalnum()) or (
                after and after.isascii() and after.isalnum()
            ):
                start = index + 1
                continue
        return index


@dataclass
class DailyMetric:
    """One day of traffic for one product."""

    day: date
    impressions: int = 0
    clicks: int = 0
    orders: int = 0
    returns: int = 0


def parse_day(value) -> date | None:
    """Accept ``2026-10-01``, ``2026-10-01T09:30:00`` and ``date`` itself."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def clamp_window(value) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_DAYS
    return max(1, min(MAX_WINDOW_DAYS, days))


def metric_of(text: str) -> tuple[str, str]:
    """Return ``(metric, note)``; note is empty when parsed from the text.

    The keyword that appears *first* wins, not the first row of the table: a
    sentence like "点击率先行，订单率随后" names the primary metric at the front,
    and reading the table in order would flip it.
    """
    lowered = str(text or "").lower()
    best: tuple[int, str] | None = None
    for hint, name in _METRIC_HINTS:
        index = _hint_index(lowered, hint)
        if index < 0:
            continue
        if best is None or index < best[0]:
            best = (index, name)
    if best is None:
        return "order_rate", "指标名未解析，按 order_rate 判定"
    name = best[1]
    if name == "inquiry_rate":
        # Local performance rows have clicks but no inquiries, so the
        # closest honest stand-in is click rate, and the outcome says so.
        return "click_rate", "inquiry_rate 无本地数据，用 click_rate 代理"
    return name, ""


# Which counter and which rate a metric is judged on. ``metric_of`` used to be
# decorative: it wrote a metric into the record and ``judge`` ignored it, so a
# click-rate experiment was scored on orders. One table, read by both.
_METRIC_FIELDS: dict[str, tuple[str, str]] = {
    "order_rate": ("orders", "order_rate"),
    "click_rate": ("clicks", "click_rate"),
}
_METRIC_CN = {"order_rate": "订单率", "click_rate": "点击率"}


def experiment_id_for(product_id: str, variant_id: str, started_at) -> str:
    """``product_id + variant_id + 开始日期``（PRD 9.4）。"""
    day = parse_day(started_at) or date.today()
    return f"{product_id}:{variant_id}:{day.isoformat()}"


def to_daily(items: Iterable) -> list[DailyMetric]:
    """Normalise ``Performance`` rows, dicts or tuples into DailyMetric."""
    rows: list[DailyMetric] = []
    for item in items or []:
        if isinstance(item, DailyMetric):
            rows.append(item)
            continue
        if isinstance(item, dict):
            day = parse_day(item.get("day") or item.get("date"))
            if day is None:
                continue
            rows.append(
                DailyMetric(
                    day=day,
                    impressions=int(item.get("impressions") or 0),
                    clicks=int(item.get("clicks") or 0),
                    orders=int(item.get("orders") or 0),
                    returns=int(item.get("returns") or 0),
                )
            )
            continue
        day = parse_day(getattr(item, "day", None))
        if day is None:
            continue
        rows.append(
            DailyMetric(
                day=day,
                impressions=int(getattr(item, "impressions", 0) or 0),
                clicks=int(getattr(item, "clicks", 0) or 0),
                orders=int(getattr(item, "orders", 0) or 0),
                returns=int(getattr(item, "returns", 0) or 0),
            )
        )
    rows.sort(key=lambda row: row.day)
    return rows


def _totals(rows: list[DailyMetric]) -> dict:
    impressions = sum(row.impressions for row in rows)
    clicks = sum(row.clicks for row in rows)
    orders = sum(row.orders for row in rows)
    returns = sum(row.returns for row in rows)
    return {
        "days": len({row.day for row in rows}),
        "impressions": impressions,
        "clicks": clicks,
        "orders": orders,
        "returns": returns,
        "order_rate": round(orders / impressions, 6) if impressions else 0.0,
        "click_rate": round(clicks / impressions, 6) if impressions else 0.0,
        "return_rate": round(returns / orders, 6) if orders else 0.0,
    }


def baseline_for(daily: Iterable, started_at, days: int = BASELINE_DAYS) -> dict:
    """Traffic in the ``days`` before publishing, summed, not averaged.

    Summing first and dividing once keeps the baseline ratio identical to how
    the treated window is computed; averaging daily ratios would weight quiet
    days the same as busy ones.
    """
    start = parse_day(started_at)
    rows = to_daily(daily)
    if start is None or not rows:
        return {"days": 0, "impressions": 0, "clicks": 0, "orders": 0, "returns": 0,
                "order_rate": 0.0, "click_rate": 0.0, "return_rate": 0.0}
    since = start - timedelta(days=max(1, int(days)))
    window = [row for row in rows if since <= row.day < start]
    totals = _totals(window)
    totals["from"] = since.isoformat()
    totals["to"] = (start - timedelta(days=1)).isoformat()
    return totals


def two_proportion_p(orders_a: int, n_a: int, orders_b: int, n_b: int) -> tuple[float, float]:
    """Pooled two-proportion z test; returns ``(z, two_sided_p)``.

    ``math.erfc(|z| / sqrt(2))`` is exactly ``2 * (1 - Phi(|z|))`` without
    pulling in scipy for one formula.
    """
    if n_a <= 0 or n_b <= 0:
        return 0.0, 1.0
    p_a = orders_a / n_a
    p_b = orders_b / n_b
    pooled = (orders_a + orders_b) / (n_a + n_b)
    variance = pooled * (1 - pooled) * (1 / n_a + 1 / n_b)
    if variance <= 0:
        return 0.0, 1.0
    z = (p_a - p_b) / math.sqrt(variance)
    return z, math.erfc(abs(z) / math.sqrt(2))


def required_impressions(
    baseline_rate: float,
    min_lift: float,
    alpha: float = SIGNIFICANCE,
    power: float = 0.8,
) -> int | None:
    """Impressions per arm needed to detect ``min_lift`` at this base rate.

    Reported next to a negative result so "not significant" is never mistaken
    for "no effect": it tells the operator how far the window is from being able
    to answer the question at all.
    """
    if baseline_rate <= 0 or min_lift <= 0:
        return None
    treated = baseline_rate * (1 + min_lift)
    if treated >= 1:
        return None
    normal = NormalDist()
    z_alpha = normal.inv_cdf(1 - alpha / 2)
    z_power = normal.inv_cdf(power)
    pooled = (baseline_rate + treated) / 2
    spread = math.sqrt(2 * pooled * (1 - pooled))
    spread_b = math.sqrt(baseline_rate * (1 - baseline_rate) + treated * (1 - treated))
    numerator = (z_alpha * spread + z_power * spread_b) ** 2
    denominator = (treated - baseline_rate) ** 2
    if denominator <= 0:
        return None
    return int(math.ceil(numerator / denominator))


def build_record(
    *,
    product_id: str,
    variant_id: str,
    variant_key: str = "",
    task: dict | None = None,
    store_slug: str = "",
    started_at="",
    window_days=None,
    baseline: dict | None = None,
    site_variant_id: str = "",
    title: str = "",
    angle: str = "",
    price_cents: int = 0,
    run_id: str = "",
    series_key: str = "",
) -> dict:
    """One experiment line, written the moment a publish succeeds."""
    task = task or {}
    start = parse_day(started_at) or date.today()
    metric, metric_note = metric_of(str(task.get("success_metric") or ""))
    record = {
        "experiment_id": experiment_id_for(product_id, variant_id, start),
        "product_id": str(product_id),
        # Where the daily numbers come from. A site product and the local daily
        # series are different identifiers, so the link is explicit, not guessed.
        "series_key": str(series_key or product_id),
        "variant_id": str(variant_id),
        "variant_key": str(variant_key or variant_id),
        "site_variant_id": str(site_variant_id or ""),
        "store_slug": str(store_slug or ""),
        "task_id": str(task.get("task_id") or ""),
        "run_id": str(run_id or task.get("run_id") or ""),
        "category": str(task.get("category") or ""),
        "title": str(title or ""),
        "angle": str(angle or ""),
        "price_cents": int(price_cents or 0),
        "hypothesis": str(task.get("hypothesis") or ""),
        "problem": str(task.get("problem") or ""),
        "success_metric": str(task.get("success_metric") or ""),
        "metric": metric,
        "metric_note": metric_note,
        "min_lift": float(task.get("min_lift") or 0.0),
        "window_days": clamp_window(window_days if window_days is not None else task.get("window_days")),
        "baseline": dict(baseline or {}),
        "started_at": start.isoformat(),
        "status": "observing",
        "verdict": "",
        "outcome": {},
        "registered_at": datetime.now().isoformat(timespec="seconds"),
    }
    return record


def judge(
    record: dict,
    daily: Iterable,
    as_of=None,
    *,
    min_days: int = MIN_OBSERVE_DAYS,
    min_impressions: int = MIN_IMPRESSIONS,
    min_clicks: int = MIN_CLICKS,
    alpha: float = SIGNIFICANCE,
) -> dict:
    """Judge one experiment; never invents a verdict before the window closes."""
    today = parse_day(as_of) or date.today()
    start = parse_day(record.get("started_at"))
    window = clamp_window(record.get("window_days"))
    baseline = dict(record.get("baseline") or {})
    metric = str(record.get("metric") or "order_rate")
    # 基线里 clicks=0 而曝光不为 0，说明数据源根本没有点击计数（站点只有
    # pv/uv/询盘/订单）。继续按点击率算只会得到 -100% 的假暴跌，降到订单率。
    downgraded = ""
    if metric == "click_rate" and int(baseline.get("clicks") or 0) <= 0:
        metric = "order_rate"
        downgraded = "数据源无点击计数，按订单率判定"

    if start is None:
        return {
            "status": "verdict",
            "verdict": "insufficient",
            "outcome": {
                "metric": metric,
                "metric_text": str(record.get("success_metric") or ""),
                "note": "started_at 不可解析，无法计算窗口",
                "as_of": today.isoformat(),
                "sample": {"days": 0, "impressions": 0, "clicks": 0, "orders": 0},
            },
        }

    end = start + timedelta(days=window)
    rows = to_daily(daily)
    observed = [row for row in rows if start <= row.day <= today and row.day < end]
    treated = _totals(observed)
    days_left = max(0, (end - today).days)

    outcome: dict = {
        "metric": metric,
        "metric_text": str(record.get("success_metric") or ""),
        "proxy": "；".join(
            part for part in (downgraded, str(record.get("metric_note") or "")) if part
        ),
        "baseline": baseline,
        "treated": treated,
        "min_lift": float(record.get("min_lift") or 0.0),
        "alpha": alpha,
        "window": {
            "started_at": start.isoformat(),
            "ends_at": end.isoformat(),
            "window_days": window,
            "days_left": days_left,
        },
        "as_of": today.isoformat(),
    }
    sample = {key: treated[key] for key in ("days", "impressions", "clicks", "orders")}
    outcome["sample"] = sample
    min_lift = float(record.get("min_lift") or 0.0)
    # The sample-size estimate has to use the same rate the verdict will use.
    needed = required_impressions(float(baseline.get(_METRIC_FIELDS.get(metric, ("", "order_rate"))[1]) or 0.0), min_lift)
    if needed:
        # "Not significant" and "too small to tell" look identical to an operator
        # unless the gap is quantified.
        outcome["required_impressions"] = needed

    if today < end:
        outcome["note"] = f"观察中，还有 {days_left} 天到期"
        return {"status": "observing", "verdict": "", "outcome": outcome}

    base_impressions = int(baseline.get("impressions") or 0)
    numerator_key, rate_key = _METRIC_FIELDS.get(metric, ("", ""))

    if not numerator_key:
        # An unrecognised metric has no honest computation. Saying "no effect"
        # would be a guess dressed up as a verdict.
        outcome["note"] = f"指标 {metric!r} 没有可计算的口径，按不确定处理"
        return {"status": "verdict", "verdict": "inconclusive", "outcome": outcome}

    base_numerator = int(baseline.get(numerator_key) or 0)

    # The click gate only makes sense for a click-rate experiment. Applying it
    # to orders permanently starved B2B experiments that get traffic but few
    # recorded clicks, and never let them leave "insufficient".
    starved = (
        sample["days"] < min_days
        or sample["impressions"] < min_impressions
        or base_impressions <= 0
        or (metric == "click_rate" and sample["clicks"] < min_clicks)
    )
    if starved:
        if not baseline:
            reason = "没有基线数据（发布前 28 天无记录）"
        elif not observed:
            reason = "窗口内没有任何按日数据（数据源未覆盖该商品）"
        else:
            reason = (
                f"样本不足（天数 {sample['days']}/{min_days}，曝光 {sample['impressions']}/{min_impressions}，"
                f"基线曝光 {base_impressions}）"
            )
            if metric == "click_rate":
                reason = reason[:-1] + f"，点击 {sample['clicks']}/{min_clicks}）"
        if needed:
            reason += f"；要判定需每组约 {needed:,} 曝光"
        outcome["note"] = reason
        # "样本不足" alone reads like nothing was measured. The direction is
        # real even when the sample cannot carry a verdict, so hand it over
        # with a flag; the console shows it as a trend, never as a conclusion.
        base_rate = float(baseline.get(rate_key) or 0.0) if baseline else 0.0
        treated_rate = float(treated.get(rate_key) or 0.0)
        if base_rate > 0:
            outcome["lift"] = round((treated_rate - base_rate) / base_rate, 4)
            outcome["provisional"] = True
        return {"status": "verdict", "verdict": "insufficient", "outcome": outcome}

    treated_numerator = int(treated[numerator_key])
    z, p_value = two_proportion_p(
        treated_numerator, sample["impressions"], base_numerator, base_impressions
    )
    base_rate = float(baseline.get(rate_key) or 0.0)
    treated_rate = float(treated[rate_key])
    lift = (treated_rate - base_rate) / base_rate if base_rate > 0 else None

    outcome.update({"z": round(z, 4), "p_value": round(p_value, 6), "lift": None if lift is None else round(lift, 4)})

    if lift is None:
        # Baseline never converted: a relative lift is undefined, so the call
        # rests on whether the treated window converts significantly more than
        # nothing — and only against a target that was actually set.
        if treated_numerator > 0 and p_value < alpha and min_lift > 0:
            verdict = "supported"
            outcome["note"] = f"基线无{_METRIC_CN[metric]}，按绝对提升判定成立"
        elif treated_numerator > 0 and p_value < alpha:
            verdict = "inconclusive"
            outcome["note"] = f"基线无{_METRIC_CN[metric]}且未设最小提升，无法判定是否达标"
        else:
            verdict = "inconclusive"
            outcome["note"] = f"基线无{_METRIC_CN[metric]}且窗口内无显著提升，无法判定"
    elif min_lift <= 0:
        # "Lift >= 0 and significant" would call any noise a success. Without a
        # target there is nothing to be right about.
        verdict = "inconclusive"
        outcome["note"] = f"未设最小提升，只有显著提升 {lift:.1%}（p={p_value:.3f}），无法判定是否达标"
    elif lift >= min_lift and p_value < alpha:
        verdict = "supported"
        outcome["note"] = f"{_METRIC_CN[metric]}提升 {lift:.1%} ≥ 目标 {min_lift:.1%}，p={p_value:.3f}"
    elif lift <= 0 and p_value < alpha:
        verdict = "rejected"
        outcome["note"] = f"{_METRIC_CN[metric]}提升 {lift:.1%} ≤ 0 且显著，p={p_value:.3f}"
    else:
        verdict = "inconclusive"
        reason = "提升未达目标" if lift < min_lift else "提升达目标但不显著"
        outcome["note"] = f"{reason}（{_METRIC_CN[metric]}提升 {lift:.1%}，p={p_value:.3f}）"
        if needed and sample["impressions"] < needed:
            outcome["note"] += f"；当前曝光 {sample['impressions']:,}，要判定需约 {needed:,}"

    return {"status": "verdict", "verdict": verdict, "outcome": outcome}


def apply_judgement(record: dict, judged: dict) -> dict:
    """Merge a judgement back into the ledger line, keeping history."""
    updated = dict(record)
    updated["status"] = str(judged.get("status") or "observing")
    verdict = str(judged.get("verdict") or "")
    updated["verdict"] = verdict
    updated["outcome"] = dict(judged.get("outcome") or {})
    if verdict:
        updated["verdict_cn"] = VERDICT_CN.get(verdict, verdict)
        updated["judged_at"] = datetime.now().isoformat(timespec="seconds")
    return updated


def is_due(record: dict, as_of=None) -> bool:
    """True when the observation window has closed."""
    today = parse_day(as_of) or date.today()
    start = parse_day(record.get("started_at"))
    if start is None:
        return True
    return today >= start + timedelta(days=clamp_window(record.get("window_days")))


def summary(records: list[dict]) -> dict:
    """Counts for the CLI footer and the console card."""
    counts = {key: 0 for key in ("observing", "verdict")}
    for item in records:
        status = str(item.get("status") or "observing")
        counts[status] = counts.get(status, 0) + 1
    verdicts = {key: 0 for key in VERDICTS}
    for item in records:
        verdict = str(item.get("verdict") or "")
        if verdict in verdicts:
            verdicts[verdict] += 1
    return {"total": len(records), "status": counts, "verdicts": verdicts}


class ExperimentRegistrar:
    """Turns one successful publish into one ledger line.

    ``baseline_provider(product_id, started_at)`` keeps this class free of both
    the site client and the mock platform: offline runs read the local daily
    series, online runs read the product's 30-day metrics, and neither leaks in.
    """

    def __init__(self, memory, baseline_provider=None, store_slug: str = "") -> None:
        self.memory = memory
        self.baseline_provider = baseline_provider
        self.store_slug = store_slug

    @staticmethod
    def _variant(proposal: dict, preview) -> dict:
        variants = proposal.get("variants") or []
        for item in variants:
            if str(item.get("variant_key") or "") == str(getattr(preview, "variant_key", "")):
                return dict(item)
        return dict(variants[0]) if variants else {}

    def register(self, proposal: dict, preview, *, started_at=None, window_days=None) -> dict:
        product_id = str(proposal.get("product_id") or getattr(preview, "product_id", ""))
        if not product_id:
            raise ValueError("proposal has no product_id")
        variant = self._variant(proposal, preview)
        task = dict(proposal.get("task") or {})
        start = parse_day(started_at) or date.today()

        baseline: dict = {}
        if self.baseline_provider is not None:
            baseline = dict(self.baseline_provider(product_id, start) or {})

        record = build_record(
            product_id=product_id,
            variant_id=str(variant.get("variant_key") or getattr(preview, "variant_key", "")),
            variant_key=str(variant.get("variant_key") or getattr(preview, "variant_key", "")),
            task=task,
            store_slug=self.store_slug,
            started_at=start,
            window_days=window_days if window_days is not None else task.get("window_days"),
            baseline=baseline,
            site_variant_id=str(getattr(preview, "variant_id", "") or ""),
            title=str(proposal.get("title") or ""),
            angle=str(variant.get("angle") or ""),
            price_cents=int(variant.get("price_cents") or 0),
            run_id=str(proposal.get("run_id") or task.get("run_id") or ""),
        )
        created = self.memory.append_experiment(record) if self.memory is not None else False
        record["created"] = bool(created)
        return record
