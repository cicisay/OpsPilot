import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.console.insight import (  # noqa: E402
    CHECK_KEYS,
    attach_verdicts,
    build_checkup,
    build_experiment_board,
    build_memory,
    build_metrics,
    replay_snapshot,
)


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )


class TempTenant(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "memory").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()


class CheckupTests(TempTenant):
    def test_missing_file_reports_unavailable_instead_of_raising(self):
        result = build_checkup(self.dir)
        self.assertFalse(result["available"])
        self.assertEqual(result["categories"], [])
        self.assertEqual(result["health"]["score"], -1)

    def test_categories_count_selected_items_and_mark_weak_ones(self):
        write_json(
            self.dir / "task_orders.json",
            {
                "run_id": "r1",
                "generated_at": "2026-11-03T10:00:00",
                "planner": "llm",
                "scanned": 50,
                "matched": 3,
                "store_health": {
                    "score": 64,
                    "summary": "订单率偏低",
                    "top_problem": "流量未转化",
                    "weak_categories": ["home-kitchen", "outdoor-garden"],
                },
                "quota": {"value": 3, "low": 3, "high": 5, "requested": 3, "guarded": False, "reason": "within bounds"},
                "notes": ["扫描 50 个"],
                "selected": [
                    {"product_id": "p1", "category": "home-kitchen", "problem": "lid", "problem_label": "盖型错发", "variant_reason": "价格带窄，出 2 版"},
                    {"product_id": "p2", "category": "home-kitchen", "problem": "lid", "problem_label": "盖型错发"},
                    {"product_id": "p3", "category": "packaging-printing", "problem": "damage", "problem_label": "破损"},
                ],
            },
        )
        result = build_checkup(self.dir)
        self.assertTrue(result["available"])
        self.assertEqual(result["scanned"], 50)
        self.assertEqual(result["matched"], 3)
        self.assertEqual(result["health"]["score"], 64)
        by_name = {item["category"]: item for item in result["categories"]}
        self.assertEqual(by_name["home-kitchen"]["count"], 2)
        self.assertTrue(by_name["home-kitchen"]["weak"])
        self.assertFalse(by_name["packaging-printing"]["weak"])
        # A weak category with nothing selected this week is still listed.
        self.assertEqual(by_name["outdoor-garden"]["count"], 0)
        self.assertEqual(by_name["home-kitchen"]["problems"][0]["count"], 2)
        self.assertEqual(result["reasons"], ["价格带窄，出 2 版"])

    def test_freshness_grades_by_age(self):
        """体检分是历史数字算出来的，面板上必须能看出数据有多旧。"""
        cases = [(0, "fresh"), (3, "fresh"), (20, "warn"), (45, "danger")]
        for age, level in cases:
            as_of = (date.today() - timedelta(days=age)).isoformat()
            write_json(
                self.dir / "task_orders.json",
                {
                    "data_as_of": as_of,
                    "data_source": "snapshot",
                    "store_health": {"score": 58},
                    "selected": [],
                },
            )
            fresh = build_checkup(self.dir)["freshness"]
            self.assertEqual(fresh["level"], level, f"age={age}")
            self.assertEqual(fresh["age_days"], age)
            self.assertEqual(fresh["as_of"], as_of)
            self.assertIn("数据截至" if age else "数据为今天", fresh["label"])

    def test_freshness_absent_on_old_task_file(self):
        write_json(self.dir / "task_orders.json", {"store_health": {"score": 58}})
        fresh = build_checkup(self.dir)["freshness"]
        self.assertFalse(fresh["available"])
        self.assertEqual(fresh["level"], "unknown")
        self.assertIn("日期未知", fresh["label"])

    def test_live_source_is_labelled_live(self):
        write_json(
            self.dir / "task_orders.json",
            {
                "data_as_of": date.today().isoformat(),
                "data_source": "live",
                "store_health": {"score": 61},
            },
        )
        fresh = build_checkup(self.dir)["freshness"]
        self.assertEqual(fresh["level"], "fresh")
        self.assertIn("实时", fresh["label"])

    def test_corrupt_task_file_is_not_fatal(self):
        (self.dir / "task_orders.json").write_text("{not json", encoding="utf-8")
        result = build_checkup(self.dir)
        self.assertFalse(result["available"])


