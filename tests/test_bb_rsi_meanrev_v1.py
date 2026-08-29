import unittest
from datetime import datetime, timezone

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


if __name__ == "__main__":
    unittest.main()
