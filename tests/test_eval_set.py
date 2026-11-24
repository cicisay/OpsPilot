"""评测语料与评测脚本的契约测试。

语料分布（PRD Step 2）：12 类意图 × 14 = 168，加 歧义 15、复合 10、噪声 7 = 200。
其中：
- 歧义 15 条 → 期望 CLARIFY（与 CLARIFY 自己的 14 条合计 29）
- 复合 10 条 → 期望落在主意图上，辅助意图写在 `also` 里
- 噪声 7 条 → 期望 UNKNOWN（与 UNKNOWN 自己的 14 条合计 21）
所以业务意图合计 150 条、CLARIFY 29 条、UNKNOWN 21 条。
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ops_agent.intake import Intent, classify  # noqa: E402

EVAL_SET = ROOT / "tests" / "fixtures" / "intake" / "eval_set.jsonl"
BUSINESS_INTENTS = [
    Intent.BATCH_UPLOAD,
    Intent.SINGLE_UPLOAD,
    Intent.LISTING_DIAGNOSE,
    Intent.LISTING_REWRITE,
    Intent.MULTILANG_CONTENT,
    Intent.MEDIA_COMPOSE,
    Intent.DATA_QUERY,
    Intent.MEMORY_MANAGE,
    Intent.PRICE_SUGGEST,
    Intent.CUSTOMER_OUTREACH,
]

# 回归下限。当前实测 98.0% / 99.1%，留出余量给后续 Step 的规则调整；
# 一旦跌破这条线，说明哪次改动把手感改坏了。
ACCURACY_FLOOR = 0.85
SLOT_ACCURACY_FLOOR = 0.90
FALSE_EXECUTION_CEILING = 0.10


def load_rows() -> list[dict]:
    rows = []
    with EVAL_SET.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_eval_module():
    """把 scripts/eval_intake.py 当模块加载，直接测它的指标算法。"""
    path = ROOT / "scripts" / "eval_intake.py"
    spec = importlib.util.spec_from_file_location("eval_intake_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestEvalSetStructure(unittest.TestCase):
    """语料本身的形状：条数、id、分布、字段。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = load_rows()

    def test_file_exists_and_has_200_rows(self) -> None:
        self.assertTrue(EVAL_SET.exists(), f"语料缺失: {EVAL_SET}")
        # 基础语料 200 条（PRD Step 2）+ 回归块（E201 起，词典修复后追加，只增不减）。
        self.assertGreaterEqual(len(self.rows), 200)

    def test_every_line_is_valid_json_with_required_fields(self) -> None:
        for row in self.rows:
            with self.subTest(id=row.get("id")):
                self.assertIsInstance(row["id"], str)
                self.assertIsInstance(row["text"], str)
                self.assertTrue(row["text"].strip())
                self.assertIn(row["intent"], {item.value for item in Intent})
                self.assertIsInstance(row["slots"], dict)
                self.assertIsInstance(row.get("note", ""), str)

    def test_ids_are_unique_and_dense(self) -> None:
        ids = [row["id"] for row in self.rows]
        self.assertEqual(len(ids), len(set(ids)), "id 有重复")
        self.assertEqual([f"E{index:03d}" for index in range(1, len(ids) + 1)], sorted(ids))

    def test_each_intent_has_at_least_ten(self) -> None:
        counts = {}
        for row in self.rows:
            counts[row["intent"]] = counts.get(row["intent"], 0) + 1
        for intent in Intent:
            with self.subTest(intent=intent.value):
                self.assertGreaterEqual(counts.get(intent.value, 0), 10)

    def test_distribution_matches_prd(self) -> None:
        counts = {}
        for row in self.rows:
            counts[row["intent"]] = counts.get(row["intent"], 0) + 1
        business = sum(counts.get(intent.value, 0) for intent in BUSINESS_INTENTS)
        # 基础 150 + 回归块里追加的业务样本（E201—E204 全是业务意图）。
        self.assertEqual(154, business)
        self.assertEqual(29, counts.get("CLARIFY", 0), "CLARIFY 应为 14 + 15 条歧义")
        self.assertEqual(21, counts.get("UNKNOWN", 0), "UNKNOWN 应为 14 + 7 条噪声")
        # 语料布局：E001—E168 基础样本，E169—E183 歧义块，E184—E193 复合块，E194—E200 噪声块。
        # E201 起是回归块，只能追加、不许改老样本。
        self.assertEqual(10, sum(1 for row in self.rows if row.get("also")))
        self.assertTrue(all(row["intent"] == "CLARIFY" for row in self.rows[168:183]))
        self.assertTrue(all(row["intent"] == "UNKNOWN" for row in self.rows[193:200]))

    def test_texts_are_unique(self) -> None:
        texts = [row["text"] for row in self.rows]
        self.assertEqual(len(texts), len(set(texts)), "语料里有重复句子")

    def test_text_length_is_realistic(self) -> None:
        for row in self.rows:
            with self.subTest(id=row["id"]):
                # 最短 2 字（"在吗"/"改改"）——真实委托里确实有这种极端省略；
                # 最长不超过一句话，太长说明在拿长段落凑数。
                self.assertGreaterEqual(len(row["text"]), 2)
                self.assertLessEqual(len(row["text"]), 60)

    def test_composite_rows_carry_also(self) -> None:
        for row in self.rows:
            if not row.get("also"):
                continue
            with self.subTest(id=row["id"]):
                self.assertIsInstance(row["also"], list)
                self.assertTrue(row["also"])
                for item in row["also"]:
                    self.assertIn(item, {x.value for x in Intent})
                    self.assertNotEqual(row["intent"], item, "复合的辅助意图不能等于主意图")

    def test_expected_slots_use_declared_names(self) -> None:
        from ops_agent.intake import INTENT_SPECS

        extra_allowed = {"quantity", "sku"}
        for row in self.rows:
            declared = set(INTENT_SPECS[Intent(row["intent"])]["slots"]) | extra_allowed
            for name in row["slots"]:
                with self.subTest(id=row["id"], slot=name):
                    self.assertIn(name, declared, "槽位名不在该意图的规格里")