class ExperimentBoardTests(TempTenant):
    def _record(self, **overrides) -> dict:
        record = {
            "experiment_id": "p1:v1:2026-10-10",
            "product_id": "p1",
            "variant_key": "v1",
            "metric": "order_rate",
            "min_lift": 0.15,
            "window_days": 14,
            "started_at": "2026-10-10",
            "status": "observing",
            "verdict": "",
            "baseline": {"impressions": 1000, "orders": 2, "order_rate": 0.002},
            "outcome": {},
        }
        record.update(overrides)
        return record

    def test_split_between_observing_and_judged(self):
        write_jsonl(
            self.dir / "memory" / "experiments.jsonl",
            [
                self._record(),
                self._record(
                    experiment_id="p2:v2:2026-10-01",
                    product_id="p2",
                    variant_key="v2",
                    started_at="2026-10-01",
                    status="verdict",
                    verdict="inconclusive",
                    verdict_cn="不确定",
                    outcome={
                        "treated": {"order_rate": 0.0024, "impressions": 9000},
                        "sample": {"days": 14, "impressions": 9000, "clicks": 500, "orders": 21},
                        "lift": 0.2,
                        "p_value": 0.4,
                        "note": "提升未达目标",
                    },
                ),
            ],
        )
        board = build_experiment_board(self.dir, today=date(2026, 10, 17))
        self.assertEqual(board["summary"]["observing"], 1)
        self.assertEqual(board["summary"]["judged"], 1)
        self.assertEqual(board["observing"][0]["days_left"], 7)
        judged = board["judged"][0]
        self.assertEqual(judged["verdict_cn"], "不确定")
        self.assertAlmostEqual(judged["baseline"], 0.002)
        self.assertAlmostEqual(judged["current"], 0.0024)
        # 10-01 + 14 days = 10-15, so by the 17th it is past its window.
        self.assertTrue(judged["overdue"])
        self.assertEqual(judged["ends_at"], "2026-10-15")

    def test_days_left_is_recomputed_not_reused_from_stored_outcome(self):
        # The stored window says 9 days left, but today is past the end date.
        write_jsonl(
            self.dir / "memory" / "experiments.jsonl",
            [
                self._record(
                    started_at="2026-10-01",
                    window_days=14,
                    outcome={"window": {"days_left": 9, "ends_at": "2026-10-15"}},
                )
            ],
        )
        board = build_experiment_board(self.dir, today=date(2026, 10, 20))
        item = board["observing"][0]
        self.assertEqual(item["days_left"], 0)
        self.assertTrue(item["overdue"])
        self.assertEqual(item["ends_at"], "2026-10-15")

    def test_metric_follows_the_record_not_the_default(self):
        write_jsonl(
            self.dir / "memory" / "experiments.jsonl",
            [
                self._record(
                    metric="click_rate",
                    baseline={"impressions": 1000, "clicks": 50, "click_rate": 0.05, "order_rate": 0.001},
                    outcome={"treated": {"click_rate": 0.06, "order_rate": 0.002}},
                )
            ],
        )
        board = build_experiment_board(self.dir, today=date(2026, 10, 12))
        item = board["observing"][0]
        self.assertEqual(item["metric"], "click_rate")
        self.assertEqual(item["metric_label"], "点击率")
        self.assertAlmostEqual(item["baseline"], 0.05)
        self.assertAlmostEqual(item["current"], 0.06)

    def test_missing_start_date_does_not_crash(self):
        write_jsonl(
            self.dir / "memory" / "experiments.jsonl",
            [self._record(started_at="not-a-date")],
        )
        board = build_experiment_board(self.dir, today=date(2026, 10, 12))
        self.assertIsNone(board["observing"][0]["days_left"])
        self.assertEqual(board["observing"][0]["ends_at"], "")


