import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import config
from orchestrator import _parse_timestamp, _source_observation_ids, _summarize_interval_results


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

    def test_cutoff_provenance_is_limited_to_matching_closed_bar(self):
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "market.db"
            config.init_market_db(db_path)
            conn = config.get_db_connection(db_path=db_path)
            cutoff = datetime(2026, 8, 29, 12, 5, tzinfo=timezone.utc)
            for observation_id, source_end in (("before", cutoff.replace(minute=0)), ("at", cutoff), ("after", cutoff.replace(minute=10))):
                conn.execute(
                    """INSERT INTO source_observations
                       (observation_id, source, venue, native_symbol, asset, market_kind,
                        interval, source_start, source_end, retrieved_at, retrieval_kind, payload_json)
                       VALUES (?, 'bybit_ws', 'bybit', 'BTCUSDT', 'BTC', 'perpetual',
                               '5m', ?, ?, ?, 'test', '{}')""",
                    (observation_id, source_end, source_end, source_end),
                )
            conn.commit()
            conn.close()
            with patch.object(config, "MARKET_DB_PATH", db_path):
                assert _source_observation_ids(cutoff) == ["at"]


if __name__ == "__main__":
    unittest.main()
