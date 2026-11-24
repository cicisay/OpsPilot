import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.experiment import (  # noqa: E402
    DailyMetric,
    apply_judgement,
    baseline_for,
    build_record,
    clamp_window,
    experiment_id_for,
    is_due,
    judge,
    metric_of,
    parse_day,
    required_impressions,
    summary,
    to_daily,
)


def series(start: date, days: int, impressions: int, orders: int, clicks: int | None = None) -> list[DailyMetric]:
    """Build a flat daily series with exact totals."""
    clicks = impressions // 20 if clicks is None else clicks
    per_imp = impressions // days
    per_clk = clicks // days
    per_ord = orders // days
    rows = []
    for index in range(days):
        rows.append(
            DailyMetric(
                day=start + timedelta(days=index),
                impressions=per_imp + (1 if index < impressions % days else 0),
                clicks=per_clk + (1 if index < clicks % days else 0),
                orders=per_ord + (1 if index < orders % days else 0),
            )
        )
    return rows


def record(**overrides) -> dict:
    base = build_record(
        product_id="p1",
        variant_id="p1:v1",
        variant_key="p1:v1",
        task={
            "task_id": "run:p1",
            "success_metric": "order_rate_pct从当前0.14%提升至至少0.18%",
            "min_lift": 0.25,
            "window_days": 14,
            "category": "home-kitchen",
            "hypothesis": "把盖型写进标题可减少错发",
        },
        started_at="2026-10-01",
        baseline={"days": 28, "impressions": 2800, "clicks": 140, "orders": 4,
                  "order_rate": 0.001429, "click_rate": 0.05, "return_rate": 0.0},
    )
    base.update(overrides)
    return base


class WindowTests(unittest.TestCase):
    def test_parse_day_accepts_three_shapes(self):
        self.assertEqual(parse_day("2026-10-01"), date(2026, 10, 1))
        self.assertEqual(parse_day("2026-10-01T09:30:00"), date(2026, 10, 1))
        self.assertEqual(parse_day(date(2026, 10, 1)), date(2026, 10, 1))
        self.assertEqual(parse_day(datetime(2026, 10, 1, 9, 30)), date(2026, 10, 1))
        self.assertIsNone(parse_day(""))
        self.assertIsNone(parse_day("not-a-date"))

    def test_window_is_clamped(self):
        self.assertEqual(clamp_window(0), 1)
        self.assertEqual(clamp_window(-5), 1)
        self.assertEqual(clamp_window(999), 90)
        self.assertEqual(clamp_window(None), 14)
        self.assertEqual(clamp_window("bad"), 14)
        self.assertEqual(clamp_window("21"), 21)

    def test_metric_is_parsed_not_demanded(self):
        self.assertEqual(metric_of("order_rate_pct从当前0.07%提升")[0], "order_rate")
        metric, note = metric_of("inquiry_rate_pct从当前0.04%提升")
        self.assertEqual(metric, "click_rate")
        self.assertIn("代理", note)
        self.assertEqual(metric_of("ctr")[0], "click_rate")
        self.assertEqual(metric_of("")[0], "order_rate")

    def test_chinese_metric_names_are_parsed(self):
        # 提示词要的是中文指标名，词表只认英文时"点击率"会掉进 order_rate，
        # 而订单率约万分之七，那个口径永远判不出结论。
        self.assertEqual(metric_of("点击率从 6.3% 提升至 7.2%")[0], "click_rate")
        self.assertEqual(metric_of("点击率")[0], "click_rate")
        self.assertEqual(metric_of("点击量提升 15%")[0], "click_rate")
        metric, note = metric_of("询盘率提升 30%")
        self.assertEqual(metric, "click_rate")
        self.assertIn("代理", note)
        self.assertEqual(metric_of("订单率从 0.07% 提升至 0.08%")[0], "order_rate")
        self.assertEqual(metric_of("转化率提升")[0], "order_rate")

    def test_first_named_metric_wins(self):
        # 一句话里出现两个指标时，按出现位置取主指标，而不是按词表顺序。
        self.assertEqual(metric_of("点击率提升，订单率随后")[0], "click_rate")
        self.assertEqual(metric_of("订单率提升，点击率随后")[0], "order_rate")

    def test_english_hint_does_not_match_inside_a_word(self):
        # electric 里含 ctr，商品名不能把后面的中文指标顶掉。
        self.assertEqual(metric_of("Electric Kettle 订单率提升到 0.1%")[0], "order_rate")
        self.assertEqual(metric_of("electric 点击率提升")[0], "click_rate")

    def test_experiment_id_matches_prd(self):
        self.assertEqual(
            experiment_id_for("p1", "p1:v1", "2026-10-01T08:00:00"),
            "p1:p1:v1:2026-10-01",
        )

    def test_baseline_only_looks_backwards(self):
        rows = series(date(2026, 9, 1), 60, 6000, 6)
        base = baseline_for(rows, "2026-10-01", days=28)
        self.assertEqual(base["days"], 28)
        self.assertEqual(base["from"], "2026-09-03")
        self.assertEqual(base["to"], "2026-09-30")
        self.assertEqual(base["impressions"], 2800)


