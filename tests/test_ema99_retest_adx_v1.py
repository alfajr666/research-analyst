import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import polars as pl

import config
from strategy_plugins import _REGISTRY
from strategies.v2.ema99_retest_adx_v1 import (
    STRATEGY_ID,
    evaluate_exit,
    evaluate_stop_revision,
    evaluate_symbol,
    run_plugin,
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


def _features(bars, fast, slow, *, rsi=50.0, atr=1.5):
    return bars.with_columns(
        pl.Series(f"ema_{config.EMA99_RETEST_FAST_EMA_LENGTH}", fast),
        pl.Series(f"ema_{config.EMA99_RETEST_SLOW_EMA_LENGTH}", slow),
        pl.Series(f"rsi_{config.EMA99_RETEST_RSI_LENGTH}", [rsi] * bars.height),
        pl.Series(f"atr_{config.EMA99_RETEST_ATR_LENGTH}", [atr] * bars.height),
    )


class Ema99RetestTests(unittest.TestCase):
    def test_single_bidirectional_plugin_is_registered_and_live_active(self):
        self.assertEqual(_REGISTRY[STRATEGY_ID].cadence, "5m")
        self.assertIn(STRATEGY_ID, config.STRATEGY_ENABLED_IDS)
        self.assertIn(STRATEGY_ID, config.STRATEGY_ACTIVE_IDS)

    def test_registry_declares_current_engine_contract(self):
        plugin = _REGISTRY[STRATEGY_ID]
        self.assertEqual(plugin.required_intervals, ("5m", "1h"))
        self.assertTrue(plugin.stateful)
        self.assertEqual(
            plugin.feature_requirements[0][0],
            "5m",
        )
        self.assertEqual(
            set(plugin.feature_requirements[0][1]["ema"]),
            {"ema_26", "ema_99"},
        )

    def test_runtime_adapter_uses_shared_features_and_direct_dmi(self):
        cutoff = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        bars5m = _bars(end=cutoff)
        bars1h = _bars(40, end=cutoff)
        features = _features(
            bars5m,
            [90.0, 95.0, 101.0, 102.0, 103.0, 104.0],
            [100.0] * 6,
        )
        context = MagicMock()
        context.features.return_value = features
        context.dmi_adx.return_value = ([None] * 39 + [30.0], 35.0, 15.0)
        event = {"strategy_id": STRATEGY_ID, "asset": "BTC", "direction": "long"}
        snapshot = {"cutoff_at": cutoff, "_strategy_feature_cache": {}}

        with patch(
            "strategies.v2.ema99_retest_adx_v1.strategy_market_connection",
            return_value=(MagicMock(), False),
        ), patch(
            "strategies.v2.ema99_retest_adx_v1.get_shared_computation_context",
            return_value=context,
        ), patch(
            "strategy_v2_context.get_shared_computation_context",
            return_value=context,
        ), patch(
            "strategies.v2.ema99_retest_adx_v1.evaluation_symbols",
            return_value=[("BTCUSDT", "BTC")],
        ), patch(
            "strategies.v2.ema99_retest_adx_v1.load_bars_for_interval",
            side_effect=[bars5m, bars1h],
        ), patch(
            "strategies.v2.ema99_retest_adx_v1.has_active_event",
            return_value=False,
        ), patch(
            "strategies.v2.ema99_retest_adx_v1.evaluate_symbol",
            return_value=event,
        ):
            events = run_plugin("5m:2026-09-01T12:00:00Z", snapshot)

        self.assertEqual(events, [{**event, "input_snapshot_id": "5m:2026-09-01T12:00:00Z"}])
        context.features.assert_called_once()
        context.dmi_adx.assert_called_once_with(
            "BTCUSDT",
            "1h",
            config.EMA99_RETEST_ADX_LENGTH,
            config.EMA99_RETEST_ADX_SMOOTHING,
        )

    def test_long_cross_then_ema99_retest_emits_targetless_candidate(self):
        cutoff = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        bars5m = _bars(close=100.2, end=cutoff).with_columns(
            pl.when(pl.arange(0, 6) == 5).then(pl.lit(100.05)).otherwise(pl.col("close")).alias("close"),
            pl.when(pl.arange(0, 6) == 5).then(pl.lit(99.0)).otherwise(pl.col("low")).alias("low"),
        )
        bars1h = _bars(40, end=cutoff)
        fast = [90.0, 95.0, 101.0, 102.0, 103.0, 104.0]
        slow = [100.0] * 6
        features = _features(bars5m, fast, slow)
        with patch(
            "strategies.v2.ema99_retest_adx_v1._expand_adx_to_5m",
            return_value=[30.0] * 6,
        ):
            event = evaluate_symbol(
                bars5m, bars1h, asset="BTC", symbol="BTCUSDT", cutoff=cutoff,
                features5m=features,
            )
        self.assertIsNotNone(event)
        self.assertEqual(event["strategy_id"], STRATEGY_ID)
        self.assertEqual(event["direction"], "long")
        self.assertEqual(event["phase"], "long_retest")
        self.assertEqual(event["targets"], [])
        self.assertAlmostEqual(event["invalidation_price"], 96.0)
        self.assertEqual(event["metadata"]["target_policy"], "executor_derived_2r")
        self.assertIn("exit_reference", event["metadata"])
        self.assertNotIn("mechanical_exit", event["metadata"])

    def test_adx_threshold_is_strict(self):
        cutoff = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        bars5m = _bars(close=100.2, end=cutoff).with_columns(
            pl.when(pl.arange(0, 6) == 5).then(pl.lit(100.05)).otherwise(pl.col("close")).alias("close"),
            pl.when(pl.arange(0, 6) == 5).then(pl.lit(99.0)).otherwise(pl.col("low")).alias("low"),
        )
        features = _features(
            bars5m,
            [90.0, 95.0, 101.0, 102.0, 103.0, 104.0],
            [100.0] * 6,
        )
        bars1h = _bars(40, end=cutoff)
        for adx, expected in ((25.0, False), (25.01, True)):
            with patch(
                "strategies.v2.ema99_retest_adx_v1._expand_adx_to_5m",
                return_value=[adx] * 6,
            ):
                event = evaluate_symbol(
                    bars5m, bars1h, asset="BTC", symbol="BTCUSDT", cutoff=cutoff,
                    features5m=features,
                )
            self.assertEqual(event is not None, expected, f"ADX={adx}")

    def test_retest_distance_boundary_is_inclusive(self):
        cutoff = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        bars1h = _bars(40, end=cutoff)
        fast = [90.0, 95.0, 101.0, 102.0, 103.0, 104.0]
        slow = [100.0] * 6
        for close, expected in ((100.1, True), (100.1001, False)):
            bars5m = _bars(close=100.2, end=cutoff).with_columns(
                pl.when(pl.arange(0, 6) == 5).then(pl.lit(close)).otherwise(pl.col("close")).alias("close"),
                pl.when(pl.arange(0, 6) == 5).then(pl.lit(99.0)).otherwise(pl.col("low")).alias("low"),
            )
            with patch(
                "strategies.v2.ema99_retest_adx_v1._expand_adx_to_5m",
                return_value=[30.0] * 6,
            ):
                event = evaluate_symbol(
                    bars5m, bars1h, asset="BTC", symbol="BTCUSDT", cutoff=cutoff,
                    features5m=_features(bars5m, fast, slow),
                )
            self.assertEqual(event is not None, expected, f"close={close}")

    def test_future_1h_data_cannot_change_cutoff_candidate(self):
        cutoff = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        bars5m = _bars(close=100.2, end=cutoff).with_columns(
            pl.when(pl.arange(0, 6) == 5).then(pl.lit(100.05)).otherwise(pl.col("close")).alias("close"),
            pl.when(pl.arange(0, 6) == 5).then(pl.lit(99.0)).otherwise(pl.col("low")).alias("low"),
        )
        features = _features(
            bars5m,
            [90.0, 95.0, 101.0, 102.0, 103.0, 104.0],
            [100.0] * 6,
        )
        direct = _bars(40, end=cutoff)
        future = pl.concat([direct, _bars(1, end=cutoff + timedelta(minutes=30))])
        with patch(
                "strategies.v2.ema99_retest_adx_v1._expand_adx_to_5m",
            return_value=[30.0] * 6,
        ):
            baseline = evaluate_symbol(
                bars5m, direct, asset="BTC", symbol="BTCUSDT", cutoff=cutoff,
                features5m=features,
            )
            event = evaluate_symbol(
                bars5m, future, asset="BTC", symbol="BTCUSDT", cutoff=cutoff,
                features5m=features,
            )
        self.assertEqual(event, baseline)

    def test_short_cross_then_ema99_retest_emits_short_candidate(self):
        cutoff = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        bars5m = _bars(close=99.8, end=cutoff).with_columns(
            pl.when(pl.arange(0, 6) == 5).then(pl.lit(99.95)).otherwise(pl.col("close")).alias("close"),
            pl.when(pl.arange(0, 6) == 5).then(pl.lit(101.0)).otherwise(pl.col("high")).alias("high"),
        )
        bars1h = _bars(40, end=cutoff)
        fast = [110.0, 105.0, 99.0, 98.0, 97.0, 96.0]
        slow = [100.0] * 6
        features = _features(bars5m, fast, slow)
        with patch(
            "strategies.v2.ema99_retest_adx_v1._expand_adx_to_5m",
            return_value=[30.0] * 6,
        ):
            event = evaluate_symbol(
                bars5m, bars1h, asset="BTC", symbol="BTCUSDT", cutoff=cutoff,
                features5m=features,
            )
        self.assertEqual(event["direction"], "short")
        self.assertEqual(event["phase"], "short_retest")
        self.assertAlmostEqual(event["invalidation_price"], 104.0)

    def test_retest_requires_wick_and_close_on_correct_side(self):
        bars = _bars()
        features = _features(
            bars,
            [90.0, 95.0, 101.0, 102.0, 103.0, 104.0],
            [100.0] * 6,
        )
        with patch(
            "strategies.v2.ema99_retest_adx_v1._expand_adx_to_5m",
            return_value=[30.0] * 6,
        ):
            event = evaluate_symbol(
                bars.with_columns(pl.lit(98.0).alias("close")),
                _bars(40, end=bars["timestamp"][-1]),
                asset="BTC", symbol="BTCUSDT", cutoff=bars["timestamp"][-1],
                features5m=features,
            )
        self.assertIsNone(event)

    def test_mechanical_exits_match_rsi_and_ema26_spread(self):
        bars = _bars(close=100.6)
        with patch(
                "strategies.v2.ema99_retest_adx_v1.ema_series",
            return_value=[100.0] * 6,
        ), patch(
                "strategies.v2.ema99_retest_adx_v1.wilder_rsi",
            return_value=[72.1] * 6,
        ):
            signal = evaluate_exit(bars, side="long", cutoff=bars["timestamp"][-1])
        self.assertEqual(signal["action"], "exit")
        self.assertEqual(signal["rule_name"], "long_rsi_ema26_spread_exit")

    def test_mechanical_exit_requires_both_conditions(self):
        bars = _bars(close=100.6)
        with patch(
                "strategies.v2.ema99_retest_adx_v1.ema_series",
            return_value=[100.0] * 6,
        ), patch(
                "strategies.v2.ema99_retest_adx_v1.wilder_rsi",
            return_value=[71.9] * 6,
        ):
            signal = evaluate_exit(bars, side="long", cutoff=bars["timestamp"][-1])
        self.assertIsNone(signal)

    def test_stop_revision_uses_fixed_trigger_extreme_and_closed_bar_atr(self):
        bars = _bars(end=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc))
        with patch(
                "strategies.v2.ema99_retest_adx_v1.wilder_atr",
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
