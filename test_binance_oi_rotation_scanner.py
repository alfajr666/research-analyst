import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import config
from binance_oi_rotation_scanner import (
    build_feed,
    build_observation,
    completed_hour,
    publish_feed_atomic,
    qualify_and_rank,
    run_scanner,
)
from binance_oi_rotation_worker import run_due_scan


INTERVAL = datetime(2026, 8, 16, 11, tzinfo=timezone.utc)


def _market(symbol="TESTUSDT"):
    return {
        "symbol": symbol,
        "baseAsset": symbol.removesuffix("USDT"),
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "contractType": "PERPETUAL",
        "status": "TRADING",
    }


def _history(interval=INTERVAL, final_oi=130_000_000, final_volume=10_000_000, base_volume=1_000_000):
    start = interval - timedelta(hours=8)
    oi, candles = [], []
    for index in range(9):
        timestamp = start + timedelta(hours=index)
        oi_value = 100_000_000 + index * 1_000_000
        if timestamp == interval:
            oi_value = final_oi
        oi.append({"timestamp": int(timestamp.timestamp() * 1000), "sumOpenInterestValue": str(oi_value)})
        quote_volume = base_volume + index * 10_000
        if timestamp == interval:
            quote_volume = final_volume
        close = 100 + index
        candles.append([int(timestamp.timestamp() * 1000), "0", "0", "0", str(close), "0", "0", str(quote_volume)])
    return oi, candles


class BinanceOIRotationDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "BINANCE_OI_ROTATION_MIN_24H_VOLUME_USD": config.BINANCE_OI_ROTATION_MIN_24H_VOLUME_USD,
            "BINANCE_OI_ROTATION_MIN_OI_DELTA_USD": config.BINANCE_OI_ROTATION_MIN_OI_DELTA_USD,
            "BINANCE_OI_ROTATION_MIN_OI_PERCENTILE": config.BINANCE_OI_ROTATION_MIN_OI_PERCENTILE,
            "BINANCE_OI_ROTATION_MIN_VOLUME_ANOMALY": config.BINANCE_OI_ROTATION_MIN_VOLUME_ANOMALY,
            "BINANCE_OI_ROTATION_HISTORY_HOURS": config.BINANCE_OI_ROTATION_HISTORY_HOURS,
        }
        config.BINANCE_OI_ROTATION_MIN_24H_VOLUME_USD = 5_000_000
        config.BINANCE_OI_ROTATION_MIN_OI_DELTA_USD = 5_000_000
        config.BINANCE_OI_ROTATION_MIN_OI_PERCENTILE = 0.9
        config.BINANCE_OI_ROTATION_MIN_VOLUME_ANOMALY = 1.0
        config.BINANCE_OI_ROTATION_HISTORY_HOURS = 8

    def tearDown(self):
        for name, value in self.settings.items():
            setattr(config, name, value)

    def test_completed_hour_never_uses_the_in_progress_bar(self):
        self.assertEqual(completed_hour(datetime(2026, 8, 16, 12, 59, tzinfo=timezone.utc)), INTERVAL)

    def test_disabled_rotation_scanner_does_not_open_the_database(self):
        with patch.object(config, "BINANCE_OI_ROTATION_ENABLED", False), patch(
            "binance_oi_rotation_worker.config.get_db_connection"
        ) as get_connection:
            self.assertFalse(run_due_scan(INTERVAL + timedelta(hours=1)))
        get_connection.assert_not_called()

    def test_liquidity_data_quality_and_feature_gates(self):
        oi, candles = _history()
        qualified = build_observation(_market(), {"quoteVolume": "10000000"}, oi, candles, INTERVAL)
        thin_oi, thin_candles = _history(final_volume=1, base_volume=1)
        thin = build_observation(_market("THINUSDT"), {"quoteVolume": "1"}, thin_oi, thin_candles, INTERVAL)
        incomplete = build_observation(_market("MISSUSDT"), {"quoteVolume": "10000000"}, oi[:-1], candles, INTERVAL)

        self.assertTrue(qualified["is_eligible"])
        self.assertGreater(qualified["oi_change_1h_usd"], 5_000_000)
        self.assertGreaterEqual(qualified["oi_spike_percentile"], 0.9)
        self.assertGreater(qualified["volume_anomaly"], 1)
        self.assertEqual(thin["rejection_reason"], "below_liquidity_floor")
        self.assertEqual(incomplete["rejection_reason"], "incomplete_completed_hour")

    def test_non_boundary_oi_observations_align_to_the_completed_hour(self):
        oi, candles = _history()
        for item in oi:
            item["timestamp"] += 15 * 60 * 1000
        record = build_observation(_market(), {"quoteVolume": "10000000"}, oi, candles, INTERVAL)
        self.assertTrue(record["is_eligible"])

    def test_ranking_is_deterministic_and_feed_explicitly_expires(self):
        oi, candles = _history()
        alpha = build_observation(_market("ALPHAUSDT"), {"quoteVolume": "10000000"}, oi, candles, INTERVAL)
        beta = {**alpha, "symbol": "BETAUSDT", "asset": "BETA", "oi_change_1h_usd": alpha["oi_change_1h_usd"] - 1}
        ranked = qualify_and_rank([beta, alpha])
        feed = build_feed(INTERVAL, ranked, generated_at=INTERVAL + timedelta(hours=1))

        self.assertEqual([item["symbol"] for item in ranked], ["ALPHAUSDT", "BETAUSDT"])
        self.assertEqual(feed["schema_version"], 1)
        self.assertEqual(feed["expires_at"], "2026-08-16T17:00:00+00:00")
        self.assertNotIn("direction", feed["candidates"][0])


class BinanceOIRotationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        self.old_feed_path = config.BINANCE_OI_ROTATION_FEED_PATH
        self.old_max_contracts = config.BINANCE_OI_ROTATION_MAX_CONTRACTS
        self.old_history_hours = config.BINANCE_OI_ROTATION_HISTORY_HOURS
        config.DB_PATH = os.path.join(self.temp_dir.name, "research.db")
        config.BINANCE_OI_ROTATION_FEED_PATH = os.path.join(self.temp_dir.name, "rotation-feed.json")
        config.BINANCE_OI_ROTATION_MAX_CONTRACTS = 5
        config.BINANCE_OI_ROTATION_HISTORY_HOURS = 8
        config.init_db()

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        config.BINANCE_OI_ROTATION_FEED_PATH = self.old_feed_path
        config.BINANCE_OI_ROTATION_MAX_CONTRACTS = self.old_max_contracts
        config.BINANCE_OI_ROTATION_HISTORY_HOURS = self.old_history_hours
        self.temp_dir.cleanup()

    def test_scan_persists_rejections_deduplicates_event_and_publishes_atomic_feed(self):
        oi, candles = _history()

        class Client:
            def eligible_markets(self):
                return [_market(), _market("THINUSDT")], {
                    "TESTUSDT": {"quoteVolume": "10000000"},
                    "THINUSDT": {"quoteVolume": "1"},
                }

            def history(self, symbol, interval):
                if symbol == "THINUSDT":
                    return _history(final_volume=1, base_volume=1)
                return oi, candles

        first = run_scanner(now=INTERVAL + timedelta(hours=1, minutes=2), client=Client())
        second = run_scanner(now=INTERVAL + timedelta(hours=1, minutes=3), client=Client())
        with open(config.BINANCE_OI_ROTATION_FEED_PATH, encoding="utf-8") as handle:
            on_disk = json.load(handle)
        conn = config.get_db_connection(read_only=True)
        observations = conn.execute("SELECT COUNT(*) FROM binance_oi_rotation_observations").fetchone()[0]
        raw_oi = conn.execute("SELECT COUNT(*) FROM binance_oi_rotation_raw_oi_history").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM binance_oi_rotation_events").fetchone()[0]
        watchlist = conn.execute("SELECT state, deep_backfill_required FROM binance_oi_rotation_watchlist_history").fetchall()
        warmup_job = conn.execute("SELECT status FROM deep_backfill_jobs WHERE symbol = 'TESTUSDT_PERP.A'").fetchone()
        conn.close()

        self.assertEqual(observations, 2)
        self.assertEqual(raw_oi, len(oi) * 2)
        self.assertEqual(events, 1)
        self.assertEqual(watchlist, [("entered", True)])
        self.assertEqual(warmup_job, ("pending",))
        self.assertEqual(first["candidates"], second["candidates"])
        self.assertEqual(on_disk["completed_interval_at"], INTERVAL.isoformat())
        self.assertFalse(os.path.exists(config.BINANCE_OI_ROTATION_FEED_PATH + ".tmp"))

    def test_incomplete_scan_is_retried_until_marked_complete(self):
        oi, candles = _history()

        class FailingThenWorkingClient:
            def __init__(self):
                self.calls = 0

            def eligible_markets(self):
                return [_market()], {"TESTUSDT": {"quoteVolume": "10000000"}}

            def history(self, symbol, interval):
                self.calls += 1
                if self.calls == 1:
                    raise __import__("httpx").HTTPError("temporary")
                return oi, candles

        client = FailingThenWorkingClient()
        run_scanner(now=INTERVAL + timedelta(hours=1, minutes=2), client=client)
        run_scanner(now=INTERVAL + timedelta(hours=1, minutes=3), client=client)
        conn = config.get_db_connection(read_only=True)
        status = conn.execute("SELECT status FROM binance_oi_rotation_scans").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM binance_oi_rotation_events").fetchone()[0]
        conn.close()
        self.assertEqual(status, "complete")
        self.assertEqual(events, 1)

    def test_atomic_writer_replaces_valid_document(self):
        path = config.BINANCE_OI_ROTATION_FEED_PATH
        publish_feed_atomic({"complete": True}, path)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"complete": True})

    def test_worker_runs_only_missing_completed_hour(self):
        from binance_oi_rotation_worker import run_due_scan

        now = INTERVAL + timedelta(hours=1, minutes=4)
        with patch("binance_oi_rotation_worker.run_scanner", return_value={"candidates": []}) as scanner:
            self.assertTrue(run_due_scan(now))
        scanner.assert_called_once_with(now=now)

        conn = config.get_db_connection()
        conn.execute(
            "INSERT INTO binance_oi_rotation_scans VALUES (?, ?, ?, ?, ?)",
            ("binance_usdm", INTERVAL, config.BINANCE_OI_ROTATION_SCANNER_VERSION, "complete", now),
        )
        conn.commit()
        conn.close()
        self.assertFalse(run_due_scan(now))


if __name__ == "__main__":
    unittest.main()
