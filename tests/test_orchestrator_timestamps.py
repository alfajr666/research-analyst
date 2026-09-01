import unittest
from datetime import datetime, timezone

from orchestrator import _parse_timestamp, _summarize_interval_results


class OrchestratorTimestampTests(unittest.TestCase):
    def test_parses_sqlite_timestamp_as_utc(self):
        self.assertEqual(
            _parse_timestamp("2026-08-29 12:34:56"),
            datetime(2026, 8, 29, 12, 34, 56, tzinfo=timezone.utc),
        )

    def test_normalizes_offset_timestamp(self):
        self.assertEqual(
            _parse_timestamp("2026-08-29T14:34:56+02:00").hour,
            12,
        )

    def test_summarizes_interval_results_before_reading_summary(self):
        summary = _summarize_interval_results(
            {"strategy": {"emitted": 2}, "skipped": {"skipped": "cadence 15m"}},
            ["BTC", "ETH"],
            {"feed_id": "feed-1"},
        )
        self.assertEqual(summary["strategy_evaluations"], 2)
        self.assertEqual(summary["strategies"]["strategy"]["status"], "completed")
        self.assertEqual(summary["strategies"]["skipped"]["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
