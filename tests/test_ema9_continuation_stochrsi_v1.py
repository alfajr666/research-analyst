import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import polars as pl

from strategies.compact.ema9_continuation_stochrsi_v1 import STRATEGY_ID, _atr, evaluate_symbol


def _bars(count, minutes, base=100.0):
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return pl.DataFrame([
        {"timestamp": start + timedelta(minutes=minutes * i), "open": base, "high": base + 1,
         "low": base - 1, "close": base, "volume": 1.0}
        for i in range(count)
    ])


class Ema9ContinuationStochRsiTests(unittest.TestCase):
    def test_builds_long_signal_with_two_r_target_and_exit_metadata(self):
        bars5 = _bars(40, 5)
        bars1 = _bars(50, 1)
        bars1 = bars1.with_columns(
            pl.when(pl.arange(0, bars1.height) == bars1.height - 1)
            .then(pl.lit(101.0))
            .otherwise(pl.col("close"))
            .alias("close")
        )
        # The oscillator fixture represents prior oversold memory and a current bull cross.
        k = [None] * 50
        d = [None] * 50
        rsi = [None] * 50
        for i in range(18, 50):
            k[i], d[i], rsi[i] = 50.0, 50.0, 50.0
        k[20] = 10.0
        k[-2], d[-2], k[-1], d[-1] = 20.0, 30.0, 40.0, 30.0
        with patch("strategies.compact.ema9_continuation_stochrsi_v1._stoch_rsi", return_value=(k, d, rsi)):
            event = evaluate_symbol(bars5, bars1, asset="BTC", symbol="BTC/USDT:USDT", cutoff=bars5["timestamp"][-1] + timedelta(minutes=5))
        self.assertIsNotNone(event)
        self.assertEqual(event["strategy_id"], STRATEGY_ID)
        entry = event["entry_condition"]["price"]
        stop = event["invalidation_price"]
        self.assertAlmostEqual(event["targets"][0], entry + 2 * (entry - stop))
        self.assertEqual(event["metadata"]["protective_take_profit_r"], 2.0)
        self.assertIn("long", event["metadata"]["strategy_exits"])

    def test_restricts_assets_before_indicator_evaluation(self):
        with patch("strategies.compact.ema9_continuation_stochrsi_v1._stoch_rsi", side_effect=AssertionError):
            self.assertIsNone(evaluate_symbol(_bars(40, 5), _bars(50, 1), asset="SOL", symbol="SOL", cutoff=datetime.now(timezone.utc)))

    def test_atr_is_true_range_average(self):
        self.assertEqual(_atr(_bars(20, 5)), 2.0)


if __name__ == "__main__":
    unittest.main()