class MetricTests(TempTenant):
    def _seed(self, *, block: int, code: int, planned: int, produced: int) -> None:
        write_json(
            self.dir / "task_orders.json",
            {
                "matched": 2,
                "selected": [
                    {"product_id": "p1", "variant_max": planned // 2},
                    {"product_id": "p2", "variant_max": planned - planned // 2},
                ],
            },
        )
        results = []
        for index in range(produced):
            severity = "block" if index < block else "pass"
            results.append(
                {
                    "product_id": f"p{index}",
                    "variant_key": f"v{index}",
                    "severity": severity,
                    "overridden_by": "code" if index < code else "",
                }
            )
        write_json(self.dir / "review_results.json", {"results": results})

    def test_completion_and_block_rate(self):
        self._seed(block=2, code=0, planned=10, produced=10)
        metrics = {item["key"]: item for item in build_metrics(self.dir)["metrics"]}
        self.assertEqual(metrics["completion"]["display"], "80%")
        self.assertEqual(metrics["block_rate"]["display"], "20%")
        self.assertEqual(metrics["completion"]["tone"], "ok")
        self.assertEqual(metrics["block_rate"]["tone"], "ok")

    def test_completion_below_target_is_flagged(self):
        self._seed(block=5, code=0, planned=10, produced=10)
        metrics = {item["key"]: item for item in build_metrics(self.dir)["metrics"]}
        self.assertEqual(metrics["completion"]["display"], "50%")
        self.assertEqual(metrics["completion"]["tone"], "warn")
        self.assertEqual(metrics["block_rate"]["display"], "50%")
        self.assertEqual(metrics["block_rate"]["tone"], "warn")

    def test_code_override_is_counted_apart_from_block(self):
        self._seed(block=1, code=1, planned=2, produced=4)
        payload = build_metrics(self.dir)
        metrics = {item["key"]: item for item in payload["metrics"]}
        self.assertEqual(metrics["code_override"]["display"], "1")
        self.assertEqual(metrics["code_override"]["tone"], "warn")
        self.assertEqual(payload["severity"]["block"], 1)
        self.assertTrue(any("代码覆盖否决" in text for text in payload["alerts"]))

    def test_quota_trim_rate_reads_decisions(self):
        write_jsonl(
            self.dir / "memory" / "decisions.jsonl",
            [
                {"decision_id": "d1", "quota": {"guarded": True}},
                {"decision_id": "d2", "quota": {"guarded": False}},
                {"decision_id": "d3", "quota": {"guarded": False}},
                {"decision_id": "d4", "quota": {"guarded": False}},
            ],
        )
        metrics = {item["key"]: item for item in build_metrics(self.dir)["metrics"]}
        self.assertEqual(metrics["quota_trim"]["display"], "25%")
        self.assertEqual(metrics["quota_trim"]["tone"], "warn")

    def test_verdict_rate_counts_only_due_experiments(self):
        write_jsonl(
            self.dir / "memory" / "experiments.jsonl",
            [
                {"experiment_id": "a", "started_at": "2026-10-01", "window_days": 14, "verdict": "supported"},
                {"experiment_id": "b", "started_at": "2026-10-01", "window_days": 14, "verdict": ""},
                # Still inside its window at the as-of date, so it must not count as overdue.
                {"experiment_id": "c", "started_at": "2026-10-25", "window_days": 14, "verdict": ""},
            ],
        )
        metrics = {item["key"]: item for item in build_metrics(self.dir, today=date(2026, 11, 3))["metrics"]}
        self.assertEqual(metrics["verdict_rate"]["display"], "50%")
        self.assertEqual(metrics["verdict_rate"]["tone"], "warn")
        self.assertIn("1/2", metrics["verdict_rate"]["note"])

    def test_calls_per_item_uses_only_the_two_per_product_agents(self):
        trace_dir = self.dir / "traces"
        trace_dir.mkdir()
        rows = [
            {"agent": "director", "ok": True, "prompt_tokens": 10, "completion_tokens": 5},
            {"agent": "workshop", "ok": True, "prompt_tokens": 10, "completion_tokens": 5},
            {"agent": "workshop", "ok": False, "prompt_tokens": 10, "completion_tokens": 0},
            {"agent": "compliance", "ok": True, "prompt_tokens": 10, "completion_tokens": 5},
            {"agent": "compliance", "ok": True, "prompt_tokens": 10, "completion_tokens": 5},
        ]
        write_jsonl(trace_dir / "run1.jsonl", rows)
        self._seed(block=0, code=0, planned=4, produced=4)
        payload = build_metrics(self.dir, trace_dir=trace_dir)
        metrics = {item["key"]: item for item in payload["metrics"]}
        # 4 workshop+compliance calls over 2 matched items.
        self.assertEqual(metrics["calls_per_item"]["display"], "2")
        self.assertEqual(payload["trace"]["failures"], 1)
        self.assertEqual(payload["trace"]["by_agent"]["workshop"]["calls"], 2)

    def test_no_trace_dir_shows_a_dash_instead_of_zero(self):
        self._seed(block=0, code=0, planned=2, produced=2)
        metrics = {item["key"]: item for item in build_metrics(self.dir)["metrics"]}
        self.assertEqual(metrics["calls_per_item"]["display"], "—")
        self.assertIn("没有", metrics["calls_per_item"]["note"])


class MemoryTests(TempTenant):
    def test_browser_lists_profile_decisions_lessons_and_snapshots(self):
        write_json(self.dir / "memory" / "store_profile.json", {"store_slug": "s1", "active_products": 50})
        write_jsonl(self.dir / "memory" / "decisions.jsonl", [{"decision_id": "d1", "run_id": "r1"}])
        write_json(
            self.dir / "memory" / "lessons.json",
            {"lessons": [{"lesson_id": "l1", "scope": "store", "statement": "别在样本不足时下结论"}]},
        )
        write_json(
            self.dir / "memory" / "snapshots" / "r1.json",
            {
                "run_id": "r1",
                "saved_at": "2026-11-03T11:00:00",
                "store_profile": {"store_slug": "s1"},
                "experiments": [{"experiment_id": "e1"}],
                "decisions": [{"decision_id": "d1"}],
                "lessons": [{"lesson_id": "l1"}],
            },
        )
        result = build_memory(self.dir)
        self.assertEqual(result["profile"]["store_slug"], "s1")
        self.assertEqual(len(result["decisions"]), 1)
        self.assertEqual(len(result["lessons"]), 1)
        self.assertEqual(result["snapshots"][0]["run_id"], "r1")
        self.assertEqual(result["snapshots"][0]["experiments"], 1)
        self.assertEqual(result["health"]["decisions"], 1)

    def test_replay_returns_the_exact_snapshot(self):
        snapshot = {
            "run_id": "20261103-110000-abcd",
            "saved_at": "2026-11-03T11:00:00",
            "store_profile": {"store_slug": "s1"},
            "experiments": [{"experiment_id": "e1"}],
            "decisions": [{"decision_id": "d1"}, {"decision_id": "d2"}],
            "lessons": [],
        }
        write_json(self.dir / "memory" / "snapshots" / "20261103-110000-abcd.json", snapshot)
        result = replay_snapshot(self.dir, snapshot["run_id"])
        self.assertEqual(result["run_id"], snapshot["run_id"])
        self.assertEqual(len(result["decisions"]), 2)

    def test_replay_rejects_traversal_and_bad_ids(self):
        for bad in ("..", "../secret", "a/../b", ".hidden", "", "x" * 80, "run id"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    replay_snapshot(self.dir, bad)

    def test_replay_missing_snapshot_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            replay_snapshot(self.dir, "20990101-000000-0000")


class ComplianceMatrixTests(unittest.TestCase):
    def _proposal(self):
        return {
            "product_id": "p1",
            "title": "T",
            "variants": [
                {"variant_key": "p1-v1", "angle": "A"},
                {"variant_key": "p1-v2", "angle": "B"},
                {"variant_key": "p1-v3", "angle": "C"},
            ],
        }

    def _reviews(self):
        checks_pass = {key: "pass" for key in CHECK_KEYS}
        return [
            {
                "product_id": "p1",
                "variant_key": "p1-v1",
                "variant_id": "p1-v1",
                "checks": checks_pass,
                "severity": "pass",
                "findings": [],
                "overridden_by": "",
                "headline": "全通过",
            },
            {
                "product_id": "p1",
                "variant_id": "p1-v2",
                "checks": {**checks_pass, "c3_category": "warn"},
                "severity": "warn",
                "findings": [
                    {
                        "category": "c3_category",
                        "quote": "FCC-ready",
                        "rule": "没有认证文件",
                        "suggestion": "人工确认",
                    }
                ],
                "overridden_by": "",
            },
            {
                "product_id": "p1",
                "variant_key": "p1-v3",
                "checks": {**checks_pass, "c2_ip": "block"},
                "severity": "block",
                "findings": [{"category": "c2_ip", "quote": "Nike", "rule": "未授权品牌", "suggestion": "删除"}],
                "overridden_by": "code",
            },
        ]

    def test_indexes_by_both_id_spellings_and_summarizes(self):
        output = attach_verdicts([self._proposal()], self._reviews())
        variants = output[0]["variants"]
        self.assertEqual(variants[1]["compliance"]["severity"], "warn", "variant_id-only verdict must still match")
        self.assertEqual(variants[0]["compliance"]["checks"]["c1_absolute"], "pass")
        self.assertEqual(output[0]["compliance_summary"]["pass"], 1)
        self.assertEqual(output[0]["compliance_summary"]["warn"], 1)
        self.assertEqual(output[0]["compliance_summary"]["block"], 1)
        self.assertEqual(output[0]["compliance_summary"]["approvable"], 2)

    def test_blocked_and_unreviewed_variants_are_not_approvable(self):
        output = attach_verdicts([self._proposal()], self._reviews())
        variants = output[0]["variants"]
        self.assertTrue(variants[0]["approvable"])
        self.assertTrue(variants[1]["approvable"])
        self.assertFalse(variants[2]["approvable"])

        unreviewed = attach_verdicts([self._proposal()], [])
        self.assertTrue(all(not item["approvable"] for item in unreviewed[0]["variants"]))
        self.assertEqual(unreviewed[0]["compliance_summary"]["missing"], 3)
        self.assertFalse(unreviewed[0]["variants"][0]["compliance"]["checked"])

    def test_missing_check_class_is_marked_not_defaulted_to_pass(self):
        reviews = [
            {
                "product_id": "p1",
                "variant_key": "p1-v1",
                "checks": {"c1_absolute": "pass", "c2_ip": "pass"},
                "severity": "pass",
            }
        ]
        output = attach_verdicts([self._proposal()], reviews)
        checks = output[0]["variants"][0]["compliance"]["checks"]
        self.assertEqual(checks["c3_category"], "missing")
        self.assertEqual(checks["c4_market"], "missing")

    def test_findings_are_normalized_and_bounded(self):
        reviews = self._reviews()
        reviews[2]["findings"] = reviews[2]["findings"] * 10
        output = attach_verdicts([self._proposal()], reviews)
        findings = output[0]["variants"][2]["compliance"]["findings"]
        self.assertEqual(len(findings), 6)
        self.assertEqual(findings[0]["category"], "c2_ip")
        self.assertEqual(findings[0]["suggestion"], "删除")

    def test_does_not_mutate_the_source_proposals(self):
        proposals = [self._proposal()]
        attach_verdicts(proposals, self._reviews())
        self.assertNotIn("compliance", proposals[0]["variants"][0])
        self.assertNotIn("variants", proposals[0].get("variants", [{}])[0])


if __name__ == "__main__":
    unittest.main()
