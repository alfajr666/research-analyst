import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from accumulation_evaluator import event_from_setup
from alpha_outbox import write_event
from signal_publisher import SignalPublisher
from two_pool_discovery import process_snapshot


class RecordingTransport:
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)
        return '{"ok":true}'


class AlphaPipelineEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_db_path = config.DB_PATH
        self.previous_alpha = config.ALPHA_DB_PATH
        self.previous_llm = config.LLM_RESEARCH_ENABLED
        config.DB_PATH = os.path.join(self.directory.name, "market_data.db")
        config.ALPHA_DB_PATH = os.path.join(self.directory.name, "alpha_events.db")
        config.LLM_RESEARCH_ENABLED = False
        config.init_db(config.DB_PATH)
        config.init_db(config.ALPHA_DB_PATH)
        self.outbox = Path(self.directory.name) / "alpha_outbox"
        self.now = datetime(2026, 8, 16, 12, 15, tzinfo=timezone.utc)

    def tearDown(self):
        config.DB_PATH = self.previous_db_path
        config.ALPHA_DB_PATH = self.previous_alpha
        config.LLM_RESEARCH_ENABLED = self.previous_llm
        self.directory.cleanup()

    def test_discovery_to_delivery_persists_one_portable_event(self):
        record = {
            "symbol": "SOLUSDT_PERP.A", "asset": "SOL", "liquidity_tier": "core",
            "eligible": True, "data_fresh": True, "history_warmed": True,
            "volume_24h_usd": 200_000_000, "open_interest_usd": 10_000_000,
            "volume_zscore": 0.2, "oi_change_1h": 0.04, "price_change_1h": 0.003,
            "price_change_24h": 0.02, "price_range_percentile": 0.15,
            "funding_rate": 0.0001, "funding_zscore": 0.1, "long_short_ratio_change": 0.03,
        }
        conn = config.get_db_connection()
        process_snapshot(conn, self.now, [record])
        conn.commit()
        self.assertEqual(conn.execute("SELECT status FROM deep_backfill_jobs").fetchone(), ("pending",))
        conn.close()

        event = event_from_setup("SOL", "SOLUSDT_PERP.A", "scanner", {
            "vol_spike": 2.0, "price_change_1h": 0.3,
        }, {
            "direction": "long", "ema_99": 100.0, "ema_distance": 0.005,
            "close": 100.5, "open": 100.0, "bar_timestamp": self.now,
        })
        event["plugin_version"] = "v1"
        event["input_snapshot_id"] = "cutoff-test-001"
        event["feature_snapshot"]["fvg_4h"] = "support"
        event["feature_snapshot"]["order_block_1h"] = "neutral"
        # simulate distinct approx VP
        event["feature_snapshot"]["coinalyze_candle_distributed_volume_profile_v1"] = {"poc": 100.2}
        created, _ = write_event(event, self.outbox)
        self.assertTrue(created)

        transport = RecordingTransport()
        publisher = SignalPublisher(config.ALPHA_DB_PATH, self.outbox, transport, now=lambda: self.now + timedelta(minutes=1))
        self.assertEqual(publisher.run_once(), {"persisted": 1, "sent": 1, "failed": 0, "invalid": 0})
        self.assertEqual(publisher.run_once(), {"persisted": 0, "sent": 0, "failed": 0, "invalid": 0})
        self.assertEqual(len(transport.messages), 1)

        conn = config.get_db_connection(read_only=True, db_path=config.ALPHA_DB_PATH)
        try:
            self.assertEqual(conn.execute("SELECT asset, status FROM alpha_events").fetchall(), [("SOL", "active")])
            self.assertEqual(conn.execute("SELECT status, attempt_number FROM signal_deliveries").fetchall(), [("sent", 1)])
            self.assertEqual(conn.execute("SELECT asset, setup_class FROM alpha_candidates").fetchall(), [("SOL", "accumulation_base")])
            # contract: event identity + evidence from plugins
            row = conn.execute("SELECT event_json FROM alpha_events").fetchone()
            persisted = json.loads(row[0])
            self.assertEqual(persisted.get("plugin_version"), "v1")
            self.assertEqual(persisted.get("input_snapshot_id"), "cutoff-test-001")
            self.assertIn("fvg_4h", persisted["feature_snapshot"])
            self.assertNotEqual(persisted["feature_snapshot"].get("coinalyze_candle_distributed_volume_profile_v1"), None)
        finally:
            conn.close()

    def test_expired_triggered_event_records_forward_outcome(self):
        event = event_from_setup("SOL", "SOLUSDT_PERP.A", "scanner", {
            "vol_spike": 2.0, "price_change_1h": 0.3,
        }, {
            "direction": "long", "ema_99": 100.0, "ema_distance": 0.005,
            "close": 100.5, "open": 100.0, "bar_timestamp": self.now,
        })
        write_event(event, self.outbox)
        conn = config.get_db_connection()
        try:
            for minutes, close, high, low in ((0, 100.5, 101.0, 99.9), (15, 101.5, 102.0, 100.8), (60, 103.0, 104.0, 101.0)):
                ts = self.now + timedelta(minutes=minutes)
                # source_observations (sole source post-drop)
                payload = json.dumps({"open": close, "high": high, "low": low, "close": close, "volume": 100})
                conn.execute("""
                    INSERT OR IGNORE INTO source_observations (observation_id, source, venue, native_symbol, asset, market_kind, interval, source_start, source_end, retrieved_at, retrieval_kind, payload_json)
                    VALUES (?, 'coinalyze', 'aggregate_perp', 'SOLUSDT_PERP.A', 'SOL', 'perpetual', '15m', ?, ?, ?, 'live', ?)
                """, (f"e2e-{ts.isoformat()}", ts, ts, ts, payload))
            conn.commit()
        finally:
            conn.close()

        transport = RecordingTransport()
        publisher = SignalPublisher(config.ALPHA_DB_PATH, self.outbox, transport, now=lambda: self.now)
        publisher.run_once()
        publisher.now = lambda: self.now + timedelta(hours=5)
        publisher.run_once()

        conn = config.get_db_connection(read_only=True, db_path=config.ALPHA_DB_PATH)
        try:
            outcome, return_15m, return_1h = conn.execute(
                "SELECT outcome, return_15m, return_1h FROM alpha_outcomes"
            ).fetchone()
            self.assertIn(outcome, {"expired", "target", "invalidated", "ambiguous_same_bar"})
            self.assertGreater(return_15m, 0)
            self.assertGreater(return_1h, return_15m)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
