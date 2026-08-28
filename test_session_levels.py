"""test_session_levels.py — unit tests for M1 PDH/PDL per LSR spec."""

import unittest
from datetime import datetime, timedelta, timezone

import polars as pl

from session_levels import pdh_pdl, pdh_pdl_series


def _make_bars_with_days() -> pl.DataFrame:
    """Two full UTC days of 15m bars. Day1: highs to 101, lows to 99. Day2: higher."""
    start = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)  # day D-1
    rows = []
    # Day D-1 (prior)
    for i in range(96):  # 24h * 4
        ts = start + timedelta(minutes=15 * i)
        base = 100.0
        rows.append({
            "timestamp": ts,
            "open": base,
            "high": base + 1.0 + (i % 4) * 0.1,
            "low": base - 1.0 - (i % 4) * 0.1,
            "close": base,
            "volume": 1000.0,
        })
    # Day D
    start_d = datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc)
    for i in range(20):  # partial current day
        ts = start_d + timedelta(minutes=15 * i)
        base = 102.0
        rows.append({
            "timestamp": ts,
            "open": base,
            "high": base + 0.5,
            "low": base - 0.5,
            "close": base,
            "volume": 1000.0,
        })
    return pl.DataFrame(rows, strict=False)


class TestSessionLevels(unittest.TestCase):
    def test_pdh_pdl_basic(self):
        bars = _make_bars_with_days()
        asof = datetime(2026, 8, 18, 5, 0, tzinfo=timezone.utc)  # during day D
        res = pdh_pdl(bars, asof)
        self.assertIsNotNone(res)
        self.assertAlmostEqual(res["pdh"], 101.3, places=1)  # max of day D-1 highs
        self.assertAlmostEqual(res["pdl"], 98.7, places=1)  # min of day D-1 lows
        self.assertEqual(res["prior_utc_day"], "2026-08-17")
        self.assertGreater(res["bar_count"], 0)

    def test_pdh_pdl_prior_day_only(self):
        bars = _make_bars_with_days()
        # asof on first bar of new day still uses prior
        asof = datetime(2026, 8, 18, 0, 15, tzinfo=timezone.utc)
        res = pdh_pdl(bars, asof)
        self.assertIsNotNone(res)
        self.assertEqual(res["prior_utc_day"], "2026-08-17")

    def test_pdh_pdl_no_prior_day(self):
        bars = _make_bars_with_days()
        asof = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)  # still on first day
        res = pdh_pdl(bars, asof)
        self.assertIsNone(res)

    def test_pdh_pdl_incomplete_prior(self):
        # Only current day bars
        start = datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc)
        bars = pl.DataFrame([
            {"timestamp": start + timedelta(minutes=15*i), "high": 105.0, "low": 99.0}
            for i in range(10)
        ], strict=False)
        res = pdh_pdl(bars, start)
        self.assertIsNone(res)

    def test_pdh_pdl_series(self):
        bars = _make_bars_with_days()
        ser = pdh_pdl_series(bars)
        self.assertGreater(ser.height, 0)
        # For a bar on day D, prior should be set
        last = ser.filter(pl.col("timestamp") > datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc))
        if last.height > 0:
            self.assertTrue(last["prior_utc_day"][0] is not None)

    def test_no_lookahead(self):
        bars = _make_bars_with_days()
        asof = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
        full = pdh_pdl(bars, asof)
        # Drop last few bars of current day; should not affect PDH of prior day
        trimmed = bars.filter(pl.col("timestamp") < datetime(2026, 8, 18, 6, 0, tzinfo=timezone.utc))
        trim_res = pdh_pdl(trimmed, asof)
        self.assertEqual(full, trim_res)


if __name__ == "__main__":
    unittest.main()
