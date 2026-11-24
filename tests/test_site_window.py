"""Site-window reconstruction for experiments picked from the live store.

The site exposes rolling windows only, so a closed window is rebuilt as
``metrics(window + age) - metrics(age)`` and handed to the judge as rows whose
totals add up to that slice.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.cli import _site_window_rows  # noqa: E402
from ops_agent.site_platform import SiteAPIError  # noqa: E402


class FakeSite:
    def __init__(self, totals=None, error=None):
        self.totals = totals or {}
        self.error = error
        self.calls: list[tuple] = []

    def window_totals(self, product_id, *, days_total, days_cut=0):
        self.calls.append((product_id, days_total, days_cut))
        if self.error:
            raise self.error
        return dict(self.totals)


def record(**over):
    base = {
        "product_id": "p1",
        "started_at": "2026-10-01",
        "window_days": 14,
    }
    base.update(over)
    return base


class SiteWindowRowsTests(unittest.TestCase):
    def test_slice_is_spread_over_the_window(self):
        site = FakeSite({"impressions": 1400, "orders": 14, "clicks": 0})
        rows = _site_window_rows(site, record(), as_of="2026-10-20")
        self.assertEqual(len(rows), 14)
        self.assertEqual(sum(row["impressions"] for row in rows), 1400)
        self.assertEqual(sum(row["orders"] for row in rows), 14)
        # 整除的余数落在首行，保证总量不丢
        self.assertEqual(rows[0]["impressions"], 100)

    def test_remainder_stays_in_the_total(self):
        site = FakeSite({"impressions": 1001, "orders": 3, "clicks": 0})
        rows = _site_window_rows(site, record(), as_of="2026-10-14")
        self.assertEqual(sum(row["impressions"] for row in rows), 1001)
        self.assertEqual(sum(row["orders"] for row in rows), 3)

    def test_two_reads_are_requested_for_a_late_judgement(self):
        site = FakeSite({"impressions": 100, "orders": 1, "clicks": 0})
        _site_window_rows(site, record(), as_of="2026-10-20")
        # 窗口 10-01..10-15 已过 5 天 → 读 19 天，再减 5 天
        self.assertEqual(site.calls, [("p1", 19, 5)])

    def test_fresh_window_reads_once(self):
        site = FakeSite({"impressions": 100, "orders": 1, "clicks": 0})
        _site_window_rows(site, record(), as_of="2026-10-15")
        self.assertEqual(site.calls, [("p1", 14, 0)])

    def test_empty_totals_mean_no_rows(self):
        site = FakeSite({})
        self.assertEqual(_site_window_rows(site, record(), as_of="2026-10-20"), [])

    def test_site_error_does_not_escape(self):
        site = FakeSite(error=SiteAPIError(500, "boom", "boom"))
        self.assertEqual(_site_window_rows(site, record(), as_of="2026-10-20"), [])

    def test_unparsable_start_returns_nothing(self):
        site = FakeSite({"impressions": 100})
        self.assertEqual(_site_window_rows(site, record(started_at="broken"), as_of="2026-10-20"), [])

    def test_no_site_returns_nothing(self):
        self.assertEqual(_site_window_rows(None, record(), as_of="2026-10-20"), [])


if __name__ == "__main__":
    unittest.main()
