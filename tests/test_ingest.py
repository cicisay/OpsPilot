"""Step 7 的上新链路测试：供应商 CSV → 结构化 → Listing 草稿。

覆盖 PRD 的四条通过标准：
1. 样例 CSV 产出 3 条草稿，缺字段 / 规格未溯源 / 合规阻断三种状态都能复现；
2. 路径穿越（``../../etc/passwd`` 之类）被拒绝；
3. dry-run 不写 ``data/``；
4. 不臆造数值：草稿里出现的每个数字都能回到供应商那一行。

另外补了几条这一层特有的边界：缺 sku 的行不炸整批、金额读不出算缺失、
市场非法不悄悄换成默认值、facts 只作上下文不进证据。
"""

import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent import ingest  # noqa: E402
from ops_agent.cli import cmd_intake_run  # noqa: E402
from ops_agent.compliance import DEFAULT_LEXICON, ComplianceChecker  # noqa: E402
from ops_agent.ingest import (  # noqa: E402
    MAX_ROWS,
    STATUS_BLOCKED,
    STATUS_NEEDS_FILL,
    STATUS_READY,
    DraftSet,
    IngestError,
    build_draft,
    build_drafts,
    read_supplier_csv,
    resolve_ingest_path,
    summarize,
    unverified_numbers,
    verify_spec,
)
from ops_agent.models import Market  # noqa: E402
from ops_agent.tools import ToolContext, build_ingest_registry  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ingest" / "sample_supplier.csv"

HEADER = [
    "sku",
    "title",
    "category",
    "cost",
    "price",
    "currency",
    "dimensions",
    "weight",
    "material",
    "market",
    "language",
]


def checker() -> ComplianceChecker:
    """固定词库，避免测试跟着 data/compliance/lexicon.json 的内容漂。"""

    return ComplianceChecker(lexicon=DEFAULT_LEXICON)


def good_row(**overrides) -> dict:
    row = {
        "sku": "FT-100",
        "title": "Ceramic Planter Trio",
        "category": "garden",
        "cost": "6.40",
        "price": "21.00",
        "currency": "USD",
        "dimensions": "6.5 x 6.5 x 5.2 in",
        "weight": "1.10 lb",
        "material": "ceramic",
        "market": "US",
        "language": "en",
        "_line": "2",
    }
    row.update(overrides)
    return row


def write_csv(path: Path, rows: list[list], header=None, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header or HEADER)
        writer.writerows(rows)
    return path


