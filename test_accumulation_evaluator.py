import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config
from accumulation_detection import confluence
from accumulation_evaluator import confidence_from_setup, event_from_setup, fresh_scanner_symbols, run_once


NOW = datetime(2026, 8, 16, 12, 17, tzinfo=timezone.utc)


def candidate(symbol="SOLUSDT_PERP.A"):
    return {
        symbol: {
            "asset": "SOL",
            "source": "scanner",
            "accumulation": {"vol_spike": 2.0, "price_change_1h": 0.3, "hour_volume": 123.0},
            "setup": {
                "direction": "long", "ema_99": 100.0, "ema_distance": 0.005,
                "close": 100.5, "open": 100.0,
                "bar_timestamp": datetime(2026, 8, 16, 12, tzinfo=timezone.utc),
            },
        }
    }


class AccumulationEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = os.path.join(self.temp_dir.name, "research.db")
        config.init_db()

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_scanner_input_requires_a_fresh_timestamp(self):
        pending = Path(self.temp_dir.name) / "pending.json"
        symbols = {"SOLUSDT_PERP.A": {"underlying": "SOL", "vol_spike": 2.0, "price_change_1h": 0.3}}
        pending.write_text(json.dumps({"scanner_timestamp": NOW.isoformat(), "symbols": symbols}))
        self.assertIn("SOLUSDT_PERP.A", fresh_scanner_symbols(pending, NOW))
        pending.write_text(json.dumps({"scanner_timestamp": (NOW - timedelta(minutes=76)).isoformat(), "symbols": symbols}))
        self.assertEqual(fresh_scanner_symbols(pending, NOW), {})

    def test_confluence_requires_a_fresh_completed_bar(self):
        conn = config.get_db_connection()
        try:
            latest = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)
            for index in range(100):
                timestamp = latest - timedelta(minutes=15 * (99 - index))
                close = 100.0 + index * 0.005
                # source_observations only (post-drop)
                payload = json.dumps({"open": close-0.02, "close": close, "volume": 10})
                conn.execute(
                    "INSERT OR IGNORE INTO source_observations (observation_id, source, venue, native_symbol, asset, market_kind, interval, source_start, source_end, retrieved_at, retrieval_kind, payload_json) VALUES (?, 'coinalyze', 'agg', 'SOLUSDT_PERP.A', 'SOL', 'perpetual', '15m', ?, ?, ?, 'live', ?)",
                    (f"acc-{timestamp.isoformat()}", timestamp, timestamp, timestamp, payload)
                )
            conn.commit()
            self.assertIsNotNone(confluence(conn, "SOLUSDT_PERP.A", datetime(2026, 8, 16, 12, 15, tzinfo=timezone.utc)))
            self.assertIsNone(confluence(conn, "SOLUSDT_PERP.A", datetime(2026, 8, 16, 13, tzinfo=timezone.utc)))
        finally:
            conn.close()

    def test_event_payload_has_accumulation_context(self):
        item = candidate()["SOLUSDT_PERP.A"]
        event = event_from_setup("SOL", "SOLUSDT_PERP.A", "scanner", item["accumulation"], item["setup"])
        self.assertEqual(event["strategy_id"], "accumulation-base-v1")
        self.assertEqual(event["setup_class"], "accumulation_base")
        self.assertEqual(event["direction"], "long")
        self.assertGreater(event["valid_until"], event["observed_at"])
        self.assertEqual(event["feature_snapshot"]["source"], "scanner")
        self.assertEqual(event["feature_snapshot"]["volume_spike_multiple"], 2.0)
        self.assertEqual(event["feature_snapshot"]["quiet_price_change_1h_pct"], 0.3)
        self.assertEqual(event["feature_snapshot"]["ema_99"], 100.0)
        self.assertEqual(event["confidence"], 0.6793)
        self.assertEqual(event["feature_snapshot"]["confidence_components"], {
            "volume": 0.175,
            "quietness": 0.18,
            "ema_proximity": 0.125,
            "candle_strength": 0.1493,
            "directional_alignment": 0.05,
        })

    def test_confidence_changes_with_market_strength(self):
        item = candidate()["SOLUSDT_PERP.A"]
        strong, _ = confidence_from_setup(item["accumulation"], item["setup"])
        weak, _ = confidence_from_setup(
            {"vol_spike": 1.5, "price_change_1h": 2.9, "hour_volume": 123.0},
            {**item["setup"], "ema_distance": 0.0099, "close": 100.01, "open": 100.0},
        )
        self.assertGreater(strong, weak)

    def test_new_entry_is_emitted_once_and_exit_clears_state(self):
        state = Path(self.temp_dir.name) / "state.json"
        outbox = Path(self.temp_dir.name) / "outbox"
        with patch("accumulation_evaluator.evaluate", side_effect=[candidate(), candidate(), {}]):
            first = run_once(NOW, state, outbox_dir=outbox)
            second = run_once(NOW + timedelta(minutes=15), state, outbox_dir=outbox)
            third = run_once(NOW + timedelta(minutes=30), state, outbox_dir=outbox)
        self.assertEqual(first, {"active": 1, "entered": 1, "emitted": 1, "exited": 0})
        self.assertEqual(second, {"active": 1, "entered": 0, "emitted": 0, "exited": 0})
        self.assertEqual(third, {"active": 0, "entered": 0, "emitted": 0, "exited": 1})
        self.assertEqual(len(list(outbox.glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
