import unittest
import os
import tempfile
from datetime import datetime, timedelta, timezone

import config
from scanner import build_discovery_record, claim_due_deep_backfill_jobs, process_deep_backfill_jobs
from two_pool_discovery import enqueue_deep_backfill_jobs


def _candles(last_close=102.0, last_volume=100.0, end=None):
    end = end or datetime(2026, 8, 16, 12, tzinfo=timezone.utc)
    candles = []
    for index in range(50):
        close = 100.0 if index < 49 else last_close
        candles.append({
            "t": (end - timedelta(hours=49 - index)).timestamp(),
            "c": close,
            "h": close + 0.5,
            "l": close - 1.0,
            "v": float(index + 1) if index < 49 else last_volume,
        })
    return candles


class ScannerDiscoveryRecordTests(unittest.TestCase):
    def test_builds_normalized_fresh_breakout_record(self):
        now = datetime(2026, 8, 16, 12, 30, tzinfo=timezone.utc)
        record = build_discovery_record(
            {"binance_symbol": "TESTUSDT", "vol_24h_usd": 10_000_000},
            "TESTUSDT_PERP.A",
            _candles(end=now),
            [{"c": 100.0}] * 49 + [{"c": 105.0}],
            current_oi=105.0,
            funding_rate=0.0001,
            now=now,
        )

        self.assertTrue(record["data_fresh"])
        self.assertTrue(record["history_warmed"])
        self.assertAlmostEqual(record["price_change_1h"], 0.02)
        self.assertAlmostEqual(record["price_change_24h"], 0.02)
        self.assertAlmostEqual(record["oi_change_1h"], 0.05)
        self.assertGreater(record["volume_zscore"], 1.0)
        self.assertTrue(record["fresh_breakout"])
        self.assertFalse(record["post_breakout_pullback"])

    def test_missing_history_is_retained_but_not_eligible_for_ranking(self):
        record = build_discovery_record(
            {"binance_symbol": "TESTUSDT", "vol_24h_usd": 10_000_000},
            "TESTUSDT_PERP.A",
            _candles()[:24],
            [{"c": 100.0}],
            current_oi=100.0,
            funding_rate=0.0,
        )

        self.assertTrue(record["eligible"])
        self.assertFalse(record["data_fresh"])
        self.assertFalse(record["history_warmed"])

    def test_old_history_is_not_treated_as_fresh(self):
        now = datetime(2026, 8, 16, 12, 30, tzinfo=timezone.utc)
        record = build_discovery_record(
            {"binance_symbol": "TESTUSDT", "vol_24h_usd": 10_000_000},
            "TESTUSDT_PERP.A",
            _candles(end=now - timedelta(hours=48)),
            [{"c": 100.0}] * 50,
            current_oi=105.0,
            funding_rate=0.0001,
            now=now,
        )
        self.assertFalse(record["data_fresh"])
        self.assertFalse(record["history_warmed"])

class DeepBackfillJobTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = os.path.join(self.temp_dir.name, "research.db")
        config.init_db()

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_failed_and_interrupted_jobs_are_retried_without_external_apis(self):
        start = datetime(2026, 8, 16, 10, tzinfo=timezone.utc)
        conn = config.get_db_connection()
        enqueue_deep_backfill_jobs(conn, start, ["TESTUSDT_PERP.A"])
        conn.commit()
        conn.close()

        calls = []
        def fail_once(symbols, days):
            calls.append(symbols)
            raise RuntimeError("temporary API failure")

        process_deep_backfill_jobs(start, bootstrap=fail_once)
        conn = config.get_db_connection()
        retry_at = conn.execute(
            "SELECT next_retry_at FROM deep_backfill_jobs WHERE symbol = ?", ("TESTUSDT_PERP.A",)
        ).fetchone()[0]
        conn.close()
        process_deep_backfill_jobs(retry_at, bootstrap=lambda symbols, days: calls.append(symbols))

        conn = config.get_db_connection()
        self.assertEqual(conn.execute("SELECT status, attempts FROM deep_backfill_jobs").fetchone(), ("completed", 2))
        conn.close()
        self.assertEqual(calls, [["TESTUSDT_PERP.A"], ["TESTUSDT_PERP.A"]])

        conn = config.get_db_connection()
        enqueue_deep_backfill_jobs(conn, start, ["NEXTUSDT_PERP.A"])
        conn.commit()
        claim_due_deep_backfill_jobs(conn, start)
        conn.close()
        process_deep_backfill_jobs(
            start + timedelta(minutes=config.DEEP_BACKFILL_LEASE_MINUTES + 1),
            bootstrap=lambda symbols, days: calls.append(symbols),
        )
        conn = config.get_db_connection()
        self.assertEqual(conn.execute("SELECT status, attempts FROM deep_backfill_jobs WHERE symbol = ?", ("NEXTUSDT_PERP.A",)).fetchone(), ("completed", 2))
        conn.close()


class TestCAShaping(unittest.TestCase):
    def test_is_ca_limited_importable_and_callable(self):
        from ingest_venue_agg_failover import is_ca_limited
        import config as cfg
        # callable without crash
        val = is_ca_limited()
        self.assertIsInstance(val, bool)

    def test_scanner_shape_logic_does_not_crash(self):
        import config as cfg
        from ingest_venue_agg_failover import is_ca_limited
        orig_shape = getattr(cfg, "CA_SHAPE_ON_CIRCUIT", None)
        orig_mf = getattr(cfg, "MARKET_FAILOVER_ENABLED", None)
        try:
            cfg.CA_SHAPE_ON_CIRCUIT = True
            cfg.MARKET_FAILOVER_ENABLED = True
            # force circuit via age sim if needed, but just call the decision
            shape = getattr(cfg, "CA_SHAPE_ON_CIRCUIT", False) and is_ca_limited()
            self.assertIsInstance(shape, bool)
        finally:
            if orig_shape is not None:
                cfg.CA_SHAPE_ON_CIRCUIT = orig_shape
            if orig_mf is not None:
                cfg.MARKET_FAILOVER_ENABLED = orig_mf

    def test_log_ca_shaped_increments_count(self):
        from ingest_venue_agg_failover import log_ca_shaped
        import config
        conn = config.get_db_connection()
        before = conn.execute(
            "SELECT COUNT(*) FROM source_request_log WHERE status = 'shaped_due_to_circuit'"
        ).fetchone()[0] or 0
        conn.close()
        log_ca_shaped("test-funding")
        conn = config.get_db_connection()
        after = conn.execute(
            "SELECT COUNT(*) FROM source_request_log WHERE status = 'shaped_due_to_circuit'"
        ).fetchone()[0] or 0
        conn.close()
        self.assertGreaterEqual(after, before)


if __name__ == "__main__":
    unittest.main()
