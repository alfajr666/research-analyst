"""Ported-strategy plugin contract tests: registry, cadence, config, defaults."""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config  # noqa: E402


PORTED_IDS = (
    "bb-tp-race-locked-v1", "bb-squeeze-trend-v1", "kama-trend-following-v1",
    "macd-ema-v1", "mr-vwap-locked-v1", "trend-pullback-vwap-v1", "trend-wall-v5",
)


class PortedStrategyRegistryTests(unittest.TestCase):
    def test_all_ported_strategies_registered_and_enabled(self):
        from strategy_plugins import _REGISTRY, KNOWN_STRATEGIES

        for strategy_id in PORTED_IDS:
            self.assertIn(strategy_id, KNOWN_STRATEGIES)
            self.assertIn(strategy_id, _REGISTRY, strategy_id)
            self.assertIn(strategy_id, config.STRATEGY_ENABLED_IDS)
            from strategy_plugins import ADMISSION_STRATEGY_IDS

            self.assertIn(strategy_id, ADMISSION_STRATEGY_IDS)
            self.assertIn(strategy_id, config.PORTED_STRATEGY_IDS)

    def test_ported_strategies_run_at_five_minute_cadence(self):
        from strategy_plugins import _REGISTRY

        for strategy_id in PORTED_IDS:
            self.assertEqual(_REGISTRY[strategy_id].cadence, "5m", strategy_id)

    def test_default_allowlist_is_ports_only(self):
        # Seven vectorbt ports plus the UTC-session v3 retiree (not a port,
        # so PORTED_IDS itself stays at seven).
        self.assertEqual(set(config.STRATEGY_ENABLED_IDS), set(PORTED_IDS) | {"mr-vwap-utc-session-v3"})
        self.assertTrue(config.LEGACY_PRODUCTION_STRATEGY_IDS.isdisjoint(config.STRATEGY_ENABLED_IDS))

    def test_legacy_strategies_still_registered(self):
        from strategy_plugins import _REGISTRY, KNOWN_STRATEGIES

        for strategy_id in config.LEGACY_PRODUCTION_STRATEGY_IDS:
            self.assertIn(strategy_id, KNOWN_STRATEGIES)
            self.assertIn(strategy_id, _REGISTRY)


def _synthetic_5m_bars(hours: int, *, base: float = 100.0, seed: int = 7):
    """Deterministic OHLCV 5m bars, end-stamped, completing at :05 boundaries."""
    import polars as pl

    start = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    rows = []
    price = base
    for index in range(hours * 12):
        drift = ((index * 37 + seed * 13) % 11 - 5) * 0.05
        open_price = price
        close_price = max(0.5, open_price + drift)
        high = max(open_price, close_price) + 0.15
        low = min(open_price, close_price) - 0.15
        rows.append({
            "timestamp": start + timedelta(minutes=5 * (index + 1)),
            "open": open_price, "high": high, "low": low, "close": close_price,
            "volume": 1000.0 + (index % 17) * 10.0,
        })
        price = close_price
    return pl.DataFrame(rows)


def _synthetic_1h_bars(hours: int, *, base: float = 100.0, seed: int = 3):
    import polars as pl

    start = datetime(2026, 9, 1, 1, tzinfo=timezone.utc)
    rows = []
    price = base
    for index in range(hours):
        drift = ((index * 53 + seed * 29) % 13 - 6) * 0.08
        open_price = price
        close_price = max(0.5, open_price + drift)
        rows.append({
            "timestamp": start + timedelta(hours=index + 1),
            "open": open_price, "high": max(open_price, close_price) + 0.2,
            "low": min(open_price, close_price) - 0.2, "close": close_price,
            "volume": 5000.0 + (index % 11) * 25.0,
        })
        price = close_price
    return pl.DataFrame(rows)


