import argparse
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.experiment import build_record  # noqa: E402
from ops_agent.memory import MemoryStore  # noqa: E402
from ops_agent.cli import (  # noqa: E402
    _judge_due_experiments,
    _limit_override,
    _limit_value,
    cmd_agent_run,
    cmd_supervisor_run,
)


class LimitParsingTests(unittest.TestCase):
    def test_auto_means_model_decides(self):
        self.assertEqual(_limit_override("auto"), 0)
        self.assertEqual(_limit_override(""), 0)
        self.assertEqual(_limit_override(None), 0)

    def test_number_is_a_human_ceiling(self):
        self.assertEqual(_limit_override("4"), 4)
        self.assertEqual(_limit_override(4), 4)
        self.assertEqual(_limit_override("-2"), 0)

    def test_rules_need_a_number_so_auto_falls_back(self):
        self.assertEqual(_limit_value("auto", auto=5), 5)
        self.assertEqual(_limit_value("7", auto=5), 7)
        self.assertEqual(_limit_value("nonsense", auto=3), 3)


class RoutingTests(unittest.TestCase):
    def test_supervisor_llm_delegates_to_the_director(self):
        args = argparse.Namespace(planner="llm", limit="auto")
        with patch("ops_agent.cli.cmd_director_run", return_value=0) as director:
            self.assertEqual(cmd_supervisor_run(args), 0)
            director.assert_called_once_with(args)

    def test_supervisor_auto_uses_the_director_when_a_key_exists(self):
        """一键脚本用 --planner auto：有模型就该出体检分，而不是只写规则任务单。"""
        args = argparse.Namespace(
            planner="auto", limit="auto", offline=True, snapshot="missing.json", days=30, out=None
        )
        settings = SimpleNamespace(enabled=True, describe=lambda: "deepseek · model-x")
        with patch("ops_agent.cli.llm_settings", return_value=settings), patch(
            "ops_agent.cli.cmd_director_run", return_value=0
        ) as director:
            self.assertEqual(cmd_supervisor_run(args), 0)
            director.assert_called_once_with(args)

    def test_supervisor_auto_without_key_runs_rules_and_says_no_health_score(self):
        args = argparse.Namespace(
            planner="auto", limit="3", offline=True, snapshot="missing.json", days=30, out=None
        )
        settings = SimpleNamespace(enabled=False, describe=lambda: "未配置")
        buffer = io.StringIO()
        with patch("ops_agent.cli.llm_settings", return_value=settings), patch(
            "ops_agent.cli._load_snapshot_data", return_value=None
        ), patch("ops_agent.cli.cmd_director_run") as director, redirect_stdout(buffer):
            self.assertEqual(cmd_supervisor_run(args), 1)
            director.assert_not_called()
        self.assertIn("不产出体检分", buffer.getvalue())

    def test_supervisor_auto_falls_back_when_the_model_fails(self):
        """key 欠费（429）比没配 key 更常见：一键脚本不能因此整条断掉。"""
        args = argparse.Namespace(
            planner="auto", limit="3", offline=True, snapshot="missing.json", days=30, out=None
        )
        settings = SimpleNamespace(enabled=True, describe=lambda: "minimax · abab")
        buffer = io.StringIO()
        with patch("ops_agent.cli.llm_settings", return_value=settings), patch(
            "ops_agent.cli.cmd_director_run", return_value=1
        ), patch("ops_agent.cli._load_snapshot_data", return_value=None), redirect_stdout(buffer):
            self.assertEqual(cmd_supervisor_run(args), 1)
        output = buffer.getvalue()
        self.assertIn("降级到规则引擎", output)
        self.assertIn("不产出体检分", output)

    def test_explicit_llm_failure_is_not_downgraded(self):
        args = argparse.Namespace(
            planner="llm", limit="3", offline=True, snapshot="missing.json", days=30, out=None
        )
        settings = SimpleNamespace(enabled=True, describe=lambda: "minimax · abab")
        with patch("ops_agent.cli.llm_settings", return_value=settings), patch(
            "ops_agent.cli.cmd_director_run", return_value=1
        ), patch("ops_agent.cli._load_snapshot_data", return_value=None) as loader:
            self.assertEqual(cmd_supervisor_run(args), 1)
            loader.assert_not_called()

    def test_supervisor_rule_does_not_touch_the_model(self):
        args = argparse.Namespace(
            planner="rule", limit="3", offline=True, snapshot="missing.json", days=30, out=None
        )
        with patch("ops_agent.cli.cmd_director_run") as director, patch(
            "ops_agent.cli._load_snapshot_data", return_value=None
        ):
            self.assertEqual(cmd_supervisor_run(args), 1)
            director.assert_not_called()

    def test_agent_run_passes_auto_through_to_the_workshop(self):
        args = argparse.Namespace(
            offline=True,
            snapshot=None,
            limit="auto",
            days=30,
            market="US",
            planner="llm",
            tasks_out=None,
            proposals_out=None,
            base_url=None,
        )
        with patch("ops_agent.cli.cmd_supervisor_run", return_value=0) as supervisor, patch(
            "ops_agent.cli.cmd_workshop_run", return_value=0
        ) as workshop, patch("ops_agent.cli.cmd_review_run", return_value=0) as review:
            self.assertEqual(cmd_agent_run(args), 0)
            self.assertEqual(supervisor.call_args[0][0].limit, "auto")
            self.assertEqual(workshop.call_args[0][0].limit, "auto")
            self.assertEqual(supervisor.call_args[0][0].planner, "llm")
            self.assertEqual(review.call_args[0][0].limit, "auto")


class DueExperimentTests(unittest.TestCase):
    """The director judges closed experiments itself: copy only, no model call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = MemoryStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, started_at: str, window: int = 14) -> dict:
        record = build_record(
            product_id="SKU-1001",
            variant_id="SKU-1001-v1",
            started_at=started_at,
            window_days=window,
            baseline={"days": 9, "impressions": 8606, "clicks": 549, "orders": 20,
                      "order_rate": 0.002324, "click_rate": 0.0638, "return_rate": 0.0},
            task={"min_lift": 0.15},
        )
        self.memory.append_experiment(record)
        return record

    def test_closed_experiment_is_judged_and_saved(self):
        self.add("2026-10-10")
        judged = _judge_due_experiments(self.memory, as_of="2026-11-03")
        self.assertEqual(len(judged), 1)
        self.assertEqual(judged[0]["verdict"], "inconclusive")
        saved = self.memory.read_experiments(limit=5)[0]
        self.assertEqual(saved["verdict"], "inconclusive")
        self.assertIn("judged_at", saved)

    def test_open_experiment_is_left_alone(self):
        self.add("2026-10-25")
        self.assertEqual(_judge_due_experiments(self.memory, as_of="2026-11-03"), [])
        self.assertEqual(self.memory.read_experiments(limit=5)[0]["verdict"], "")

    def test_already_judged_is_not_recounted(self):
        self.add("2026-10-10")
        _judge_due_experiments(self.memory, as_of="2026-11-03")
        self.assertEqual(_judge_due_experiments(self.memory, as_of="2026-11-03"), [])
        self.assertEqual(len(_judge_due_experiments(self.memory, as_of="2026-11-03", rejudge=True)), 1)


if __name__ == "__main__":
    unittest.main()
