"""任务单必须自带数据日期：旧照片最容易被当成刚体检过。"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.cli import _apply_freshness, _freshness  # noqa: E402


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _snapshot(self, fetched_at) -> Path:
        path = self.dir / "site_snapshot.json"
        payload = {"store": {"slug": "globaltrade"}, "products": []}
        if fetched_at is not None:
            payload["fetched_at"] = fetched_at
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _args(self, offline: bool, snapshot: Path | None = None):
        return SimpleNamespace(offline=offline, snapshot=str(snapshot) if snapshot else None)

    def test_live_run_is_fresh_by_definition(self):
        result = _freshness(self._args(offline=False))
        self.assertEqual(result["data_source"], "live")
        self.assertEqual(result["data_as_of"], date.today().isoformat())
        self.assertEqual(result["snapshot_age_days"], 0)

    def test_offline_uses_snapshot_fetched_at(self):
        day = date.today() - timedelta(days=3)
        path = self._snapshot(f"{day.isoformat()}T10:33:52")
        result = _freshness(self._args(offline=True, snapshot=path))
        self.assertEqual(result["data_as_of"], day.isoformat())
        self.assertEqual(result["snapshot_age_days"], 3)
        self.assertEqual(result["data_source"], "snapshot")

    def test_snapshot_without_fetched_at_stays_unknown(self):
        path = self._snapshot(None)
        result = _freshness(self._args(offline=True, snapshot=path))
        self.assertEqual(result["data_as_of"], "")
        self.assertIsNone(result["snapshot_age_days"])

    def test_unparsable_fetched_at_is_unknown_not_today(self):
        path = self._snapshot("昨天下午")
        result = _freshness(self._args(offline=True, snapshot=path))
        self.assertEqual(result["data_as_of"], "")
        self.assertIsNone(result["snapshot_age_days"])

    def test_missing_snapshot_is_unknown_not_fatal(self):
        result = _freshness(self._args(offline=True, snapshot=self.dir / "nope.json"))
        self.assertEqual(result["data_as_of"], "")

    def test_stale_snapshot_prints_warning(self):
        day = date.today() - timedelta(days=40)
        path = self._snapshot(f"{day.isoformat()}T08:00:00")
        payload = {}
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _apply_freshness(payload, self._args(offline=True, snapshot=path))
        self.assertEqual(payload["snapshot_age_days"], 40)
        self.assertIn("已过期 40 天", buffer.getvalue())

    def test_fresh_snapshot_does_not_warn(self):
        day = date.today() - timedelta(days=2)
        path = self._snapshot(f"{day.isoformat()}T08:00:00")
        payload = {}
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _apply_freshness(payload, self._args(offline=True, snapshot=path))
        self.assertNotIn("WARN", buffer.getvalue())
        self.assertIn("2 天前", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