class JudgeTests(unittest.TestCase):
    def test_open_window_reports_progress_only(self):
        rows = series(date(2026, 10, 1), 14, 7000, 35)
        judged = judge(record(), rows, as_of="2026-10-08")
        self.assertEqual(judged["status"], "observing")
        self.assertEqual(judged["verdict"], "")
        self.assertIn("观察中", judged["outcome"]["note"])
        self.assertEqual(judged["outcome"]["window"]["days_left"], 7)

    def test_closed_window_with_thin_sample_is_insufficient(self):
        rows = series(date(2026, 10, 1), 5, 100, 1, clicks=3)
        judged = judge(record(), rows, as_of="2026-10-20")
        self.assertEqual(judged["status"], "verdict")
        self.assertEqual(judged["verdict"], "insufficient")
        self.assertIn("样本不足", judged["outcome"]["note"])

    def test_thin_sample_still_reports_direction(self):
        """样本不够下结论，但涨跌方向要带出来，否则看板只剩一句"没数据"。"""
        rows = series(date(2026, 10, 1), 5, 100, 1, clicks=3)
        judged = judge(record(), rows, as_of="2026-10-20")
        self.assertEqual(judged["verdict"], "insufficient")
        self.assertTrue(judged["outcome"].get("provisional"))
        self.assertIn("lift", judged["outcome"])

    def test_click_rate_without_click_counter_falls_back_to_order_rate(self):
        """站点只有 pv/询盘/订单，没有点击计数。

        基线 clicks=0 时若仍按点击率算，会得到 0/基线的 -100% 假暴跌；
        应自动降到订单率，并在 outcome 里说明为什么换指标。
        """
        rec = record(
            metric="click_rate",
            baseline={"days": 30, "impressions": 3002, "clicks": 0, "orders": 5,
                      "order_rate": 0.001666, "click_rate": 0.0, "return_rate": 0.0,
                      "source": "site_snapshot"},
        )
        rows = series(date(2026, 10, 1), 14, 4200, 14)
        judged = judge(rec, rows, as_of="2026-10-20")
        outcome = judged["outcome"]
        self.assertEqual(outcome["metric"], "order_rate")
        self.assertIn("无点击计数", outcome["proxy"])
        self.assertNotEqual(outcome.get("lift"), -1.0)
        self.assertGreater(outcome.get("lift"), 0)

    def test_clear_lift_is_supported(self):
        rows = series(date(2026, 10, 1), 14, 7000, 35)
        judged = judge(record(), rows, as_of="2026-10-20")
        self.assertEqual(judged["verdict"], "supported")
        self.assertLess(judged["outcome"]["p_value"], 0.05)
        self.assertGreater(judged["outcome"]["lift"], 0.25)

    def test_clear_drop_is_rejected(self):
        rows = series(date(2026, 10, 1), 14, 7000, 2)
        judged = judge(record(), rows, as_of="2026-10-20")
        self.assertEqual(judged["verdict"], "rejected")
        self.assertLess(judged["outcome"]["lift"], 0)

    def test_small_drop_is_inconclusive(self):
        rows = series(date(2026, 10, 1), 14, 7000, 5)
        judged = judge(record(), rows, as_of="2026-10-20")
        self.assertEqual(judged["verdict"], "inconclusive")

    def test_zero_baseline_with_sales_counts_as_supported(self):
        rec = record(baseline={"days": 28, "impressions": 2800, "clicks": 140, "orders": 0,
                               "order_rate": 0.0, "click_rate": 0.05, "return_rate": 0.0})
        judged = judge(rec, series(date(2026, 10, 1), 14, 7000, 35), as_of="2026-10-20")
        self.assertEqual(judged["verdict"], "supported")
        self.assertIn("基线无订单", judged["outcome"]["note"])

    def test_zero_baseline_without_sales_is_inconclusive(self):
        rec = record(baseline={"days": 28, "impressions": 2800, "clicks": 140, "orders": 0,
                               "order_rate": 0.0, "click_rate": 0.05, "return_rate": 0.0})
        judged = judge(rec, series(date(2026, 10, 1), 14, 7000, 0), as_of="2026-10-20")
        # No orders means the click sample is still fine, but there is no signal.
        self.assertEqual(judged["verdict"], "inconclusive")

    def test_missing_baseline_is_insufficient_not_guessed(self):
        rec = record(baseline={})
        judged = judge(rec, series(date(2026, 10, 1), 14, 7000, 35), as_of="2026-10-20")
        self.assertEqual(judged["verdict"], "insufficient")

    def test_click_rate_experiment_is_judged_on_clicks(self):
        """`metric` used to be written to the record and ignored by the judge."""
        rows = series(date(2026, 10, 1), 14, 7000, 4, 700)
        judged = judge(record(metric="click_rate", min_lift=0.2), rows, as_of="2026-10-20")
        self.assertEqual(judged["verdict"], "supported")
        self.assertAlmostEqual(judged["outcome"]["lift"], 1.0, places=3)
        self.assertEqual(judged["outcome"]["metric"], "click_rate")

    def test_order_rate_experiment_ignores_the_click_surge(self):
        rows = series(date(2026, 10, 1), 14, 7000, 4, 700)
        judged = judge(record(metric="order_rate", min_lift=0.2), rows, as_of="2026-10-20")
        self.assertNotEqual(judged["verdict"], "supported")

    def test_order_rate_experiment_is_not_starved_by_zero_clicks(self):
        """Orders can arrive without a recorded click; the click gate is per-metric."""
        rows = series(date(2026, 10, 1), 14, 7000, 35, 0)
        judged = judge(record(metric="order_rate"), rows, as_of="2026-10-20")
        self.assertNotEqual(judged["verdict"], "insufficient")

    def test_unknown_metric_is_inconclusive_not_guessed(self):
        judged = judge(
            record(metric="refund_speed"),
            series(date(2026, 10, 1), 14, 7000, 35),
            as_of="2026-10-20",
        )
        self.assertEqual(judged["verdict"], "inconclusive")
        self.assertIn("没有可计算的口径", judged["outcome"]["note"])

    def test_missing_min_lift_never_yields_supported(self):
        """`lift >= 0 and p < alpha` would call any noise a success."""
        rows = series(date(2026, 10, 1), 14, 7000, 35)
        judged = judge(record(min_lift=0), rows, as_of="2026-10-20")
        self.assertEqual(judged["verdict"], "inconclusive")
        self.assertIn("未设最小提升", judged["outcome"]["note"])

    def test_rows_after_window_are_ignored(self):
        rows = series(date(2026, 10, 1), 30, 15000, 75)
        judged = judge(record(), rows, as_of="2026-11-10")
        # Window is 14 days, so only half of the series may be counted.
        self.assertEqual(judged["outcome"]["treated"]["impressions"], 7000)

    def test_bad_started_at_does_not_crash(self):
        rec = record(started_at="broken")
        judged = judge(rec, [], as_of="2026-10-20")
        self.assertEqual(judged["verdict"], "insufficient")
        self.assertIn("不可解析", judged["outcome"]["note"])

    def test_apply_judgement_keeps_history(self):
        judged = judge(record(), series(date(2026, 10, 1), 14, 7000, 35), as_of="2026-10-20")
        updated = apply_judgement(record(), judged)
        self.assertEqual(updated["status"], "verdict")
        self.assertEqual(updated["verdict_cn"], "成立")
        self.assertIn("judged_at", updated)
        self.assertEqual(updated["baseline"]["orders"], 4)

    def test_is_due(self):
        rec = record()
        self.assertFalse(is_due(rec, as_of="2026-10-14"))
        self.assertTrue(is_due(rec, as_of="2026-10-15"))
        self.assertTrue(is_due(record(started_at="bad")))

    def test_summary_counts(self):
        items = [
            {"status": "observing"},
            {"status": "verdict", "verdict": "supported"},
            {"status": "verdict", "verdict": "insufficient"},
        ]
        counts = summary(items)
        self.assertEqual(counts["total"], 3)
        self.assertEqual(counts["status"]["observing"], 1)
        self.assertEqual(counts["verdicts"]["supported"], 1)