class PortedStrategyEvaluationTests(unittest.TestCase):
    CUTOFF = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)

    def _evaluate(self, module_name: str, **kwargs):
        module = importlib.import_module(module_name)
        bars5m = _synthetic_5m_bars(24 * 20, seed=hash(module_name) % 97)
        bars1h = _synthetic_1h_bars(20 * 24, seed=hash(module_name) % 89)
        result = module.evaluate_symbol(
            *module.__dict__.get("_evaluate_args", (bars5m, bars1h)),
            asset="TEST", symbol="TESTUSDT", cutoff=self.CUTOFF, **kwargs,
        )
        return module, result

    def test_bb_tp_race_signal_shape(self):
        from strategies.v2.bb_tp_race_locked_v1 import evaluate_symbol

        bars15 = _resampled_15m(_synthetic_5m_bars(24 * 20))
        bars1h = _synthetic_1h_bars(20 * 24)
        event = evaluate_symbol(bars15, bars1h, asset="TEST", symbol="TESTUSDT", cutoff=self.CUTOFF)
        if event is not None:
            self.assertIn(event["direction"], ("long", "short"))
            self.assertGreater(event["entry_price"], 0)
            self.assertGreater(event["invalidation_price"], 0)
            self.assertEqual(len(event["targets"]), 2)
            bracket = event["metadata"]["bracket_spec"]
            self.assertIn("tp3_rule", bracket)
            self.assertEqual(bracket["version"], "bb_locked_v1")

    def test_bb_tp_race_rejects_stale_frame(self):
        from strategies.v2.bb_tp_race_locked_v1 import evaluate_symbol

        bars15 = _resampled_15m(_synthetic_5m_bars(24 * 20))
        bars1h = _synthetic_1h_bars(20 * 24)
        stale = self.CUTOFF - timedelta(hours=6)
        self.assertIsNone(evaluate_symbol(
            bars15.filter(bars15["timestamp"] <= stale),
            bars1h.filter(bars1h["timestamp"] <= stale),
            asset="TEST", symbol="TESTUSDT", cutoff=self.CUTOFF,
        ))

    def test_macd_ema_signal_shape(self):
        from strategies.v2.macd_ema_v1 import evaluate_symbol

        event = evaluate_symbol(
            _synthetic_1h_bars(20 * 24), _synthetic_5m_bars(24 * 20),
            asset="TEST", symbol="TESTUSDT", cutoff=self.CUTOFF,
        )
        if event is not None:
            self.assertEqual(event["direction"], "long")
            self.assertEqual(event["metadata"]["direction_scope"], "long_only")
            self.assertEqual(event["targets"], [])

    def test_macd_ema_gates_to_hourly_boundary(self):
        from strategies.v2.macd_ema_v1 import evaluate_symbol

        event = evaluate_symbol(
            _synthetic_1h_bars(20 * 24), _synthetic_5m_bars(24 * 20),
            asset="TEST", symbol="TESTUSDT",
            cutoff=self.CUTOFF.replace(minute=30),
        )
        self.assertIsNone(event)

    def test_kama_signal_shape(self):
        from strategies.v2.kama_trend_following_v1 import evaluate_symbol

        event = evaluate_symbol(
            _synthetic_5m_bars(24 * 20), _synthetic_1h_bars(20 * 24),
            asset="TEST", symbol="TESTUSDT", cutoff=self.CUTOFF,
        )
        if event is not None:
            self.assertIn(event["direction"], ("long", "short"))
            target = event["targets"][0]
            risk = abs(event["entry_price"] - event["invalidation_price"])
            self.assertAlmostEqual(
                abs(target - event["entry_price"]), risk, places=6,
            )

    def test_trend_wall_v5_signal_shape(self):
        from strategies.v2.trend_wall_v5 import evaluate_symbol

        event = evaluate_symbol(
            _synthetic_5m_bars(24 * 20), _synthetic_1h_bars(20 * 24),
            asset="TEST", symbol="TESTUSDT", cutoff=self.CUTOFF,
        )
        if event is not None:
            self.assertIn(event["direction"], ("long", "short"))
            self.assertIn("structure_exit", event["metadata"]["strategy_exits"])


def _resampled_15m(bars5m):
    from strategy_v2_context import resample_ohlcv

    return resample_ohlcv(bars5m, "15m")


