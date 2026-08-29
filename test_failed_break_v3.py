import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import polars as pl

from failed_break_v3 import SUPPORTED_ASSETS, evaluate_symbol


def _bars(n=40):
    start = datetime(2026, 8, 29, tzinfo=timezone.utc)
    ts = [start + timedelta(minutes=i * 5) for i in range(n)]
    return pl.DataFrame({"timestamp": ts, "open": [100.0] * n, "high": [101.0] * n,
                         "low": [99.0] * n, "close": [100.0] * n, "volume": [1.0] * n})


class FailedBreakV3Tests(unittest.TestCase):
    def test_exact_symbol_scope(self):
        self.assertEqual(SUPPORTED_ASSETS, frozenset(("BTC", "ETH", "PAXG", "QQQ")))

    def test_emits_concrete_entry_stop_and_two_r_fallback(self):
        setup = {"direction": "long", "stop": 98.0, "swing": 99.0,
                 "armed_at": datetime(2026, 8, 29, tzinfo=timezone.utc)}
        with patch("failed_break_v3._latest_setup", return_value=setup), patch(
            "failed_break_v3._stoch_rsi", return_value=(pl.Series([10.0, 15.0]), pl.Series([20.0, 12.0]))
        ):
            event = evaluate_symbol(_bars(), _bars(), asset="BTC", symbol="BTCUSDT",
                                    cutoff=datetime(2026, 8, 29, 3, 20, tzinfo=timezone.utc))
        self.assertIsNotNone(event)
        self.assertEqual(event["entry_price"], 100.0)
        self.assertEqual(event["stop_loss"], 98.0)
        self.assertEqual(event["take_profit"], 104.0)
        self.assertEqual(event["targets"], [104.0])

    def test_invalidates_when_price_crosses_opposite_side(self):
        setup = {"direction": "long", "stop": 98.0, "swing": 99.0,
                 "armed_at": datetime(2026, 8, 29, tzinfo=timezone.utc)}
        with patch("failed_break_v3._latest_setup", return_value=None):
            self.assertIsNone(evaluate_symbol(_bars(), _bars(), asset="BTC", symbol="BTCUSDT",
                                               cutoff=datetime(2026, 8, 29, 3, 20, tzinfo=timezone.utc)))


if __name__ == "__main__":
    unittest.main()
