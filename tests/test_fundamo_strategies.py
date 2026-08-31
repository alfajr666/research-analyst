import unittest
from datetime import datetime, timezone

import polars as pl
import config
from strategies.v2.dual_zone_follower_v2 import evaluate_symbol
from strategies.v2.ema20_pullback_h4_trend_v1 import evaluate_symbol as evaluate_ema20
from strategy_plugins import _REGISTRY
from intent_outbox import build_executor_intent


def bars(closes, *, opens=None, highs=None, lows=None, start="2026-08-31T00:00:00+00:00", minutes=5):
    opens = opens or closes
    highs = highs or [max(o, c) * 1.001 for o, c in zip(opens, closes)]
    lows = lows or [min(o, c) * .999 for o, c in zip(opens, closes)]
    ts = [datetime.fromisoformat(start) + __import__("datetime").timedelta(minutes=minutes*i)
          for i in range(len(closes))]
    return pl.DataFrame({"timestamp": ts, "open": opens, "high": highs, "low": lows, "close": closes,
                         "volume": [1.0] * len(closes)})


class FundamoStrategyTests(unittest.TestCase):
    def test_registry_has_new_strategies_and_cadences(self):
        self.assertEqual(_REGISTRY["dual-zone-follower-v2"].cadence, "5m")
        self.assertEqual(_REGISTRY["dual-zone-short-follower-v2"].cadence, "5m")
        self.assertEqual(_REGISTRY["ema20-pullback-h4-trend-v1"].cadence, "5m")
        self.assertEqual(_REGISTRY["ema-stack-15m-adx-stochrsi-5m-v1"].cadence, "5m")
        self.assertNotIn("dual-zone-follower-v1", _REGISTRY)

    def test_dual_zone_v2_emits_channel_a_with_locked_geometry(self):
        close = [100 + i * .08 for i in range(100)]
        event = evaluate_symbol(bars(close), asset="BTC", symbol="BTCUSDT",
                                cutoff=None, direction="long")
        self.assertIsNotNone(event)
        self.assertEqual(event["strategy_id"], "dual-zone-follower-v2")
        self.assertEqual(event["phase"], "channel_a")
        self.assertLess(event["invalidation_price"], event["entry_price"])

    def test_ema20_pullback_requires_engulfing_and_builds_two_r_target(self):
        local = bars([100 + i for i in range(30)] + [124, 130],
                     opens=[100 + i for i in range(30)] + [128, 124],
                     highs=[101 + i for i in range(30)] + [129, 131],
                     lows=[99 + i for i in range(30)] + [125, 120])
        h4 = bars([100 + i for i in range(210)], minutes=240)
        event = evaluate_ema20(local, h4, asset="BTC", symbol="BTCUSDT",
                               cutoff=None, direction="long")
        self.assertIsNotNone(event)
        risk = event["entry_price"] - event["invalidation_price"]
        self.assertAlmostEqual(event["targets"][0], event["entry_price"] + 2 * risk)

    def test_fundamo_routes_cannot_be_overridden(self):
        intent = build_executor_intent({"strategy_id": "ema20-pullback-h4-trend-v1",
                                        "asset": "BTC", "direction": "long",
                                        "observed_at": "2026-08-31T00:00:00+00:00",
                                        "entry_price": 100, "invalidation_price": 95,
                                        "targets": [110]}, account_id="hyro")
        self.assertEqual((intent["exchange_id"], intent["account_id"]), ("bybit", "fundamo"))


if __name__ == "__main__":
    unittest.main()
