import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import polars as pl

import config
from strategies.v2.ema99_double_touch_stochrsi_state_v1 import (
    STRATEGY_ID,
    evaluate_exit,
    evaluate_symbol,
)
from strategy_plugins import _REGISTRY


def _bars(count, minutes, close=100.0, end=None):
    end = end or datetime(2026, 8, 1, tzinfo=timezone.utc)
    start = end - timedelta(minutes=minutes * (count - 1))
    return pl.DataFrame([
        {"timestamp": start + timedelta(minutes=minutes * i), "open": close,
         "high": close + 1.0, "low": close - 1.0, "close": close,
         "volume": 1.0}
        for i in range(count)
    ])


class Ema99DoubleTouchTests(unittest.TestCase):
    def test_missing_symbol_bars_are_rejected_without_schema_error(self):
        event = evaluate_symbol(
            pl.DataFrame(), _bars(40, 5), _bars(60, 60),
            asset="AKE", symbol="AKEUSDT", cutoff=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        self.assertIsNone(event)

    def test_registered_at_five_minute_cadence_and_enabled(self):
        self.assertEqual(_REGISTRY[STRATEGY_ID].cadence, "5m")
        self.assertIn(STRATEGY_ID, config.STRATEGY_ENABLED_IDS)

    def test_long_entry_uses_true_one_minute_trigger_and_saved_touch_stop(self):
        cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
        bars1 = _bars(140, 1, end=cutoff).with_columns(
            pl.when(pl.arange(0, 140) == 139)
            .then(pl.lit(101.0))
            .otherwise(pl.col("close"))
            .alias("close")
        )
        k = [50.0] * 140
        d = [50.0] * 140
        k[-2], d[-2], k[-1], d[-1] = 10.0, 20.0, 20.0, 10.0
        rsi = [50.0] * 140
        with patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1._stoch_values",
            return_value=([50.0] * 140, k, d),
        ), patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1._rsi_series",
            return_value=rsi,
        ), patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1._rsi5_series",
            return_value=[50.0] * 40,
        ), patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1._dmi_adx",
            return_value=(20.0, 30.0, 10.0),
        ), patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1._adx_series",
            return_value=[20.0] * 140,
        ), patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1._recent_cross",
            return_value=True,
        ):
            event = evaluate_symbol(
                bars1, _bars(40, 5, end=cutoff), _bars(60, 60, end=cutoff),
                asset="BTC", symbol="BTCUSDT", cutoff=cutoff,
            )
        self.assertIsNotNone(event)
        self.assertEqual(event["strategy_id"], STRATEGY_ID)
        self.assertEqual(event["direction"], "long")
        self.assertAlmostEqual(event["invalidation_price"], 97.0)
        self.assertEqual(event["targets"], [])

    def test_adx_below_minimum_rejects_before_touch_replay(self):
        with patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1._dmi_adx",
            return_value=(19.99, 30.0, 10.0),
        ), patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1._stoch_values",
            side_effect=AssertionError("ADX gate must run first"),
        ):
            event = evaluate_symbol(
                _bars(140, 1), _bars(40, 5), _bars(60, 60),
                asset="BTC", symbol="BTCUSDT",
                cutoff=datetime(2026, 8, 1, 1, 19, tzinfo=timezone.utc),
            )
        self.assertIsNone(event)

    def test_second_touch_requires_a_later_candle(self):
        bars = _bars(99, 1)
        with patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1._stoch_values",
            return_value=([50.0] * 99, [50.0] * 99, [50.0] * 99),
        ):
            state = __import__(
                "strategies.v2.ema99_double_touch_stochrsi_state_v1",
                fromlist=["_replay_touch_state"],
            )._replay_touch_state(bars, [20.0] * 99)
        self.assertTrue(state["long_touch1"])
        self.assertFalse(state["long_touch2"])

        bars = _bars(100, 1)
        state = __import__(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1",
            fromlist=["_replay_touch_state"],
        )._replay_touch_state(bars, [20.0] * 100)
        self.assertTrue(state["long_touch2"])

    def test_long_and_short_dynamic_exits(self):
        bars5 = _bars(40, 5, close=104.0)
        with patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1._rsi5_series",
            return_value=[71.0] * 40,
        ), patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1.ema_series",
            return_value=[100.0] * 40,
        ):
            long_signal = evaluate_exit(
                bars5, side="long", cutoff=bars5["timestamp"][-1],
            )
        self.assertEqual(long_signal["rule_name"], "long_ema26_rsi_exit")

        bars5 = _bars(40, 5, close=96.0)
        with patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1._rsi5_series",
            return_value=[29.0] * 40,
        ), patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1.ema_series",
            return_value=[100.0] * 40,
        ):
            short_signal = evaluate_exit(
                bars5, side="short", cutoff=bars5["timestamp"][-1],
            )
        self.assertEqual(short_signal["rule_name"], "short_ema26_rsi_exit")


if __name__ == "__main__":
    unittest.main()
