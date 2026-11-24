"""Step 8 端到端与评测的契约测试。

这里测的是「串起来以后还对不对」，不是单模块行为：
- 演示脚本一次跑通，轨迹里有意图、计划、风险、记忆、草稿、收口；
- 该阻断的照样阻断，该澄清的照样停下，不能为了演示好看放过去；
- 跑完 `data/` 指纹不变（脚本只读）；
- 评测的记忆 60 组 / 端到端 10 个任务都按口径算，报告落盘位置不再弄脏工作区。
"""

import contextlib
import importlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import demo_intake  # noqa: E402
import eval_intake  # noqa: E402

DATA_DIR = ROOT / "data"
MAIN_TEXT = "这 20 个新品上到美国站"


def data_fingerprint() -> dict:
    """`data/` 下每个文件的大小 + mtime。只读脚本不该让这里有任何一个字节变化。"""

    items = {}
    if not DATA_DIR.exists():
        return items
    for path in sorted(DATA_DIR.rglob("*")):
        if path.is_file():
            stat = path.stat()
            items[str(path.relative_to(DATA_DIR))] = (stat.st_size, stat.st_mtime_ns)
    return items


def run_main(module, argv):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = module.main(argv)
    return code, buffer.getvalue()


class TestDemoPipeline(unittest.TestCase):
    """演示脚本的主链路。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.trace = demo_intake.run_pipeline(MAIN_TEXT)

    def test_stage_order(self) -> None:
        self.assertEqual(
            ["intent", "plan", "clarify", "risk", "memory", "ingest", "close"],
            self.trace["stages"],
        )

    def test_intent_and_slots(self) -> None:
        intent = self.trace["intent"]
        self.assertEqual("BATCH_UPLOAD", intent["name"])
        self.assertEqual("US", intent["slots"]["market"])
        self.assertIn("source_path", intent["missing"])

    def test_drafts_cover_three_statuses(self) -> None:
        summary = self.trace["summary"]
        self.assertEqual(3, summary["drafts"])
        self.assertEqual(1, summary["ready"])
        self.assertEqual(1, summary["needs_fill"])
        self.assertEqual(1, summary["blocked"])
        self.assertEqual(["FT-001"], summary["ready_skus"])
        self.assertEqual(["FT-003"], summary["blocked_skus"])

    def test_plan_and_drafts_share_the_same_csv_skus(self) -> None:
        order_ids = {order["product_id"] for order in self.trace["orders"]}
        draft_ids = {draft["sku"] for draft in self.trace["drafts"]}
        self.assertEqual({"FT-001", "FT-003"}, order_ids)
        self.assertEqual({"FT-001", "FT-002", "FT-003"}, draft_ids)
        self.assertTrue(order_ids.issubset(draft_ids))

    def test_blocked_draft_keeps_compliance_evidence(self) -> None:
        blocked = next(item for item in self.trace["drafts"] if item["status"] == "blocked")
        self.assertEqual("FT-003", blocked["sku"])
        violations = blocked["compliance"]["violations"]
        self.assertTrue(violations)
        self.assertEqual("BRD-001", violations[0]["rule_id"])

    def test_blocked_is_not_silently_dropped(self) -> None:
        # 阻断的草稿仍在结果里，只是不能进工坊；"消失"和"阻断"是两回事。
        self.assertTrue(any(item["status"] == "blocked" for item in self.trace["drafts"]))
        self.assertTrue(self.trace["summary"]["blocked_skus"])

    def test_missing_price_is_not_invented(self) -> None:
        needs_fill = next(item for item in self.trace["drafts"] if item["status"] == "needs_fill")
        self.assertEqual("FT-002", needs_fill["sku"])
        self.assertEqual(0, needs_fill["price_cents"])
        self.assertIn("price", needs_fill["missing"])

    def test_clarifications_are_resolved_explicitly(self) -> None:
        slots = {item["slot"]: item for item in self.trace["clarifications"]}
        self.assertIn("source_path", slots)
        self.assertTrue(slots["source_path"]["resolved"])
        self.assertIn("language", slots)
        self.assertTrue(slots["language"]["resolved"])

    def test_risk_table_marks_draft_write_as_r1(self) -> None:
        rows = {row["name"]: row for row in self.trace["risk"]["tools"]}
        self.assertEqual("R0", rows["file_parse"]["level"])
        self.assertEqual("R1", rows["listing_draft"]["level"])
        self.assertTrue(rows["listing_draft"]["requires_approval"])
        self.assertTrue(rows["listing_draft"]["visible"])
        self.assertEqual("R1", self.trace["risk"]["ceiling"])

    def test_publish_is_never_in_planned_tools(self) -> None:
        names = {row["name"] for row in self.trace["risk"]["tools"]}
        self.assertNotIn("publish_listing", names)
        self.assertNotIn("rollback", names)

    def test_memory_injected_with_shop_rule_pinned(self) -> None:
        memory = self.trace["memory"]
        self.assertTrue(memory["enabled"])
        self.assertGreaterEqual(memory["count"], 2)
        contents = " ".join(item["content"] for item in memory["facts"])
        self.assertIn("品牌授权", contents)
        self.assertGreaterEqual(memory["pinned"], 1)

    def test_memory_can_be_switched_off(self) -> None:
        trace = demo_intake.run_pipeline(MAIN_TEXT, memory=False)
        self.assertFalse(trace["memory"]["enabled"])
        self.assertEqual(0, trace["memory"]["count"])
        self.assertEqual(0, trace["memory"]["tokens"])

    def test_plan_preview_off_hides_assumptions_but_warns(self) -> None:
        trace = demo_intake.run_pipeline(MAIN_TEXT, plan_preview=False)
        self.assertNotIn("plan", trace["stages"])
        # 值仍然算出来了，只是没给人看——这正是"隐性假设"的含义。
        self.assertTrue(trace["plan"]["assumed"])
        self.assertTrue(any("跳过计划预览" in item for item in trace["warnings"]))
        self.assertEqual(len(self.trace["stages"]) - 1, len(trace["stages"]))

    def test_unknown_text_stops_without_draft(self) -> None:
        trace = demo_intake.run_pipeline("帮我搞一下")
        self.assertEqual("clarify", trace["status"])
        self.assertEqual(0, len(trace["drafts"]))
        self.assertEqual(0, len(trace["orders"]))
        self.assertEqual(1, len(trace["clarifications"]))

    def test_empty_text_is_rejected(self) -> None:
        trace = demo_intake.run_pipeline("   ")
        self.assertEqual("empty", trace["status"])
        self.assertEqual([], trace["stages"])

    def test_single_rewrite_binds_product_from_raw_text(self) -> None:
        trace = demo_intake.run_pipeline("把 FT-001 的标题重写一下")
        self.assertEqual(1, len(trace["orders"]))
        self.assertEqual("FT-001", trace["orders"][0]["product_id"])
        # K-01 修好后，自有编码（FT-001）由意图层直接抽进 slots，
        # 不再需要"意图层没抽到、计划层兜底"的那条澄清。
        self.assertEqual("ok", trace["status"])
        self.assertFalse([item for item in trace["clarifications"] if item["slot"] == "sku"])

    def test_single_upload_without_sku_asks(self) -> None:
        trace = demo_intake.run_pipeline("这款要不要上")
        self.assertEqual("clarify", trace["status"])
        sku_line = next(item for item in trace["clarifications"] if item["slot"] == "sku")
        self.assertFalse(sku_line["resolved"])

    def test_market_conflict_is_surfaced(self) -> None:
        trace = demo_intake.run_pipeline("上 5 个新品到德国站")
        self.assertTrue(trace["market_conflicts"])
        self.assertTrue(any("不一致" in item for item in trace["warnings"]))
        # 冲突只告警，不改状态：资料是权威源，草稿按资料出。
        self.assertTrue(all(draft["market"] == "US" for draft in trace["drafts"]))

    def test_render_has_block_section_and_clarify_section(self) -> None:
        text = demo_intake.render(self.trace)
        self.assertIn("Step 7 上新", text)
        self.assertIn("阻断依据", text)
        self.assertIn("槽位澄清", text)
        self.assertIn("不进工坊", text)

    def test_pipeline_does_not_touch_data_dir(self) -> None:
        before = data_fingerprint()
        demo_intake.run_pipeline(MAIN_TEXT)
        demo_intake.run_pipeline("上 5 个新品到德国站", memory=False)
        self.assertEqual(before, data_fingerprint())

    def test_source_policy_rejects_files_outside_ingest_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "secret.csv"
            outside.write_text("sku,title\nX-1,x\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                demo_intake.resolve_source(outside)

    def test_source_policy_allows_packaged_sample(self) -> None:
        root, relative = demo_intake.resolve_source(demo_intake.SAMPLE_CSV)
        self.assertEqual("sample_supplier.csv", relative)
        self.assertEqual(demo_intake.SAMPLE_CSV.parent.resolve(), root)

    def test_source_policy_allows_data_ingest_path(self) -> None:
        root, relative = demo_intake.resolve_source(demo_intake.INGEST_ROOT / "sub" / "a.csv")
        self.assertEqual(demo_intake.INGEST_ROOT.resolve(), root)
        self.assertEqual("sub/a.csv", relative)

    def test_missing_csv_inside_ingest_root_reports_no_draft(self) -> None:
        trace = demo_intake.run_pipeline(MAIN_TEXT, csv_path=demo_intake.INGEST_ROOT / "missing.csv")
        self.assertEqual("no_draft", trace["status"])
        self.assertEqual([], trace["drafts"])
        self.assertTrue(trace["warnings"])

    def test_unknown_sku_is_not_reported_as_ok(self) -> None:
        trace = demo_intake.run_pipeline("把 SKU-999 的标题翻译成德语")
        self.assertEqual("no_order", trace["status"])
        self.assertEqual([], trace["orders"])
        self.assertTrue(any("不在店铺快照里" in item for item in trace["warnings"]))

    def test_cli_exit_code_reflects_no_order(self) -> None:
        code, output = run_main(demo_intake, ["--text", "把 SKU-999 的标题翻译成德语"])
        self.assertEqual(3, code)
        self.assertIn("no_order", output)


class TestDemoCli(unittest.TestCase):
    def test_json_output_single_text(self) -> None:
        code, output = run_main(demo_intake, ["--text", MAIN_TEXT, "--json"])
        self.assertEqual(0, code)
        trace = json.loads(output)
        self.assertEqual("BATCH_UPLOAD", trace["intent"]["name"])
        self.assertEqual(3, len(trace["drafts"]))

    def test_json_output_multiple_texts(self) -> None:
        code, output = run_main(demo_intake, ["--text", MAIN_TEXT, "--text", "帮我搞一下", "--json"])
        self.assertEqual(0, code)
        traces = json.loads(output)
        self.assertEqual(2, len(traces))
        self.assertEqual("clarify", traces[1]["status"])

    def test_missing_source_returns_2(self) -> None:
        code, output = run_main(demo_intake, ["--text", MAIN_TEXT, "--source", "no-such-file.csv"])
        self.assertEqual(2, code)
        self.assertIn("找不到供应商资料", output)

    def test_default_text_when_none_given(self) -> None:
        code, output = run_main(demo_intake, ["--json"])
        self.assertEqual(0, code)
        self.assertEqual(MAIN_TEXT, json.loads(output)["text"])


class TestMemoryEval(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = eval_intake.evaluate_memory()

    def test_group_counts_match_prd(self) -> None:
        self.assertEqual(60, self.report["groups"])
        self.assertEqual(30, self.report["breakdown"]["recall"]["total"])
        self.assertEqual(15, self.report["breakdown"]["conflict"]["total"])
        self.assertEqual(15, self.report["breakdown"]["forget"]["total"])

    def test_all_groups_pass(self) -> None:
        self.assertEqual([], self.report["failures"])
        self.assertEqual(1.0, self.report["metrics"]["overall"])

    def test_forget_covers_three_kinds(self) -> None:
        by_kind = self.report["breakdown"]["forget"]["by_kind"]
        self.assertEqual({"revoked", "expired", "superseded"}, set(by_kind))
        for kind, bucket in by_kind.items():
            self.assertEqual(5, bucket["total"], kind)
            self.assertEqual(bucket["total"], bucket["ok"], kind)

    def test_deterministic_clock_supported(self) -> None:
        from datetime import datetime

        report = eval_intake.evaluate_memory(now=datetime(2026, 11, 5, 9, 0, 0))
        self.assertEqual(1.0, report["metrics"]["overall"])


class TestE2EEval(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = eval_intake.evaluate_e2e()

    def test_ten_tasks(self) -> None:
        self.assertEqual(10, self.report["tasks"])
        self.assertEqual(10, len(self.report["rows"]))

    def test_all_tasks_pass_invariants(self) -> None:
        self.assertEqual([], self.report["failures"])
        self.assertEqual(1.0, self.report["metrics"]["pass_rate"])

    def test_blocked_and_clarify_present(self) -> None:
        self.assertGreaterEqual(self.report["metrics"]["blocked_total"], 3)
        self.assertGreaterEqual(self.report["metrics"]["clarify_total"], 5)

    def test_quality_findings_are_kept_not_hidden(self) -> None:
        ids = {item["id"] for item in self.report["quality_findings"]}
        # K-02 修好后 E2E-03 从"告警"改成"needs_fill"，备注仍在；E2E-04 的
        # "抽不到 FT-001" 已随 K-01 消失，备注不该再出现。
        self.assertIn("E2E-03", ids)
        self.assertNotIn("E2E-04", ids)

    def test_every_row_carries_actual_counts(self) -> None:
        for row in self.report["rows"]:
            with self.subTest(id=row["id"]):
                self.assertEqual(set(eval_intake.E2E_KEYS), set(row["actual"]))


class TestAblations(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = eval_intake.evaluate_ablations()

    def test_plan_preview_adds_a_stage(self) -> None:
        arms = {arm["arm"]: arm for arm in self.report["plan_preview"]}
        self.assertGreater(arms["with_preview"]["steps_total"], arms["no_preview"]["steps_total"])
        self.assertEqual(0, arms["no_preview"]["visible_assumptions"])
        self.assertGreater(arms["no_preview"]["hidden_assumptions"], 0)

    def test_preview_does_not_change_edit_rate(self) -> None:
        arms = {arm["arm"]: arm for arm in self.report["plan_preview"]}
        self.assertEqual(arms["with_preview"]["edit_rate"], arms["no_preview"]["edit_rate"])

    def test_memory_injects_context_only(self) -> None:
        arms = {arm["arm"]: arm for arm in self.report["memory"]}
        self.assertGreater(arms["with_memory"]["facts_injected"], 0)
        self.assertGreater(arms["with_memory"]["memory_tokens"], 0)
        self.assertGreater(arms["with_memory"]["drafts_with_facts"], 0)
        self.assertEqual(0, arms["no_memory"]["facts_injected"])
        self.assertEqual(0, arms["no_memory"]["memory_tokens"])
        # 现状：记忆不参与槽位补全，所以两组的修改率相同。这个断言是"如实记录"，
        # 不是为了通过而通过——真要改变，得先在流水线里接上记忆补槽。
        self.assertEqual(arms["with_memory"]["edit_rate"], arms["no_memory"]["edit_rate"])


class TestFullReport(unittest.TestCase):
    def test_default_report_paths_stay_out_of_tests(self) -> None:
        self.assertNotIn("tests", str(eval_intake.DEFAULT_REPORT))
        self.assertEqual(ROOT / "data" / "intake" / "eval_report.json", eval_intake.DEFAULT_REPORT)
        self.assertEqual(ROOT / "data" / "intake" / "eval_full_report.json", eval_intake.FULL_REPORT)

    def test_full_report_written_with_all_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "full.json"
            code, output = run_main(eval_intake, ["--full", "--report", str(target), "--top", "1"])
            self.assertEqual(0, code)
            self.assertTrue(target.exists(), output)
            report = json.loads(target.read_text(encoding="utf-8"))
        for key in ("intent", "memory", "e2e", "ablations", "known_issues", "metrics"):
            self.assertIn(key, report)
        # 200 条基础语料 + 回归块（E201 起，词典修复后追加）。
        self.assertGreaterEqual(report["intent"]["metrics"]["total"], 204)
        self.assertEqual(60, report["memory"]["groups"])
        self.assertEqual(10, report["e2e"]["tasks"])
        self.assertEqual(
            {"plan_preview", "memory"},
            set(report["ablations"]),
        )
        self.assertTrue(report["known_issues"])

    def test_intent_only_run_keeps_legacy_report_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "intent.json"
            code, _ = run_main(eval_intake, ["--report", str(target)])
            self.assertEqual(0, code)
            report = json.loads(target.read_text(encoding="utf-8"))
        self.assertIn("metrics", report)
        self.assertIn("per_intent", report)
        self.assertNotIn("e2e", report)

    def test_module_import_has_no_side_effects(self) -> None:
        module = importlib.reload(eval_intake)
        self.assertTrue(callable(module.evaluate))
        self.assertTrue(callable(module.evaluate_memory))


if __name__ == "__main__":
    unittest.main()
