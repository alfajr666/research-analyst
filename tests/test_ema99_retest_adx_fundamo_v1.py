import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import polars as pl

import config
from strategy_plugins import _REGISTRY
from strategies.v2.ema99_retest_adx_fundamo_v1 import (
    STRATEGY_ID,
    evaluate_exit,
    evaluate_stop_revision,
    evaluate_symbol,
)


def _bars(count=6, *, close=100.0, end=None):
    end = end or datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    start = end - timedelta(minutes=5 * (count - 1))
    return pl.DataFrame([
        {
            "timestamp": start + timedelta(minutes=5 * index),
            "open": close,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "volume": 1.0,
        }
        for index in range(count)
    ])


class Ema99RetestFundamoTests(unittest.TestCase):
    def test_single_bidirectional_plugin_is_registered_but_not_live_enabled(self):
        self.assertEqual(_REGISTRY[STRATEGY_ID].cadence, "5m")
        self.assertNotIn(STRATEGY_ID, config.STRATEGY_ENABLED_IDS)

    def test_long_cross_then_ema99_retest_emits_targetless_candidate(self):
        cutoff = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        bars5m = _bars(close=100.2, end=cutoff).with_columns(
            pl.when(pl.arange(0, 6) == 5).then(pl.lit(100.05)).otherwise(pl.col("close")).alias("close"),
            pl.when(pl.arange(0, 6) == 5).then(pl.lit(99.0)).otherwise(pl.col("low")).alias("low"),
        )
        bars1h = _bars(40, end=cutoff)
        fast = [90.0, 95.0, 101.0, 102.0, 103.0, 104.0]
        slow = [100.0] * 6
        with patch(
            "strategies.v2.ema99_retest_adx_fundamo_v1.ema_series",
            side_effect=[fast, slow],
        ), patch(
            "strategies.v2.ema99_retest_adx_fundamo_v1._expand_adx_to_5m",
            return_value=[30.0] * 6,
        ), patch(
            "strategies.v2.ema99_retest_adx_fundamo_v1.wilder_atr",
            return_value=1.5,
        ):
            event = evaluate_symbol(
                bars5m, bars1h, asset="BTC", symbol="BTCUSDT", cutoff=cutoff,
            )
        self.assertIsNotNone(event)
        self.assertEqual(event["strategy_id"], STRATEGY_ID)
        self.assertEqual(event["direction"], "long")
        self.assertEqual(event["phase"], "long_retest")
        self.assertEqual(event["targets"], [])
        self.assertAlmostEqual(event["invalidation_price"], 96.0)
        self.assertEqual(event["metadata"]["target_policy"], "executor_derived_2r")

    def test_short_cross_then_ema99_retest_emits_short_candidate(self):
        cutoff = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        bars5m = _bars(close=99.8, end=cutoff).with_columns(
            pl.when(pl.arange(0, 6) == 5).then(pl.lit(99.95)).otherwise(pl.col("close")).alias("close"),
            pl.when(pl.arange(0, 6) == 5).then(pl.lit(101.0)).otherwise(pl.col("high")).alias("high"),
        )
        bars1h = _bars(40, end=cutoff)
        fast = [110.0, 105.0, 99.0, 98.0, 97.0, 96.0]
        slow = [100.0] * 6
        with patch(
            "strategies.v2.ema99_retest_adx_fundamo_v1.ema_series",
            side_effect=[fast, slow],
        ), patch(
            "strategies.v2.ema99_retest_adx_fundamo_v1._expand_adx_to_5m",
            return_value=[30.0] * 6,
        ), patch(
            "strategies.v2.ema99_retest_adx_fundamo_v1.wilder_atr",
            return_value=1.5,
        ):
            event = evaluate_symbol(
                bars5m, bars1h, asset="BTC", symbol="BTCUSDT", cutoff=cutoff,
            )
        self.assertEqual(event["direction"], "short")
        self.assertEqual(event["phase"], "short_retest")
        self.assertAlmostEqual(event["invalidation_price"], 104.0)

    def test_retest_requires_wick_and_close_on_correct_side(self):
        bars = _bars()
        with patch(
            "strategies.v2.ema99_retest_adx_fundamo_v1.ema_series",
            side_effect=[
                [90.0, 95.0, 101.0, 102.0, 103.0, 104.0],
                [100.0] * 6,
            ],
        ), patch(
            "strategies.v2.ema99_retest_adx_fundamo_v1._expand_adx_to_5m",
            return_value=[30.0] * 6,
        ):
            event = evaluate_symbol(
                bars.with_columns(pl.lit(98.0).alias("close")),
                _bars(40, end=bars["timestamp"][-1]),
                asset="BTC", symbol="BTCUSDT", cutoff=bars["timestamp"][-1],
            )
        self.assertIsNone(event)

    def test_mechanical_exits_match_rsi_and_ema26_spread(self):
        bars = _bars(close=100.6)
        with patch(
            "strategies.v2.ema99_retest_adx_fundamo_v1.ema_series",
            return_value=[100.0] * 6,
        ), patch(
            "strategies.v2.ema99_retest_adx_fundamo_v1.wilder_rsi",
            return_value=[72.1] * 6,
        ):
            signal = evaluate_exit(bars, side="long", cutoff=bars["timestamp"][-1])
        self.assertEqual(signal["action"], "exit")
        self.assertEqual(signal["rule_name"], "long_rsi_ema26_spread_exit")

    def test_mechanical_exit_requires_both_conditions(self):
        bars = _bars(close=100.6)
        with patch(
            "strategies.v2.ema99_retest_adx_fundamo_v1.ema_series",
            return_value=[100.0] * 6,
        ), patch(
            "strategies.v2.ema99_retest_adx_fundamo_v1.wilder_rsi",
            return_value=[71.9] * 6,
        ):
            signal = evaluate_exit(bars, side="long", cutoff=bars["timestamp"][-1])
        self.assertIsNone(signal)

    def test_stop_revision_uses_fixed_trigger_extreme_and_closed_bar_atr(self):
        bars = _bars(end=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc))
        with patch(
            "strategies.v2.ema99_retest_adx_fundamo_v1.wilder_atr",
            return_value=2.0,
        ):
            revision = evaluate_stop_revision(
                bars, side="long", trigger_extreme=97.0,
                cutoff=bars["timestamp"][-1],
            )
        self.assertEqual(revision["action"], "update_stop")
        self.assertEqual(revision["stop_loss"], 93.0)


if __name__ == "__main__":
    unittest.main()
