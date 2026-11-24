"""委托历史（intake/history.jsonl）：只增不改、best-effort、与 facts 隔离。

这一层要被反复证明的不是"能写进去"，而是三条纪律：
1. **只增不改**——追加第二条时，第一条的字节不变；
2. **best-effort**——目录不可写时返回 None，绝不抛异常拖垮预览；
3. **不混进 facts**——history.jsonl 与 memory/facts.jsonl 是两个文件两份语义，
   委托原文永远不进 facts。
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.intake.history import (  # noqa: E402
    DEFAULT_READ_LIMIT,
    HISTORY_FILENAME,
    HISTORY_SUBDIR,
    MAX_READ_LIMIT,
    MAX_TEXT,
    append_history,
    history_path,
    read_history,
)


def record(**over) -> dict:
    base = {
        "tenant_id": "globaltrade",
        "actor": "admin",
        "role": "admin",
        "text": "这20个新品上到美国站",
        "intent": "BATCH_UPLOAD",
        "confidence": 0.95,
        "artifact": "task_orders",
        "risk_ceiling": "R1",
        "slots": {"market": "US", "quantity": 20},
        "missing": [],
        "assumed": {"language": True},
        "pending_input": ["source_path"],
        "order_count": 3,
        "task_ids": ["BATCH_UPLOAD:p1", "BATCH_UPLOAD:p2", "BATCH_UPLOAD:p3"],
        "reason": "",
    }
    base.update(over)
    return base


class AppendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "globaltrade"

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_creates_the_file_and_one_line_per_call(self):
        entry = append_history(self.root, record())
        self.assertIsNotNone(entry)
        path = history_path(self.root)
        self.assertEqual("history.jsonl", HISTORY_FILENAME)
        self.assertEqual("intake", HISTORY_SUBDIR)
        self.assertEqual(self.root / "intake" / "history.jsonl", path)
        self.assertEqual(1, len(path.read_text(encoding="utf-8").splitlines()))
        self.assertTrue(entry["history_id"].startswith("H-"))
        self.assertEqual("preview", entry["status"])

    def test_append_is_append_only_existing_bytes_never_change(self):
        append_history(self.root, record(), now=datetime(2026, 11, 5, 9, 0, 0))
        path = history_path(self.root)
        first_bytes = path.read_bytes()
        append_history(self.root, record(text="把 p-alpha 的标题重写一下"), now=datetime(2026, 11, 5, 9, 1, 0))
        self.assertTrue(path.read_bytes().startswith(first_bytes))
        self.assertEqual(first_bytes, path.read_bytes()[: len(first_bytes)])

    def test_full_text_is_kept_up_to_the_limit(self):
        text = "这20个新品上到美国站" * 300
        entry = append_history(self.root, record(text=text))
        self.assertEqual(MAX_TEXT, len(entry["text"]))
        self.assertTrue(entry["text_truncated"])

    def test_bad_field_does_not_drop_the_record(self):
        entry = append_history(self.root, record(confidence="not-a-number", order_count="x"))
        self.assertIsNotNone(entry)
        self.assertEqual(0.0, entry["confidence"])
        self.assertEqual(0, entry["order_count"])

    def test_non_dict_record_is_rejected_without_raising(self):
        self.assertIsNone(append_history(self.root, "not a dict"))
        self.assertFalse(history_path(self.root).exists())

    def test_unwritable_root_returns_none_instead_of_raising(self):
        # 把一个普通文件当成租户目录：mkdir 会失败，接口必须自己咽下去。
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        self.assertIsNone(append_history(blocker, record()))
        self.assertEqual([], read_history(blocker))

    def test_history_never_creates_the_facts_file(self):
        append_history(self.root, record())
        self.assertFalse((self.root / "memory" / "facts.jsonl").exists())
        self.assertFalse((self.root / "memory").exists())


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "globaltrade"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_reads_as_empty(self):
        self.assertEqual([], read_history(self.root))

    def test_newest_first(self):
        for index in range(3):
            append_history(self.root, record(text=f"委托 {index}"))
        entries = read_history(self.root)
        self.assertEqual(["委托 2", "委托 1", "委托 0"], [item["text"] for item in entries])

    def test_limit_keeps_the_newest(self):
        for index in range(5):
            append_history(self.root, record(text=f"委托 {index}"))
        entries = read_history(self.root, limit=2)
        self.assertEqual(["委托 4", "委托 3"], [item["text"] for item in entries])

    def test_limit_is_clamped(self):
        append_history(self.root, record())
        self.assertEqual([], read_history(self.root, limit=0))
        self.assertEqual(1, len(read_history(self.root, limit=10**6)))
        # 非法 limit 回落到默认值，而不是抛异常。
        self.assertEqual(1, len(read_history(self.root, limit="abc")))
        self.assertGreaterEqual(MAX_READ_LIMIT, DEFAULT_READ_LIMIT)

    def test_broken_line_is_skipped_not_fatal(self):
        append_history(self.root, record(text="好的一行"))
        with history_path(self.root).open("a", encoding="utf-8") as handle:
            handle.write('{"half": "line"\n')  # 进程被杀留下的半行
        append_history(self.root, record(text="第二行"))
        entries = read_history(self.root)
        self.assertEqual(["第二行", "好的一行"], [item["text"] for item in entries])

    def test_entries_are_plain_json(self):
        append_history(self.root, record())
        raw = history_path(self.root).read_text(encoding="utf-8").strip()
        parsed = json.loads(raw)
        self.assertEqual("BATCH_UPLOAD", parsed["intent"])
        self.assertEqual("US", parsed["slots"]["market"])


if __name__ == "__main__":
    unittest.main()
