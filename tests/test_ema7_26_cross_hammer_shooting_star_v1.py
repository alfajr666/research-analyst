import math
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import polars as pl

import config
from strategies.v2.ema7_26_cross_hammer_shooting_star_v1 import (
    STRATEGY_ID,
    evaluate_exit,
    evaluate_symbol,
)
from strategy_plugins import _REGISTRY


def _bars(count, minutes, close=100.0, end=None):
    end = end or datetime(2026, 8, 1, tzinfo=timezone.utc)
    start = end - timedelta(minutes=minutes * (count - 1))
    return pl.DataFrame([
        {
            "timestamp": start + timedelta(minutes=minutes * i),
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1.0,
        }
        for i in range(count)
    ])


def _long_setup_bars(cutoff):
    bars = _bars(40, 5, end=cutoff)
    return bars.with_columns(
        pl.when(pl.arange(0, 40) == 38).then(pl.lit(99.8)).otherwise(pl.col("open")).alias("open"),
        pl.when(pl.arange(0, 40) == 38).then(pl.lit(100.05)).otherwise(
            pl.when(pl.arange(0, 40) == 39).then(pl.lit(101.5)).otherwise(pl.col("high"))
        ).alias("high"),
        pl.when(pl.arange(0, 40) == 39).then(pl.lit(101.0)).otherwise(pl.col("close")).alias("close"),
        pl.when(pl.arange(0, 40) == 38).then(pl.lit(98.0)).otherwise(
            pl.when(pl.arange(0, 40) == 39).then(pl.lit(99.5)).otherwise(pl.col("low"))
        ).alias("low"),
    )


def _short_setup_bars(cutoff):
    bars = _bars(40, 5, end=cutoff)
    return bars.with_columns(
        pl.when(pl.arange(0, 40) == 38).then(pl.lit(100.2)).otherwise(pl.col("open")).alias("open"),
        pl.when(pl.arange(0, 40) == 38).then(pl.lit(99.95)).otherwise(
            pl.when(pl.arange(0, 40) == 39).then(pl.lit(98.5)).otherwise(pl.col("low"))
        ).alias("low"),
        pl.when(pl.arange(0, 40) == 39).then(pl.lit(99.0)).otherwise(pl.col("close")).alias("close"),
        pl.when(pl.arange(0, 40) == 38).then(pl.lit(102.0)).otherwise(
            pl.when(pl.arange(0, 40) == 39).then(pl.lit(100.5)).otherwise(pl.col("high"))
        ).alias("high"),
    )


