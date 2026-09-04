import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import polars as pl

import config
from strategies.v2.ema9_adx_stochrsi_state_v1 import (
    STRATEGY_ID,
    evaluate_exit,
    evaluate_symbol,
)


def _bars(count, minutes, close=100.0, end=None):
    end = end or datetime(2026, 8, 1, tzinfo=timezone.utc)
    start = end - timedelta(minutes=minutes * (count - 1))
    return pl.DataFrame([
        {
            "timestamp": start + timedelta(minutes=minutes * i),
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1.0,
        }
        for i in range(count)
    ])


class Ema9AdxStochRsiStateTests(unittest.TestCase):
    def test_missing_symbol_bars_are_rejected_without_schema_error(self):
        event = evaluate_symbol(
            pl.DataFrame(), _bars(40, 5), _bars(60, 60),
            asset="AKE", symbol="AKEUSDT", cutoff=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        self.assertIsNone(event)

    def test_long_candidate_requires_adx_and_all_completed_structure(self):
        cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
        bars1 = _bars(80, 1, end=cutoff)
        bars1 = bars1.with_columns(
            pl.when(pl.arange(0, bars1.height) == bars1.height - 1)
            .then(pl.lit(101.0))
            .otherwise(pl.col("close"))
            .alias("close")
        )
        bars5 = _bars(40, 5, end=cutoff)
        bars1h = _bars(60, 60, end=cutoff)
        k = [50.0] * 80
        d = [50.0] * 80
        k[-2], d[-2], k[-1], d[-1] = 20.0, 30.0, 40.0, 30.0
        with patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._stoch_values",
            return_value=([50.0] * 80, k, d),
        ), patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._dmi_adx",
            return_value=(21.0, 30.0, 10.0),
        ):
            event = evaluate_symbol(
                bars1, bars5, bars1h, asset="BTC", symbol="BTCUSDT",
                cutoff=cutoff,
            )
        self.assertIsNotNone(event)
        self.assertEqual(event["strategy_id"], STRATEGY_ID)
        self.assertEqual(event["direction"], "long")
        self.assertEqual(event["targets"], [])
        self.assertNotIn("quantity", event)
        self.assertLess(event["invalidation_price"], event["entry_price"])

    def test_adx_at_threshold_rejects(self):
        with patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._dmi_adx",
            return_value=(20.0, 30.0, 10.0),
        ):
            event = evaluate_symbol(
                _bars(80, 1), _bars(40, 5), _bars(60, 60),
                asset="BTC", symbol="BTCUSDT",
                cutoff=datetime(2026, 8, 1, 1, 19, tzinfo=timezone.utc),
            )
        self.assertIsNone(event)

    def test_short_candidate_uses_bearish_cross_and_upper_structure_stop(self):
        cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
        bars1 = _bars(80, 1, end=cutoff).with_columns(
            pl.when(pl.arange(0, 80) == 79)
            .then(pl.lit(99.0))
            .otherwise(pl.col("close"))
            .alias("close")
        )
        k = [50.0] * 80
        d = [50.0] * 80
        k[-2], d[-2], k[-1], d[-1] = 30.0, 20.0, 10.0, 20.0
        with patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._stoch_values",
            return_value=([50.0] * 80, k, d),
        ), patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._dmi_adx",
            return_value=(21.0, 10.0, 30.0),
        ):
            event = evaluate_symbol(
                bars1, _bars(40, 5, end=cutoff), _bars(60, 60, end=cutoff),
                asset="BTC", symbol="BTCUSDT", cutoff=cutoff,
            )
        self.assertIsNotNone(event)
        self.assertEqual(event["direction"], "short")
        self.assertGreater(event["invalidation_price"], event["entry_price"])

    def test_structure_uses_the_latest_fifteen_completed_bars_only(self):
        cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
        bars1 = _bars(80, 1, end=cutoff).with_columns(
            pl.when(pl.arange(0, 80) == 79)
            .then(pl.lit(101.0))
            .otherwise(pl.col("close"))
            .alias("close")
        )
        bars5 = _bars(40, 5, end=cutoff).with_columns(
            pl.when(pl.arange(0, 40) == 24)
            .then(pl.lit(90.0))
            .otherwise(pl.col("close"))
            .alias("close")
        )
        k = [50.0] * 80
        d = [50.0] * 80
        k[-2], d[-2], k[-1], d[-1] = 20.0, 30.0, 40.0, 30.0
        with patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._stoch_values",
            return_value=([50.0] * 80, k, d),
        ), patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._dmi_adx",
            return_value=(21.0, 30.0, 10.0),
        ):
            event = evaluate_symbol(
                bars1, bars5, _bars(60, 60, end=cutoff), asset="BTC",
                symbol="BTCUSDT", cutoff=cutoff,
            )
        self.assertIsNotNone(event)

    def test_stale_one_minute_data_rejects_before_signal_evaluation(self):
        cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
        stale_end = cutoff - timedelta(seconds=config.DATA_FRESHNESS_MAX_SECONDS + 1)
        with patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._stoch_values",
            side_effect=AssertionError("stale data must not calculate signals"),
        ):
            event = evaluate_symbol(
                _bars(80, 1, end=stale_end), _bars(40, 5, end=cutoff),
                _bars(60, 60, end=cutoff), asset="BTC", symbol="BTCUSDT",
                cutoff=cutoff,
            )
        self.assertIsNone(event)

    def test_short_momentum_exit_requires_an_extreme_after_entry(self):
        bars1 = _bars(80, 1)
        bars5 = _bars(40, 5)
        k1 = [50.0] * 80
        d1 = [50.0] * 80
        k1[60] = 10.0
        k1[-2], d1[-2], k1[-1], d1[-1] = 10.0, 20.0, 40.0, 35.0
        k5 = [50.0] * 40
        d5 = [50.0] * 40
        opened_at = bars1["timestamp"][50]
        with patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._stoch_values",
            side_effect=[([50.0] * 40, k5, d5), ([50.0] * 80, k1, d1)],
        ), patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._rsi_series",
            return_value=[20.0] * 40,
        ):
            signal = evaluate_exit(
                bars1, bars5, side="short", opened_at=opened_at,
                cutoff=bars1["timestamp"][-1],
            )
        self.assertEqual(signal["rule_name"], "short_momentum_exit")

    def test_exit_requires_post_entry_extreme_and_reports_extension_first(self):
        cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
        bars1 = _bars(80, 1, end=cutoff)
        bars5 = _bars(40, 5, end=cutoff).with_columns(
            pl.when(pl.arange(0, 40) == 39)
            .then(pl.lit(110.0))
            .otherwise(pl.col("close"))
            .alias("close")
        )
        k1 = [50.0] * 80
        d1 = [50.0] * 80
        k1[60] = 85.0
        k1[-2], d1[-2], k1[-1], d1[-1] = 70.0, 60.0, 50.0, 60.0
        k5 = [50.0] * 40
        d5 = [50.0] * 40
        k5[-2], d5[-2], k5[-1], d5[-1] = 70.0, 75.0, 85.0, 80.0
        rsi5 = [60.0] * 40
        rsi5[-1] = 80.0
        opened_at = bars1["timestamp"][50]
        with patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._stoch_values",
            side_effect=[([50.0] * 40, k5, d5), ([50.0] * 80, k1, d1)],
        ), patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._rsi_series",
            return_value=rsi5,
        ):
            signal = evaluate_exit(
                bars1, bars5, side="long", opened_at=opened_at,
                cutoff=cutoff,
            )
        self.assertEqual(signal["rule_name"], "long_extension_tp")


if __name__ == "__main__":
    unittest.main()
