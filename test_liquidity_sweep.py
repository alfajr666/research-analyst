"""test_liquidity_sweep.py — M3 geometry tests per LSR spec."""

import unittest
from datetime import datetime, timedelta, timezone

import polars as pl

from liquidity_sweep import (
    qualify_bullish_sweep,
    qualify_bearish_sweep,
    arm_long_sweep,
    advance_sweep_state,
    bos_long,
    entry_mid,
    invalidation_long,
    impulse_long,
)


def _sweep_bars_long() -> pl.DataFrame:
    """Simple long sweep setup: sweep below PDL then reclaim, then BOS."""
    start = datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc)
    # prices designed for ATR ~1.0 around 100
    rows = []
    base = 100.0
    for i in range(20):
        ts = start + timedelta(minutes=15 * i)
        if i == 2:  # sweep bar
            o, h, l, c = base - 0.05, base + 0.1, base - 0.6, base + 0.2  # depth 0.6, reclaim
        elif i == 7:  # BOS bar
            o, h, l, c = base + 0.1, base + 1.7, base + 0.05, base + 1.6  # close > structure
        else:
            o, h, l, c = base, base + 0.2, base - 0.2, base + 0.05
        rows.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": c})
    return pl.DataFrame(rows, strict=False)


class TestLiquiditySweep(unittest.TestCase):
    def test_qualify_bullish(self):
        bar = {"low": 99.4, "close": 100.2, "high": 100.3, "open": 100.0}
        q = qualify_bullish_sweep(bar, pdl=100.0, atr=1.0)
        self.assertIsNotNone(q)
        self.assertAlmostEqual(q.depth, 0.6)
        self.assertAlmostEqual(q.depth_atr, 0.6)

    def test_qualify_reject_depth(self):
        bar = {"low": 99.95, "close": 100.05, "high": 100.1, "open": 100.0}
        q = qualify_bullish_sweep(bar, pdl=100.0, atr=1.0)
        self.assertIsNone(q)  # too small

    def test_arm_and_bos(self):
        bars = _sweep_bars_long()
        state = arm_long_sweep(bars, sweep_index=2, structure_level=101.5, sweep_atr=1.0)
        self.assertEqual(state.status, "armed")
        # advance to BOS
        state2 = advance_sweep_state(state, bars, through_index=8, bos_window=8)
        self.assertEqual(state2.status, "bos_confirmed")
        self.assertTrue(bos_long(101.7, 101.5))

    def test_cancel_extreme(self):
        bars = _sweep_bars_long()
        state = arm_long_sweep(bars, sweep_index=2, structure_level=101.5, sweep_atr=1.0)
        # Force a lower low after sweep
        rows = bars.to_dicts()
        rows[4]["low"] = 99.0
        bad = pl.DataFrame(rows, strict=False)
        s = advance_sweep_state(state, bad, through_index=5, bos_window=8)
        self.assertEqual(s.status, "cancelled_extreme")

    def test_impulse_and_entry(self):
        bars = _sweep_bars_long()
        imp = impulse_long(bars, 2, 7, 99.4)
        mid = entry_mid(imp["impulse_low"], imp["impulse_high"], 0.5)
        self.assertGreater(mid, 99.4)
        inv = invalidation_long(99.4, 1.0, 0.15)
        self.assertAlmostEqual(inv, 99.25)

    def test_replay_matches(self):
        bars = _sweep_bars_long()
        state = arm_long_sweep(bars, 2, 101.5, 1.0)
        s1 = advance_sweep_state(state, bars, 8)
        # Rebuild step by step
        s2 = state
        for ii in range(3, 9):
            s2 = advance_sweep_state(s2, bars, ii)
        self.assertEqual(s1.status, s2.status)


if __name__ == "__main__":
    unittest.main()