class PathPolicyTests(unittest.TestCase):
    """路径策略：先拒 ..，再 resolve，最后 relative_to 兜底。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ingest"
        self.root.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_relative_path_resolves_inside_root(self):
        target, relative = resolve_ingest_path("sample.csv", self.root)
        self.assertEqual(relative, "sample.csv")
        self.assertEqual(target, (self.root / "sample.csv").resolve())

    def test_nested_relative_path_is_allowed(self):
        (self.root / "batch").mkdir()
        _, relative = resolve_ingest_path("batch/a.csv", self.root)
        self.assertEqual(relative, "batch/a.csv")

    def test_parent_traversal_is_rejected(self):
        with self.assertRaises(IngestError) as ctx:
            resolve_ingest_path("../../etc/passwd", self.root)
        self.assertEqual(ctx.exception.code, "path_traversal")

    def test_backslash_traversal_is_rejected(self):
        with self.assertRaises(IngestError) as ctx:
            resolve_ingest_path("..\\..\\windows\\system32\\x.csv", self.root)
        self.assertIn(ctx.exception.code, ("path_traversal", "bad_extension"))

    def test_absolute_path_is_rejected(self):
        with self.assertRaises(IngestError) as ctx:
            resolve_ingest_path("/etc/passwd.csv", self.root)
        self.assertEqual(ctx.exception.code, "absolute_path")

    def test_drive_letter_is_rejected(self):
        with self.assertRaises(IngestError) as ctx:
            resolve_ingest_path("C:\\temp\\a.csv", self.root)
        self.assertEqual(ctx.exception.code, "absolute_path")

    def test_empty_path_is_rejected(self):
        with self.assertRaises(IngestError) as ctx:
            resolve_ingest_path("   ", self.root)
        self.assertEqual(ctx.exception.code, "empty_path")

    def test_non_csv_is_rejected(self):
        with self.assertRaises(IngestError) as ctx:
            resolve_ingest_path("sample.xlsx", self.root)
        self.assertEqual(ctx.exception.code, "bad_extension")

    def test_symlink_escape_is_rejected(self):
        outside = Path(self._tmp.name) / "outside.csv"
        write_csv(outside, [])
        link = self.root / "link.csv"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("当前环境不允许创建符号链接")
        with self.assertRaises(IngestError) as ctx:
            resolve_ingest_path("link.csv", self.root)
        self.assertEqual(ctx.exception.code, "path_traversal")

    def test_missing_file_reports_not_found(self):
        with self.assertRaises(IngestError) as ctx:
            ingest.parse_supplier_csv("nope.csv", self.root)
        self.assertEqual(ctx.exception.code, "file_not_found")

    def test_oversized_file_is_rejected(self):
        write_csv(self.root / "big.csv", [["a"] * len(HEADER)] * 3)
        with mock.patch.object(ingest, "MAX_BYTES", 5):
            with self.assertRaises(IngestError) as ctx:
                read_supplier_csv(self.root / "big.csv")
        self.assertEqual(ctx.exception.code, "file_too_large")


class CsvParsingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_fixture_parses_three_rows(self):
        parsed = read_supplier_csv(FIXTURE)
        self.assertEqual(len(parsed.rows), 3)
        self.assertIn("sku", parsed.columns)
        self.assertEqual(parsed.notes, [])

    def test_bom_header_is_stripped(self):
        path = write_csv(self.root / "bom.csv", [list(good_row().values())], encoding="utf-8-sig")
        parsed = read_supplier_csv(path)
        self.assertIn("sku", parsed.columns)
        self.assertEqual(parsed.rows[0]["sku"], "FT-100")

    def test_missing_required_column_is_noted(self):
        header = [name for name in HEADER if name != "price"]
        path = write_csv(self.root / "thin.csv", [["FT-1", "x", "garden", "1.0"]], header=header)
        parsed = read_supplier_csv(path)
        self.assertTrue(any("price" in note for note in parsed.notes))

    def test_unknown_columns_are_ignored_but_reported(self):
        header = HEADER + ["moq"]
        row = list(good_row().values()) + ["100"]
        path = write_csv(self.root / "extra.csv", [row], header=header)
        parsed = read_supplier_csv(path)
        self.assertEqual(parsed.ignored_columns, ["moq"])
        self.assertTrue(any("moq" in note for note in parsed.notes))

    def test_blank_lines_are_skipped(self):
        path = self.root / "blank.csv"
        path.write_text(
            ",".join(HEADER) + "\n" + "\n" + ",".join(str(v) for v in good_row().values()) + "\n",
            encoding="utf-8",
        )
        parsed = read_supplier_csv(path)
        self.assertEqual(len(parsed.rows), 1)

    def test_header_only_file_reports_no_rows(self):
        path = write_csv(self.root / "empty.csv", [])
        parsed = read_supplier_csv(path)
        self.assertEqual(parsed.rows, [])
        self.assertTrue(any("没有数据行" in note for note in parsed.notes))

    def test_zero_byte_file_is_rejected(self):
        path = self.root / "zero.csv"
        path.write_bytes(b"")
        with self.assertRaises(IngestError) as ctx:
            read_supplier_csv(path)
        self.assertEqual(ctx.exception.code, "empty_csv")

    def test_row_cap_truncates_and_notes(self):
        rows = [list(good_row(sku=f"FT-{index}").values()) for index in range(6)]
        path = write_csv(self.root / "many.csv", rows)
        with mock.patch.object(ingest, "MAX_ROWS", 3):
            parsed = read_supplier_csv(path)
        self.assertEqual(len(parsed.rows), 3)
        self.assertTrue(any(str(3) in note for note in parsed.notes))

    def test_default_row_cap_is_positive(self):
        self.assertGreaterEqual(MAX_ROWS, 50)


class DraftBuildTests(unittest.TestCase):
    def setUp(self):
        self.checker = checker()

    def build(self, **overrides) -> DraftSet:
        return build_draft(good_row(**overrides), checker=self.checker)

    def test_complete_row_is_ready(self):
        draft = self.build()
        self.assertEqual(draft.status, STATUS_READY)
        self.assertEqual(draft.missing, [])
        self.assertEqual(draft.price_cents, 2100)
        self.assertEqual(draft.cost_cents, 640)

    def test_missing_price_needs_fill_and_keeps_zero(self):
        draft = self.build(price="")
        self.assertEqual(draft.status, STATUS_NEEDS_FILL)
        self.assertIn("price", draft.missing)
        # 不臆造：价格留 0，而不是按成本乘个系数。
        self.assertEqual(draft.price_cents, 0)

    def test_unparseable_amount_counts_as_missing(self):
        draft = self.build(cost="待定")
        self.assertEqual(draft.status, STATUS_NEEDS_FILL)
        self.assertIn("cost", draft.missing)
        self.assertTrue(any("不是可识别金额" in note for note in draft.notes))

    def test_market_conflict_with_request_needs_fill(self):
        # K-02：资料写 US、委托要上 DE，不能静默按资料出——上错站点的代价比多问一句高。
        draft = build_draft(good_row(market="US"), market=Market.DE, checker=self.checker)
        self.assertEqual(draft.status, STATUS_NEEDS_FILL)
        self.assertIn("market", draft.missing)
        self.assertIn("market_conflict", draft.flags)
        self.assertTrue(any("不一致" in note for note in draft.notes))

    def test_market_match_is_not_flagged(self):
        draft = build_draft(good_row(market="US"), market=Market.US, checker=self.checker)
        self.assertNotIn("market_conflict", draft.flags)
        self.assertNotIn("market", draft.missing)

    def test_missing_sku_uses_row_placeholder(self):
        draft = self.build(sku="")
        self.assertEqual(draft.status, STATUS_NEEDS_FILL)
        self.assertIn("sku", draft.missing)
        self.assertIn("missing_sku", draft.flags)
        self.assertTrue(draft.sku.startswith("ROW-"))

    def test_specs_present_are_verified(self):
        draft = self.build()
        for name in ("dimensions", "weight", "material"):
            self.assertTrue(draft.specs[name]["verified"], name)
            self.assertEqual(draft.specs[name]["reason"], "")

    def test_absent_specs_are_unverified_and_flagged(self):
        draft = self.build(dimensions="", weight="", material="")
        for name in ("dimensions", "weight", "material"):
            self.assertFalse(draft.specs[name]["verified"], name)
            self.assertEqual(draft.specs[name]["reason"], "源资料未提供")
        self.assertIn("spec_unverified", draft.flags)

    def test_spec_number_not_in_source_is_unverified(self):
        verified, reason = verify_spec("9.9 x 9.9 in", "6.5 x 6.5 in")
        self.assertFalse(verified)
        self.assertIn("9.9", reason)

    def test_spec_without_numbers_requires_verbatim_text(self):
        self.assertTrue(verify_spec("ceramic", "ceramic planter")[0])
        self.assertFalse(verify_spec("stoneware", "ceramic planter")[0])

    def test_unverified_numbers_detects_leak(self):
        leaked = unverified_numbers("600D fabric, 30% lighter", "ceramic planter 6.5 in")
        self.assertIn("600", leaked)
        self.assertIn("30", leaked)

    def test_copy_never_contains_numbers_outside_source(self):
        for draft in build_drafts(read_supplier_csv(FIXTURE).rows, checker=self.checker):
            self.assertEqual(draft.unverified_numbers, [], draft.sku)
            self.assertNotIn("unverified_numbers", draft.flags)

    def test_brand_term_blocks_the_draft(self):
        draft = self.build(title="LEGO Compatible Storage Box")
        self.assertEqual(draft.status, STATUS_BLOCKED)
        self.assertFalse(draft.compliance["passed"])
        self.assertTrue(
            any("品牌" in item["message"] for item in draft.compliance["violations"])
        )

    def test_compliance_cannot_be_skipped(self):
        # 不传 checker 也要跑预检：这是"禁止跳过合规"的代码级保证。
        draft = build_draft(good_row(title="LEGO Compatible Storage Box"))
        self.assertEqual(draft.status, STATUS_BLOCKED)
        self.assertTrue(draft.compliance["checked"])

    def test_blocked_wins_over_needs_fill(self):
        draft = self.build(title="LEGO Brick Box", price="")
        self.assertEqual(draft.status, STATUS_BLOCKED)
        self.assertIn("price", draft.missing)

    def test_invalid_market_needs_fill_instead_of_silent_default(self):
        draft = self.build(market="EU")
        self.assertEqual(draft.status, STATUS_NEEDS_FILL)
        self.assertIn("market", draft.missing)
        self.assertIn("bad_market", draft.flags)

    def test_missing_market_uses_store_default_and_records_assumption(self):
        draft = self.build(market="")
        self.assertEqual(draft.market, "US")
        self.assertTrue(draft.assumed.get("market"))
        self.assertNotIn("market", draft.missing)

    def test_missing_language_is_assumed(self):
        draft = self.build(language="")
        self.assertEqual(draft.language, "en")
        self.assertTrue(draft.assumed.get("language"))

    def test_store_market_argument_is_used_when_row_is_silent(self):
        draft = build_draft(good_row(market=""), market=Market.DE, checker=self.checker)
        self.assertEqual(draft.market, "DE")

    def test_facts_are_context_not_evidence(self):
        draft = self.build()
        with_facts = build_draft(
            good_row(),
            facts=["不做宠物类目", "品牌词：Aurora", "不做宠物类目"],
            checker=self.checker,
        )
        self.assertEqual(with_facts.facts_used, ["不做宠物类目", "品牌词：Aurora"])
        self.assertEqual(draft.facts_used, [])
        self.assertNotIn("不做宠物类目", json.dumps(draft.to_dict(), ensure_ascii=False))

    def test_too_many_facts_are_capped(self):
        draft = build_draft(
            good_row(),
            facts=[f"fact-{index}" for index in range(40)],
            checker=self.checker,
        )
        self.assertEqual(len(draft.facts_used), ingest.MAX_FACTS)

    def test_thin_copy_is_flagged_when_facts_are_scarce(self):
        draft = self.build(material="", dimensions="", weight="")
        self.assertIn("thin_copy", draft.flags)
        self.assertLess(len(draft.bullets), 2)

    def test_duplicate_sku_is_flagged_not_dropped(self):
        rows = [good_row(), good_row(title="Same SKU Again")]
        drafts = build_drafts(rows, checker=self.checker)
        self.assertEqual(len(drafts), 2)
        self.assertNotIn("duplicate_sku", drafts[0].flags)
        self.assertIn("duplicate_sku", drafts[1].flags)

    def test_duplicate_detection_ignores_case(self):
        rows = [good_row(sku="ft-100"), good_row()]
        drafts = build_drafts(rows, checker=self.checker)
        self.assertIn("duplicate_sku", drafts[1].flags)

    def test_money_parsing_accepts_symbols_and_commas(self):
        draft = self.build(cost="$1,299.00", price="USD 1,999.50")
        self.assertEqual(draft.cost_cents, 129900)
        self.assertEqual(draft.price_cents, 199950)

    def test_negative_or_zero_price_is_missing(self):
        self.assertEqual(self.build(price="0").status, STATUS_NEEDS_FILL)
        self.assertEqual(self.build(price="-3").status, STATUS_NEEDS_FILL)

    def test_draft_shape_has_no_write_path(self):
        payload = self.build().to_dict()
        self.assertEqual(payload["writes"], 0)
        self.assertEqual(payload["source"], "supplier_csv")
        self.assertEqual(payload["draft_id"], "DRAFT:FT-100")
        self.assertNotIn("task_id", payload)

    def test_draft_set_rejects_unknown_status(self):
        with self.assertRaises(ValueError):
            DraftSet(sku="X", status="published", market="US", language="en")

    def test_draft_set_requires_sku(self):
        with self.assertRaises(ValueError):
            DraftSet(sku="", status=STATUS_READY, market="US", language="en")


class FixtureTests(unittest.TestCase):
    """PRD 的验收口径：样例 CSV 出 3 条，三种状态各一条。"""

    def setUp(self):
        self.drafts = build_drafts(read_supplier_csv(FIXTURE).rows, checker=checker())

    def test_three_drafts(self):
        self.assertEqual(len(self.drafts), 3)

    def test_all_three_statuses_are_reproducible(self):
        statuses = [draft.status for draft in self.drafts]
        self.assertIn(STATUS_READY, statuses)
        self.assertIn(STATUS_NEEDS_FILL, statuses)
        self.assertIn(STATUS_BLOCKED, statuses)
        self.assertEqual(summarize(self.drafts)["status"][STATUS_BLOCKED], 1)

    def test_summary_counts_and_flags(self):
        summary = summarize(self.drafts)
        self.assertEqual(summary["total"], 3)
        self.assertIn("spec_unverified", summary["flags"])

    def test_draft_states_are_honest(self):
        by_sku = {draft.sku: draft for draft in self.drafts}
        self.assertEqual(by_sku["FT-001"].status, STATUS_READY)
        self.assertEqual(by_sku["FT-002"].missing, ["price"])
        self.assertEqual(by_sku["FT-003"].status, STATUS_BLOCKED)


class ToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ingest"
        write_csv(self.root / "sample.csv", [
            list(good_row().values()),
            list(good_row(sku="FT-200", title="LEGO Brick Tray").values()),
        ])
        self.context = ToolContext(
            compliance=checker(),
            ingest_root=self.root,
        )
        self.registry = build_ingest_registry()

    def tearDown(self):
        self._tmp.cleanup()

    def test_registry_exposes_two_tools_with_expected_risk(self):
        specs = {item["name"]: item for item in self.registry.specs()}
        self.assertEqual(set(specs), {"file_parse", "listing_draft"})
        self.assertEqual(specs["file_parse"]["risk_level"], "R0")
        self.assertEqual(specs["listing_draft"]["risk_level"], "R1")
        self.assertTrue(specs["listing_draft"]["requires_approval"])

    def test_file_parse_reads_rows(self):
        result = self.registry.call("file_parse", {"path": "sample.csv", "kind": "csv"}, self.context)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["count"], 2)
        self.assertEqual(len(self.context.ingest_rows), 2)

    def test_file_parse_rejects_traversal(self):
        result = self.registry.call("file_parse", {"path": "../../etc/passwd", "kind": "csv"}, self.context)
        self.assertFalse(result.ok)
        self.assertIn(result.code, ("path_traversal", "bad_extension"))
        self.assertEqual(self.context.ingest_rows, [])

    def test_file_parse_rejects_non_csv_kind(self):
        # schema 的 enum 先拦一层，函数里的 kind 判断是第二道。
        result = self.registry.call("file_parse", {"path": "sample.csv", "kind": "xlsx"}, self.context)
        self.assertFalse(result.ok)
        self.assertIn(result.code, ("bad_kind", "invalid_arguments"))

    def test_file_parse_function_guard_rejects_unknown_kind(self):
        from ops_agent.tools import ToolError, file_parse

        with self.assertRaises(ToolError) as ctx:
            file_parse(self.context, "sample.csv", kind="json")
        self.assertEqual(ctx.exception.code, "bad_kind")

    def test_listing_draft_requires_parsed_rows(self):
        result = self.registry.call(
            "listing_draft", {"sku": "FT-100", "market": "US"}, self.context
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "no_ingest_rows")

    def test_listing_draft_returns_draft(self):
        self.registry.call("file_parse", {"path": "sample.csv"}, self.context)
        result = self.registry.call(
            "listing_draft",
            {"sku": "ft-100", "market": "US", "language": "en", "facts": ["不做宠物类目"]},
            self.context,
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["status"], STATUS_READY)
        self.assertEqual(result.data["writes"], 0)
        self.assertEqual(result.data["facts_used"], ["不做宠物类目"])

    def test_listing_draft_blocks_brand_row(self):
        self.registry.call("file_parse", {"path": "sample.csv"}, self.context)
        result = self.registry.call(
            "listing_draft", {"sku": "FT-200", "market": "US"}, self.context
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["status"], STATUS_BLOCKED)

    def test_listing_draft_unknown_sku(self):
        self.registry.call("file_parse", {"path": "sample.csv"}, self.context)
        result = self.registry.call(
            "listing_draft", {"sku": "NOPE", "market": "US"}, self.context
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "unknown_sku")

    def test_listing_draft_rejects_overlong_fact(self):
        self.registry.call("file_parse", {"path": "sample.csv"}, self.context)
        result = self.registry.call(
            "listing_draft",
            {"sku": "FT-100", "market": "US", "facts": ["x" * 300]},
            self.context,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_arguments")

    def test_tool_calls_write_nothing(self):
        before = sorted(path.name for path in self.root.iterdir())
        self.registry.call("file_parse", {"path": "sample.csv"}, self.context)
        self.registry.call("listing_draft", {"sku": "FT-100", "market": "US"}, self.context)
        after = sorted(path.name for path in self.root.iterdir())
        self.assertEqual(before, after)


class CliTests(unittest.TestCase):
    """CLI 只认 --dry-run，stdout 必须是纯 JSONL。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "data"
        (self.data / "ingest").mkdir(parents=True)
        write_csv(
            self.data / "ingest" / "batch.csv",
            [
                list(good_row().values()),
                list(good_row(sku="FT-300", title="LEGO Brick Tray").values()),
            ],
        )

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, **kwargs):
        args = mock.Mock()
        args.text = kwargs.get("text", "上这 3 个新品")
        args.dry_run = kwargs.get("dry_run", True)
        args.ingest = kwargs.get("ingest", True)
        args.source = kwargs.get("source", None)
        args.snapshot = kwargs.get("snapshot", None)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("ops_agent.cli.DATA_DIR", self.data):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cmd_intake_run(args)
        return code, out.getvalue(), err.getvalue()

    def test_stdout_is_pure_jsonl(self):
        code, out, err = self.run_cli()
        self.assertEqual(code, 0)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            payload = json.loads(line)
            self.assertEqual(payload["writes"], 0)
        self.assertIn("2 条草稿", err)

    def test_source_option_limits_to_named_file(self):
        write_csv(self.data / "ingest" / "other.csv", [list(good_row(sku="FT-999").values())])
        code, out, _ = self.run_cli(source="other.csv")
        self.assertEqual(code, 0)
        payload = json.loads(out.splitlines()[0])
        self.assertEqual(payload["sku"], "FT-999")

    def test_source_traversal_is_refused(self):
        code, out, err = self.run_cli(source="../../etc/passwd")
        self.assertEqual(code, 2)
        self.assertEqual(out.strip(), "")
        self.assertIn("路径不接受", err)

    def test_missing_dry_run_writes_nothing(self):
        before = sorted(path.relative_to(self.data) for path in self.data.rglob("*"))
        code, out, _ = self.run_cli(dry_run=False)
        after = sorted(path.relative_to(self.data) for path in self.data.rglob("*"))
        self.assertEqual(code, 2)
        self.assertEqual(out.strip(), "")
        self.assertEqual(before, after)

    def test_empty_ingest_dir_falls_back_to_packaged_sample(self):
        for path in (self.data / "ingest").glob("*.csv"):
            path.unlink()
        code, out, err = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(len([line for line in out.splitlines() if line.strip()]), 3)
        self.assertIn("packaged_sample", err)

    def test_cli_leaves_data_dir_untouched(self):
        before = sorted(path.relative_to(self.data) for path in self.data.rglob("*"))
        self.run_cli()
        after = sorted(path.relative_to(self.data) for path in self.data.rglob("*"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