class PowerTests(unittest.TestCase):
    def test_low_base_rate_needs_far_more_traffic(self):
        # 0.24% vs 0.276% is a 15% lift on a tiny base: hundreds of thousands.
        orders = required_impressions(0.0024, 0.15)
        clicks = required_impressions(0.05, 0.15)
        self.assertGreater(orders, 200_000)
        self.assertLess(clicks, 30_000)
        self.assertGreater(orders, clicks)

    def test_undefined_inputs(self):
        self.assertIsNone(required_impressions(0.0, 0.15))
        self.assertIsNone(required_impressions(0.0024, 0.0))
        self.assertIsNone(required_impressions(0.9, 1.0))

    def test_judgement_reports_the_gap(self):
        rows = series(date(2026, 10, 1), 14, 7000, 5)
        judged = judge(record(), rows, as_of="2026-10-20")
        self.assertIn("required_impressions", judged["outcome"])
        self.assertGreater(judged["outcome"]["required_impressions"], 7000)


class RecordTests(unittest.TestCase):
    def test_record_carries_what_judging_needs(self):
        rec = record()
        self.assertEqual(rec["experiment_id"], "p1:p1:v1:2026-10-01")
        self.assertEqual(rec["metric"], "order_rate")
        self.assertEqual(rec["window_days"], 14)
        self.assertEqual(rec["status"], "observing")
        self.assertEqual(rec["baseline"]["orders"], 4)
        self.assertEqual(rec["category"], "home-kitchen")

    def test_to_daily_accepts_objects_and_dicts(self):
        class Row:
            day = date(2026, 10, 1)
            impressions = 10
            clicks = 1
            orders = 0
            returns = 0

        rows = to_daily([Row(), {"day": "2026-10-02", "impressions": 20, "clicks": 2, "orders": 1}])
        self.assertEqual([row.day.day for row in rows], [1, 2])
        self.assertEqual(rows[1].orders, 1)

    def test_to_daily_skips_unparsable_rows(self):
        self.assertEqual(to_daily([{"day": "??"}, None]), [])


if __name__ == "__main__":
    unittest.main()
