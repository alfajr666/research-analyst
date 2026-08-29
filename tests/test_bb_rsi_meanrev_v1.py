import unittest
from datetime import datetime, timedelta, timezone

import polars as pl

import config
from strategies.compact.bb_rsi_meanrev_v1 import BBRsiMeanRevConfig, evaluate_symbol
from intent_outbox import build_executor_intent, validate_geometry


class BBRsiMeanRevTests(unittest.TestCase):
    def test_restricts_universe(self):
        bars = pl.DataFrame({"timestamp": [datetime.now(timezone.utc)], "open": [1], "high": [2], "low": [0], "close": [1], "volume": [1]})
        self.assertIsNone(evaluate_symbol(bars, asset="SOL", symbol="SOLUSDT_PERP.A", cutoff=datetime.now(timezone.utc)))

    def test_middle_band_below_two_r_is_advisory_only(self):
        event = {"strategy_id": "bb-rsi-meanrev-v1", "asset": "BTC", "direction": "long", "observed_at": "2026-08-29T00:00:00Z", "entry_condition": {"price": 100}, "invalidation_price": 99, "targets": [101]}
        intent = build_executor_intent(event)
        ok, reason = validate_geometry(intent)
        self.assertFalse(ok)
        self.assertIn("below minimum", reason)

    def test_plugin_declares_five_minute_dataset(self):
        previous = config.STRATEGY_ENABLED_IDS
        config.STRATEGY_ENABLED_IDS = ("bb-rsi-meanrev-v1",)
        try:
            from strategy_plugins import load_enabled_plugins
            plugin = load_enabled_plugins()[0]
            self.assertEqual(plugin.required_datasets, ("bars_5m",))
        finally:
            config.STRATEGY_ENABLED_IDS = previous

    def test_evaluation_extracts_scalar_values_from_last_polars_row(self):
        start = datetime(2026, 8, 28, tzinfo=timezone.utc)
        timestamps = [start + timedelta(minutes=i * 5) for i in range(60)]
        closes = [100.0 + (i % 3) for i in range(60)]
        bars = pl.DataFrame({
            "timestamp": timestamps,
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [1.0] * 60,
        })
        # This exercises the last-row access without requiring a signal setup.
        self.assertIsNone(evaluate_symbol(
            bars, asset="BTC", symbol="BTCUSDT", cutoff=timestamps[-1],
        ))


if __name__ == "__main__":
    unittest.main()
