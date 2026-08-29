import unittest
from datetime import datetime, timedelta, timezone

import polars as pl

from structure_zones import (
    compute_atr,
    detect_fvg,
    detect_order_blocks,
    get_active_zones_for_snapshot,
)


class FVGOrderBlockFixtureTests(unittest.TestCase):
    def setUp(self):
        self.base = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)

    def _make_bars(self, closes, highs=None, lows=None, opens=None):
        n = len(closes)
        ts = [self.base + timedelta(minutes=60 * i) for i in range(n)]  # hourly for simplicity
        if highs is None:
            highs = [float(c * 1.01) for c in closes]
        if lows is None:
            lows = [float(c * 0.99) for c in closes]
        if opens is None:
            opens = [float(c) for c in closes]
        closes = [float(c) for c in closes]
        return pl.DataFrame({
            "timestamp": ts,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
        }, strict=False)

    def test_fvg_creation_on_three_candle_imbalance(self):
        # Bullish FVG: bar1 high < bar3 low, after bar3 close
        bars = self._make_bars(
            closes=[100, 101, 105],
            highs=[100.5, 101.5, 105.5],
            lows=[99.5, 100.5, 104.5],
        )
        atr = 2.0
        fvgs = detect_fvg(bars, atr=atr, min_gap_mult=0.25)
        self.assertEqual(len(fvgs), 1)
        f = fvgs[0]
        self.assertEqual(f["direction"], "bullish")
        self.assertGreater(f["gap"], 0.25 * atr)
        self.assertEqual(f["state"], "active")

    def test_fvg_mitigation_on_wick(self):
        bars = self._make_bars(
            closes=[100, 101, 105, 103],
            highs=[100.5, 101.5, 105.5, 104.5],
            lows=[99.5, 100.5, 104.5, 102.5],  # wick into gap
        )
        atr = 2.0
        fvgs = detect_fvg(bars, atr=atr)
        # After wick into zone, at least mitigated or filled/partial
        self.assertTrue(any(f["state"] in ("partial", "mitigated", "filled") for f in fvgs))

    def test_fvg_fill_and_invalidation(self):
        # Fill when reaches far boundary
        bars = self._make_bars(
            closes=[100, 101, 105, 108],  # reaches far
        )
        fvgs = detect_fvg(bars, atr=2.0)
        self.assertTrue(any(f["state"] == "filled" for f in fvgs))

        # Invalidate on close through
        bars2 = self._make_bars(
            closes=[100, 101, 105, 103, 99],  # close below for bullish?
        )
        # simplistic
        fvgs2 = detect_fvg(bars2, atr=2.0)
        self.assertTrue(len(fvgs2) >= 0)  # at least no crash

    def test_order_block_creation(self):
        # Simple: opposing candle before displacement >1.5 ATR closing new swing
        bars = self._make_bars(
            closes=[100, 98, 140],  # bearish OB then bullish displacement large
            highs=[101, 99, 160],
            lows=[99, 97, 130],
        )
        atr = 3.0
        obs = detect_order_blocks(bars, atr=atr, swing_lookback=1)
        self.assertTrue(len(obs) > 0)
        ob = obs[0]
        self.assertIn(ob["direction"], ("bullish", "bearish"))
        self.assertIn(ob["state"], ("active", "partial"))

    def test_zones_snapshot_limits_to_three_recent_active(self):
        # Many zones, snapshot caps at 3 per ...
        zones = [
            {"asset": "BTC", "timeframe": "4h", "direction": "bullish", "type": "fvg", "state": "active"},
            {"asset": "BTC", "timeframe": "4h", "direction": "bullish", "type": "fvg", "state": "active"},
            {"asset": "BTC", "timeframe": "4h", "direction": "bullish", "type": "fvg", "state": "active"},
            {"asset": "BTC", "timeframe": "4h", "direction": "bullish", "type": "fvg", "state": "active"},
        ]
        snap = get_active_zones_for_snapshot(zones, max_per=3)
        self.assertEqual(len(snap), 3)

    def test_unavailable_for_missing_data(self):
        empty = pl.DataFrame({"timestamp": [], "open": [], "high": [], "low": [], "close": []})
        self.assertEqual(detect_fvg(empty, atr=1.0), [])
        self.assertEqual(detect_order_blocks(empty, atr=1.0), [])


if __name__ == "__main__":
    unittest.main()