class Ema7CrossHammerTests(unittest.TestCase):
    def test_missing_symbol_bars_are_rejected_without_schema_error(self):
        event = evaluate_symbol(
            pl.DataFrame(), _bars(60, 60),
            asset="AKE", symbol="AKEUSDT", cutoff=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        self.assertIsNone(event)

    def test_registered_at_five_minute_cadence_and_enabled(self):
        self.assertEqual(_REGISTRY[STRATEGY_ID].cadence, "5m")
        self.assertIn(STRATEGY_ID, config.STRATEGY_ENABLED_IDS)

    def test_long_cross_uses_prior_hammer_and_current_atr_stop(self):
        cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
        with patch(
            "strategies.v2.ema7_26_cross_hammer_shooting_star_v1._dmi_adx",
            return_value=(20.0, 30.0, 10.0),
        ), patch(
            "strategies.v2.ema7_26_cross_hammer_shooting_star_v1._rsi_series",
            return_value=[50.0] * 40,
        ), patch(
            "strategies.v2.ema7_26_cross_hammer_shooting_star_v1.wilder_atr",
            return_value=1.0,
        ):
            event = evaluate_symbol(
                _long_setup_bars(cutoff), _bars(60, 60, end=cutoff),
                asset="BTC", symbol="BTCUSDT", cutoff=cutoff,
            )
        self.assertIsNotNone(event)
        self.assertEqual(event["direction"], "long")
        self.assertAlmostEqual(event["entry_price"], 101.0)
        self.assertAlmostEqual(event["invalidation_price"], 97.0)
        self.assertEqual(event["targets"], [])
        self.assertEqual(event["valid_until"], "2026-08-01T00:05:00+00:00")

    def test_short_cross_uses_prior_shooting_star_and_upper_stop(self):
        cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
        with patch(
            "strategies.v2.ema7_26_cross_hammer_shooting_star_v1._dmi_adx",
            return_value=(20.0, 10.0, 30.0),
        ), patch(
            "strategies.v2.ema7_26_cross_hammer_shooting_star_v1._rsi_series",
            return_value=[50.0] * 40,
        ), patch(
            "strategies.v2.ema7_26_cross_hammer_shooting_star_v1.wilder_atr",
            return_value=1.0,
        ):
            event = evaluate_symbol(
                _short_setup_bars(cutoff), _bars(60, 60, end=cutoff),
                asset="BTC", symbol="BTCUSDT", cutoff=cutoff,
            )
        self.assertIsNotNone(event)
        self.assertEqual(event["direction"], "short")
        self.assertAlmostEqual(event["entry_price"], 99.0)
        self.assertAlmostEqual(event["invalidation_price"], 103.0)

    def test_adx_below_threshold_rejects_before_setup_scan(self):
        with patch(
            "strategies.v2.ema7_26_cross_hammer_shooting_star_v1._dmi_adx",
            return_value=(19.99, 30.0, 10.0),
        ), patch(
            "strategies.v2.ema7_26_cross_hammer_shooting_star_v1._find_setup",
            side_effect=AssertionError("setup must not be scanned"),
        ):
            event = evaluate_symbol(
                _bars(40, 5), _bars(60, 60),
                asset="BTC", symbol="BTCUSDT",
                cutoff=datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
        self.assertIsNone(event)

    def test_nonfinite_adx_rejects_before_setup_scan(self):
        with patch(
            "strategies.v2.ema7_26_cross_hammer_shooting_star_v1._dmi_adx",
            return_value=(math.nan, 30.0, 10.0),
        ), patch(
            "strategies.v2.ema7_26_cross_hammer_shooting_star_v1._find_setup",
            side_effect=AssertionError("setup must not be scanned"),
        ):
            event = evaluate_symbol(
                _bars(40, 5), _bars(60, 60),
                asset="BTC", symbol="BTCUSDT",
                cutoff=datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
        self.assertIsNone(event)

    def test_missing_current_5m_cutoff_rejects_old_cross(self):
        cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
        event = evaluate_symbol(
            _bars(40, 5, end=cutoff - timedelta(minutes=5)),
            _bars(60, 60, end=cutoff),
            asset="BTC", symbol="BTCUSDT", cutoff=cutoff,
        )
        self.assertIsNone(event)

    def test_gapped_5m_history_rejects_signal(self):
        cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
        bars = _long_setup_bars(cutoff).with_columns(
            pl.when(pl.arange(0, 40) == 20)
            .then(pl.col("timestamp") + timedelta(minutes=10))
            .otherwise(pl.col("timestamp"))
            .alias("timestamp")
        )
        event = evaluate_symbol(
            bars, _bars(60, 60, end=cutoff),
            asset="BTC", symbol="BTCUSDT", cutoff=cutoff,
        )
        self.assertIsNone(event)

    def test_symmetric_proximity_and_zero_body_rules(self):
        from strategies.v2.ema7_26_cross_hammer_shooting_star_v1 import (
            _candle_pattern,
            _near_ema26,
        )

        self.assertTrue(_near_ema26(100.25, 100.0))
        self.assertFalse(_near_ema26(100.26, 100.0))
        self.assertFalse(_near_ema26(99.74, 100.0))
        self.assertIsNone(_candle_pattern(100.0, 100.0, 100.0, 100.0))

    def test_exit_thresholds_are_strict(self):
        bars = _bars(40, 5, close=101.0)
        with patch(
            "strategies.v2.ema7_26_cross_hammer_shooting_star_v1._ema_rsi_spread",
            return_value=(27.99, 100.0, 100.6, 60.0),
        ):
            long_exit = evaluate_exit(bars, side="long", cutoff=bars["timestamp"][-1])
        self.assertEqual(long_exit["rule_name"], "long_rsi_ema_spread_exit")

        with patch(
            "strategies.v2.ema7_26_cross_hammer_shooting_star_v1._ema_rsi_spread",
            return_value=(28.0, 100.0, 100.6, 60.0),
        ):
            no_exit = evaluate_exit(bars, side="long", cutoff=bars["timestamp"][-1])
        self.assertIsNone(no_exit)

        with patch(
            "strategies.v2.ema7_26_cross_hammer_shooting_star_v1._ema_rsi_spread",
            return_value=(72.01, 100.0, 100.6, 50.01),
        ):
            short_exit = evaluate_exit(bars, side="short", cutoff=bars["timestamp"][-1])
        self.assertEqual(short_exit["rule_name"], "short_rsi_ema_spread_exit")


if __name__ == "__main__":
    unittest.main()
