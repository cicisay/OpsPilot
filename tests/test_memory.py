import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.memory import MemoryStore  # noqa: E402


def experiment(index: int, status: str = "verdict", started_at: str = "") -> dict:
    return {
        "experiment_id": f"p{index}:v1:2026-11",
        "product_id": f"p{index}",
        "variant_id": "v1",
        "started_at": started_at or f"2026-11-{index + 1:02d}T10:00:00",
        "window_days": 14,
        "baseline": {"order_rate": 0.007},
        "status": status,
    }


class MemoryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class ProfileTests(MemoryTestCase):
    def test_profile_roundtrip(self):
        self.store.write_profile({"store_slug": "globaltrade", "active_products": 50})
        profile = self.store.read_profile()
        self.assertEqual(profile["store_slug"], "globaltrade")
        self.assertEqual(profile["active_products"], 50)
        self.assertIn("updated_at", profile)

    def test_missing_profile_reads_as_empty(self):
        self.assertEqual(self.store.read_profile(), {})

    def test_corrupt_profile_reads_as_empty_and_is_counted(self):
        self.store.profile_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.profile_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store.read_profile(), {})
        self.assertEqual(self.store.bad_lines, 1)

    def test_profile_history_is_bounded(self):
        for index in range(12):
            self.store.write_profile({"active_products": index})
        versions = sorted(self.store.history_dir.glob("*.json"))
        self.assertEqual(len(versions), 8)

    def test_no_temp_file_is_left_behind(self):
        self.store.write_profile({"store_slug": "globaltrade"})
        self.assertEqual(list(self.store.root.glob("*.tmp")), [])

    def test_credentials_are_scrubbed(self):
        self.store.write_profile(
            {
                "store_slug": "globaltrade",
                "api_key": "sk-secret",
                "nested": {"accessToken": "abc", "note": "keep"},
            }
        )
        profile = self.store.read_profile()
        self.assertNotIn("api_key", profile)
        self.assertEqual(profile["nested"], {"note": "keep"})


class ExperimentTests(MemoryTestCase):
    def test_append_then_read(self):
        self.assertTrue(self.store.append_experiment(experiment(1, status="observing")))
        items = self.store.read_experiments()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["product_id"], "p1")

    def test_append_is_idempotent(self):
        self.assertTrue(self.store.append_experiment(experiment(1)))
        self.assertFalse(self.store.append_experiment(experiment(1)))
        self.assertEqual(len(self.store.read_experiments()), 1)

    def test_appending_requires_an_id(self):
        with self.assertRaises(ValueError):
            self.store.append_experiment({"product_id": "p1"})

    def test_update_writes_a_verdict(self):
        self.store.append_experiment(experiment(1, status="observing"))
        changed = self.store.update_experiment(
            "p1:v1:2026-11",
            {"status": "verdict", "verdict": "supported", "outcome": {"lift": 0.22}},
        )
        self.assertTrue(changed)
        item = self.store.read_experiments()[0]
        self.assertEqual(item["verdict"], "supported")
        self.assertEqual(item["outcome"]["lift"], 0.22)

    def test_update_unknown_id_returns_false(self):
        self.assertFalse(self.store.update_experiment("nope", {"status": "verdict"}))

    def test_open_experiments_stay_in_the_window(self):
        for index in range(25):
            self.store.append_experiment(experiment(index, status="verdict"))
        self.store.append_experiment(
            experiment(99, status="observing", started_at="2026-01-01T00:00:00")
        )
        items = self.store.read_experiments(limit=20, include_open=True)
        self.assertEqual(len(items), 21)
        self.assertTrue(any(item["status"] == "observing" for item in items))

    def test_without_open_extension_only_the_window_is_returned(self):
        self.store.append_experiment(experiment(1, status="observing"))
        items = self.store.read_experiments(include_open=False)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "observing")

    def test_broken_line_is_skipped(self):
        path = self.store.experiments_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(experiment(1)) + "\n" + "{broken\n" + json.dumps(experiment(2)) + "\n",
            encoding="utf-8",
        )
        items = self.store.read_experiments()
        self.assertEqual(len(items), 2)
        self.assertEqual(self.store.bad_lines, 1)

    def test_broken_line_is_counted_once(self):
        path = self.store.experiments_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken\n", encoding="utf-8")
        self.store.read_experiments()
        self.store.read_experiments()
        self.assertEqual(self.store.bad_lines, 1)


class DecisionTests(MemoryTestCase):
    def test_decision_roundtrip(self):
        self.assertTrue(
            self.store.append_decision(
                {"decision_id": "run-1", "selected_count": 4, "reason": "eight anomalies"}
            )
        )
        decisions = self.store.read_decisions()
        self.assertEqual(decisions[0]["selected_count"], 4)

    def test_decision_is_idempotent(self):
        record = {"decision_id": "run-1", "selected_count": 4}
        self.assertTrue(self.store.append_decision(record))
        self.assertFalse(self.store.append_decision(record))

    def test_appending_without_newline_still_parses(self):
        path = self.store.decisions_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"decision_id": "run-1"}', encoding="utf-8")
        self.store.append_decision({"decision_id": "run-2"})
        decisions = self.store.read_decisions()
        self.assertEqual(len(decisions), 2)


class LessonsAndContextTests(MemoryTestCase):
    def test_lessons_roundtrip(self):
        self.store.write_lessons(
            [{"lesson_id": "l1", "scope": "tents", "statement": "bulk pricing works"}]
        )
        lessons = self.store.read_lessons()
        self.assertEqual(lessons[0]["lesson_id"], "l1")

    def test_context_contains_all_four_memories(self):
        self.store.write_profile({"store_slug": "globaltrade"})
        self.store.append_experiment(experiment(1))
        self.store.append_decision({"decision_id": "run-1"})
        self.store.write_lessons([{"lesson_id": "l1"}])
        context = self.store.context(run_id="run-1")
        self.assertEqual(
            set(context),
            {"run_id", "store_profile", "experiments", "decisions", "lessons", "saved_at"},
        )
        self.assertEqual(context["store_profile"]["store_slug"], "globaltrade")
        self.assertEqual(len(context["experiments"]), 1)

    def test_snapshot_is_written(self):
        self.store.write_profile({"store_slug": "globaltrade"})
        path = self.store.snapshot("run-1")
        self.assertTrue(path.exists())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["run_id"], "run-1")

    def test_snapshot_requires_run_id(self):
        with self.assertRaises(ValueError):
            self.store.snapshot("")

    def test_health_reports_counts(self):
        self.store.write_profile({"store_slug": "globaltrade"})
        self.store.append_experiment(experiment(1))
        self.store.append_decision({"decision_id": "run-1"})
        health = self.store.health()
        self.assertTrue(health["profile"])
        self.assertEqual(health["experiments"], 1)
        self.assertEqual(health["decisions"], 1)
        self.assertEqual(health["bad_lines"], 0)


if __name__ == "__main__":
    unittest.main()