class TestEvalMetrics(unittest.TestCase):
    """指标口径：脚本算出来的数要能被解释，也要守住下限。"""

    def test_evaluate_computes_accuracy_on_fixture(self) -> None:
        module = load_eval_module()
        rows = [
            {"id": "T1", "text": "这20个新品帮我上到美国站", "intent": "BATCH_UPLOAD", "slots": {"market": "US"}},
            {"id": "T2", "text": "今天天气不错", "intent": "BATCH_UPLOAD", "slots": {}},
        ]
        metrics = module.evaluate(rows)["metrics"]
        self.assertEqual(0.5, metrics["intent_accuracy"])
        self.assertEqual(1, metrics["error_count"])
        self.assertEqual(1.0, metrics["slot_key_accuracy"])

    def test_false_execution_is_measured_on_safe_cases(self) -> None:
        module = load_eval_module()
        rows = [
            {"id": "T1", "text": "今天天气不错", "intent": "CLARIFY", "slots": {}},
            {"id": "T2", "text": "这20个新品帮我上到美国站", "intent": "UNKNOWN", "slots": {}},
        ]
        report = module.evaluate(rows)
        # 第一条实际 UNKNOWN（同属安全）→ 算澄清；第二条被执行 → 算误执行。
        self.assertEqual(0.5, report["metrics"]["clarify_recall"])
        self.assertEqual(0.5, report["metrics"]["false_execution_rate"])
        # 两条都算错：一条把 CLARIFY 判成 UNKNOWN，一条把 UNKNOWN 判成业务意图。
        # 但安全口径只关心后者——前者只是措辞不同。
        self.assertEqual(2, len(report["errors"]))

    def test_slot_lists_compare_as_sets(self) -> None:
        module = load_eval_module()
        rows = [
            {"id": "T1", "text": "曝光和点击的数据拉一下", "intent": "DATA_QUERY",
             "slots": {"metrics": ["点击", "曝光"]}},
        ]
        self.assertEqual(1.0, module.evaluate(rows)["metrics"]["slot_key_accuracy"])

    def test_accuracy_floor_holds(self) -> None:
        rows = load_rows()
        metrics = load_eval_module().evaluate(rows)["metrics"]
        self.assertGreaterEqual(
            metrics["intent_accuracy"], ACCURACY_FLOOR,
            f"意图准确率跌破下限：{metrics['intent_accuracy']:.2%}",
        )

    def test_slot_accuracy_floor_holds(self) -> None:
        rows = load_rows()
        metrics = load_eval_module().evaluate(rows)["metrics"]
        self.assertGreaterEqual(metrics["slot_key_accuracy"], SLOT_ACCURACY_FLOOR)

    def test_false_execution_stays_low(self) -> None:
        rows = load_rows()
        metrics = load_eval_module().evaluate(rows)["metrics"]
        self.assertLessEqual(
            metrics["false_execution_rate"], FALSE_EXECUTION_CEILING,
            "该反问的委托被执行了，这是安全指标，不能靠调语料放过去",
        )

    def test_error_entries_carry_id_expected_actual_reason(self) -> None:
        rows = [
            {"id": "T1", "text": "今天天气不错", "intent": "BATCH_UPLOAD", "slots": {}},
        ]
        report = load_eval_module().evaluate(rows)
        error = report["errors"][0]
        for key in ("id", "expected", "actual", "reason", "kind"):
            self.assertIn(key, error)
        self.assertTrue(all(error[key] for key in ("id", "expected", "actual", "reason")))

    def test_classify_result_matches_expected_enough(self) -> None:
        """抽查 12 条（每类 1 条），确保语料与实现没有整体错位。"""
        for intent in Intent:
            sample = next(row for row in load_rows() if row["intent"] == intent.value and not row.get("also"))
            with self.subTest(intent=intent.value):
                self.assertEqual(intent, classify(sample["text"]).intent)


if __name__ == "__main__":
    unittest.main()