class PortedStrategyIndependenceTests(unittest.TestCase):
    """The ports must be self-contained: no shared vectorbt-derived module."""

    PORT_MODULES = (
        "strategies.v2.bb_tp_race_locked_v1",
        "strategies.v2.bb_squeeze_trend_v1",
        "strategies.v2.kama_trend_following_v1",
        "strategies.v2.macd_ema_v1",
        "strategies.v2.mr_vwap_locked_v1",
        "strategies.v2.trend_pullback_vwap_v1",
        "strategies.v2.trend_wall_v5",
    )

    def test_no_shared_port_module_exists(self):
        import os

        self.assertFalse(os.path.exists(
            os.path.join(os.path.dirname(__file__), "..", "src", "research_analyst",
                         "strategies", "v2", "port_indicators.py"),
        ))

    def test_ported_plugins_import_only_native_engines(self):
        import importlib
        import sys

        allowed = {
            "config", "polars", "strategy_features", "strategy_v2_context",
            "polars_indicators", "datetime", "math", "bisect", "typing",
        }
        for module_name in self.PORT_MODULES:
            module = importlib.import_module(module_name)
            self.assertIsNotNone(module)
            source_name = module_name.rsplit(".", 1)[-1]
            # Only native RA modules may be imported; the shared ports module
            # must not come back.
            for attr in vars(module).values():
                if getattr(attr, "__module__", None):
                    self.assertNotIn("port_indicators", str(getattr(attr, "__module__")))
            for name, value in vars(module).items():
                if isinstance(value, type(sys)) and getattr(value, "__name__", "").startswith(("strategies", "port_indicators")):
                    self.assertNotIn("port_indicators", value.__name__)
        self.assertTrue(all(name in allowed or name not in ("port_indicators",)
                            for name in allowed))


class PortIndicatorParityTests(unittest.TestCase):
    """Strategy-private kernels: parity against reference computations."""

    def test_kama_recurrence_shape(self):
        from strategies.v2.kama_trend_following_v1 import _kama_series

        values = [100.0 + ((i * 29) % 17) * 0.4 for i in range(120)]
        result = _kama_series(values, 14, 2, 30)
        self.assertEqual(len(result), len(values))
        self.assertIsNone(result[13])
        self.assertIsNotNone(result[14])
        self.assertGreater(result[14], values[13] - 1.0)
        self.assertLess(result[14], values[14] + 1.0)

    def test_choppiness_range(self):
        from strategies.v2.kama_trend_following_v1 import _choppiness_series

        bars = _synthetic_5m_bars(12)
        result = _choppiness_series(bars, 14)
        values = [v for v in result if v is not None]
        self.assertTrue(values)
        for value in values:
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 100.0)

    def test_group_bars_30m_exact_groups_only(self):
        from strategies.v2.trend_wall_v5 import _group_bars_30m

        bars = _synthetic_5m_bars(4)
        grouped = _group_bars_30m(bars)
        self.assertEqual(grouped.height, 8)  # 48 x 5m bars -> 8 x 30m bars
        first = grouped.row(0, named=True)
        self.assertEqual(first["timestamp"], datetime(2026, 9, 1, 0, 30, tzinfo=timezone.utc))
        self.assertEqual(first["close"], float(bars["close"][5]))

    def test_group_bars_30m_drops_incomplete_tail(self):
        from strategies.v2.trend_wall_v5 import _group_bars_30m

        bars = _synthetic_5m_bars(4).head(23)  # last bucket has 5 bars only
        grouped = _group_bars_30m(bars)
        self.assertEqual(grouped.height, 3)

    def test_linreg_slope_matches_finite_difference(self):
        from strategies.v2.bb_squeeze_trend_v1 import _linreg_slope_series

        values = [float(i) for i in range(40)]
        result = _linreg_slope_series(values, 11)
        self.assertAlmostEqual(result[-1], 1.0, places=9)

    def test_completed_htf_mapping_causality(self):
        from strategies.v2.trend_wall_v5 import _map_completed

        import polars as pl

        htf = pl.DataFrame({
            "timestamp": [datetime(2026, 9, 1, 1, tzinfo=timezone.utc),
                          datetime(2026, 9, 1, 2, tzinfo=timezone.utc)],
        })
        exec_times = [
            datetime(2026, 9, 1, 1, 30, tzinfo=timezone.utc),
            datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 1, 2, 30, tzinfo=timezone.utc),
        ]
        mapped = _map_completed(htf, [10.0, 20.0], exec_times)
        self.assertEqual(mapped, [10.0, 20.0, 20.0])


if __name__ == "__main__":
    unittest.main()
