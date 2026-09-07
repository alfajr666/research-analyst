import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import polars as pl

import config
from strategies.v2.ema9_adx_stochrsi_state_v1 import evaluate_exit, evaluate_symbol


def _bars(count, minutes, close=100.0, end=None):
    end = end or datetime(2026, 8, 1, tzinfo=timezone.utc)
    start = end - timedelta(minutes=minutes * (count - 1))
    return pl.DataFrame([
        {"timestamp": start + timedelta(minutes=minutes * i), "open": close,
         "high": close + 1.0, "low": close - 1.0, "close": close, "volume": 1.0}
        for i in range(count)
    ])


class Ema9AdxStochRsiStateTests(unittest.TestCase):
    def test_missing_symbol_bars_are_rejected_without_schema_error(self):
        event = evaluate_symbol(
            pl.DataFrame(), _bars(60, 60), asset="AKE", symbol="AKEUSDT",
            cutoff=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        self.assertIsNone(event)

    def test_long_candidate_uses_completed_5m_trigger_and_1h_adx(self):
        cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
        bars5 = _bars(80, 5, end=cutoff).with_columns(
            pl.when(pl.arange(0, 80) == 79).then(pl.lit(101.0))
            .otherwise(pl.col("close")).alias("close")
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
            event = evaluate_symbol(bars5, _bars(60, 60, end=cutoff), asset="BTC",
                                     symbol="BTCUSDT", cutoff=cutoff)
        self.assertIsNotNone(event)
        self.assertEqual(event["direction"], "long")
        self.assertEqual(event["plugin_version"], "v2")
        self.assertEqual(event["metadata"]["execution_timeframe"], "5m")

    def test_adx_at_threshold_rejects(self):
        with patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._dmi_adx",
            return_value=(20.0, 30.0, 10.0),
        ):
            event = evaluate_symbol(
                _bars(80, 5), _bars(60, 60), asset="BTC", symbol="BTCUSDT",
                cutoff=datetime(2026, 8, 1, 1, 19, tzinfo=timezone.utc),
            )
        self.assertIsNone(event)

    def test_short_candidate_uses_bearish_cross_and_upper_structure_stop(self):
        cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
        bars5 = _bars(80, 5, end=cutoff).with_columns(
            pl.when(pl.arange(0, 80) == 79).then(pl.lit(99.0))
            .otherwise(pl.col("close")).alias("close")
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
            event = evaluate_symbol(bars5, _bars(60, 60, end=cutoff), asset="BTC",
                                     symbol="BTCUSDT", cutoff=cutoff)
        self.assertIsNotNone(event)
        self.assertEqual(event["direction"], "short")
        self.assertGreater(event["invalidation_price"], event["entry_price"])

    def test_stale_5m_data_rejects_before_signal_evaluation(self):
        cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
        stale_end = cutoff - timedelta(seconds=config.DATA_FRESHNESS_MAX_SECONDS + 1)
        with patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._stoch_values",
            side_effect=AssertionError("stale data must not calculate signals"),
        ):
            event = evaluate_symbol(
                _bars(80, 5, end=stale_end), _bars(60, 60, end=cutoff),
                asset="BTC", symbol="BTCUSDT", cutoff=cutoff,
            )
        self.assertIsNone(event)

    def test_momentum_exit_uses_5m_bars_after_entry(self):
        bars5 = _bars(40, 5)
        k = [50.0] * 40
        d = [50.0] * 40
        k[25] = 10.0
        k[-2], d[-2], k[-1], d[-1] = 10.0, 20.0, 40.0, 35.0
        with patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._stoch_values",
            return_value=([50.0] * 40, k, d),
        ), patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._rsi_series",
            return_value=[20.0] * 40,
        ):
            signal = evaluate_exit(
                bars5, side="short", opened_at=bars5["timestamp"][20],
                cutoff=bars5["timestamp"][-1],
            )
        self.assertEqual(signal["rule_name"], "short_momentum_exit")


if __name__ == "__main__":
    unittest.main()
