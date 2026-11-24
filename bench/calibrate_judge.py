"""标定实验判定器：没有真实店铺，也能知道它判得准不准。

回答两个问题：

1. 它说"成立"的时候，有多少次是真成立？（误报率 / 检出率）
2. 一家多大的店，才养得起一次能判定的实验？（规模门槛）

做法不是"跑一遍看看"，而是造 1 万次**已知答案**的模拟实验，让 judge()
去判，再拿判定结果对答案。这是统计学里验证一个判定程序的常规手段，
不需要任何真实店铺、不需要联网、不需要 API key。

跑法：
    python bench/calibrate_judge.py
"""

from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.experiment import (  # noqa: E402
    DailyMetric,
    baseline_for,
    judge,
    required_impressions,
)

BASELINE_DAYS = 28
WINDOW_DAYS = 14
MIN_LIFT = 0.15
TRIALS = 400
SEED = 20261123

# 真实跨境电商的订单率大致区间：B2B 询盘型 0.5% 上下，B2C 标品 2%–5%。
BASE_RATES = ((0.005, "0.5%"), (0.02, "2%"), (0.05, "5%"))
# 单个商品每天的自然曝光。300 是刚起步的小店，8000 是成熟店铺的爆款。
TRAFFICS = ((300, "300/天"), (1500, "1500/天"), (8000, "8000/天"))
# 三种"标准答案"：完全没效果、有效果但不到目标、有效果且超过目标。
CASES = ((0.0, "没效果"), (0.08, "8%"), (0.30, "30%"))


def _width(text) -> int:
    """Display width: CJK counts double."""
    return sum(2 if ord(ch) > 127 else 1 for ch in str(text))


def pad(text, width: int) -> str:
    return str(text) + " " * max(0, width - _width(text))


def draw(mean_impressions: int, rate: float, rng: random.Random) -> tuple[int, int]:
    """One day of traffic: impressions jitter around the mean, orders are binomial."""
    traffic = max(1, int(round(rng.gauss(mean_impressions, mean_impressions * 0.15))))
    if hasattr(rng, "binomialvariate"):
        orders = rng.binomialvariate(traffic, rate)
    else:  # Python < 3.12 fallback
        orders = sum(1 for _ in range(traffic) if rng.random() < rate)
    return traffic, min(traffic, orders)


def run_one(base_rate: float, daily: int, true_lift: float, rng: random.Random) -> str:
    """Generate one experiment with a known truth and return the judge's verdict."""
    start = date(2026, 3, 2)
    rows: list[DailyMetric] = []
    for i in range(BASELINE_DAYS):
        day = start - timedelta(days=BASELINE_DAYS - i)
        traffic, orders = draw(daily, base_rate, rng)
        rows.append(DailyMetric(day=day, impressions=traffic, orders=orders))
    for i in range(WINDOW_DAYS):
        traffic, orders = draw(daily, base_rate * (1 + true_lift), rng)
        rows.append(DailyMetric(day=start + timedelta(days=i), impressions=traffic, orders=orders))

    record = {
        "product_id": "SKU-1",
        "variant_id": "v1",
        "started_at": start.isoformat(),
        "window_days": WINDOW_DAYS,
        "metric": "order_rate",
        "min_lift": MIN_LIFT,
        "success_metric": "订单率",
        "baseline": baseline_for(rows, start, BASELINE_DAYS),
    }
    judged = judge(record, rows, as_of=start + timedelta(days=WINDOW_DAYS))
    return str(judged.get("verdict") or "observing")


def part_one() -> None:
    print("=" * 78)
    print("第一部分 · 判定器准不准（每次 400 次模拟，共 10800 次）")
    print("=" * 78)
    print("场景：最小提升目标 15%，观察窗口 14 天，基线取发布前 28 天。")
    print()
    header = (
        pad("订单率", 8)
        + pad("曝光/天", 10)
        + pad("样本/需求", 10)
        + pad("误报率", 9)
        + pad("误报达标", 10)
        + pad("检出率", 9)
        + pad("样本不足", 10)
    )
    print(header)
    print("-" * 78)

    for base_rate, rate_text in BASE_RATES:
        for daily, traffic_text in TRAFFICS:
            need = required_impressions(base_rate, MIN_LIFT) or 1
            ratio = (daily * WINDOW_DAYS) / need
            rng = random.Random(f"{SEED}-{base_rate}-{daily}")
            counts = {}
            for truth, _label in CASES:
                tally = {"supported": 0, "rejected": 0, "inconclusive": 0, "insufficient": 0}
                for _ in range(TRIALS):
                    verdict = run_one(base_rate, daily, truth, rng)
                    tally[verdict] = tally.get(verdict, 0) + 1
                counts[truth] = tally

            false_alarm = counts[0.0]["supported"] / TRIALS
            false_target = counts[0.08]["supported"] / TRIALS
            detection = counts[0.30]["supported"] / TRIALS
            starved = counts[0.30]["insufficient"] / TRIALS

            print(
                pad(rate_text, 8)
                + pad(traffic_text, 10)
                + pad(f"{ratio:.0%}", 10)
                + pad(f"{false_alarm:.1%}", 9)
                + pad(f"{false_target:.1%}", 10)
                + pad(f"{detection:.1%}", 9)
                + pad(f"{starved:.1%}", 10)
            )


def part_two() -> None:
    print()
    print("=" * 78)
    print("第二部分 · 一家多大的店，才养得起一次能判定的实验")
    print("=" * 78)
    print("每组所需曝光由 required_impressions() 算出（alpha=0.05, power=0.8）。")
    print("观察窗口 14 天，所以「每天需要」= 每组需求 / 14。")
    print()
    header = (
        pad("订单率", 8)
        + pad("目标提升", 10)
        + pad("每组需曝光", 12)
        + pad("每天需曝光", 12)
        + pad("店型", 10)
    )
    print(header)
    print("-" * 78)

    for base_rate, rate_text in BASE_RATES:
        for lift in (0.10, 0.15, 0.30):
            need = required_impressions(base_rate, lift)
            if not need:
                print(pad(rate_text, 8) + pad(f"{lift:.0%}", 10) + "无法计算（基线为 0 或提升过大）")
                continue
            per_day = need / WINDOW_DAYS
            if per_day < 500:
                shop = "微型"
            elif per_day < 2000:
                shop = "小型"
            elif per_day < 10000:
                shop = "中型"
            else:
                shop = "大型"
            print(
                pad(rate_text, 8)
                + pad(f"{lift:.0%}", 10)
                + pad(f"{need:,}", 12)
                + pad(f"{per_day:,.0f}", 12)
                + pad(shop, 10)
            )
        print("-" * 78)


def main() -> int:
    print()
    print("实验判定器 · 蒙特卡洛标定")
    print("不需要真实店铺：造已知答案的模拟实验，再让 judge() 去判。")
    part_one()
    part_two()
    print()
    print("读法：误报率 = 真没效果却宣布成立；检出率 = 真有效果且判对；")
    print("     样本不足 = 数据不够，系统拒绝下结论（这是诚实，不是失败）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
