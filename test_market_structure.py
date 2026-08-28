"""test_market_structure.py — tests for 2/2 pivots (M2) per LSR spec. No lookahead."""

import unittest
from datetime import datetime, timedelta, timezone

import polars as pl

from market_structure import (
    confirmed_pivot_highs,
    confirmed_pivot_lows,
    latest_confirmed_pivot_high,
    latest_confirmed_pivot_low,
)


def _pivot_fixture() -> pl.DataFrame:
    """Synthetic series with clear pivot high at index 5 (0-based), low at 10.
    Confirmation at index 5+2 = 7 for the high.
    """
    start = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    prices_high = [100, 101, 100, 102, 101, 105, 104, 103, 102, 101, 100, 99, 98]
    # Make a clean pivot high at i=5 (105), then down
    # For low later
    lows = [99, 98, 99, 97, 98, 96, 97, 98, 99, 100, 95, 96, 97]  # pivot low at i=10 =95
    rows = []
    for i in range(len(prices_high)):
        ts = start + timedelta(minutes=15 * i)
        rows.append({
            "timestamp": ts,
            "open": prices_high[i] - 0.5,
            "high": prices_high[i],
            "low": lows[i],
            "close": prices_high[i] - 0.2,
        })
    return pl.DataFrame(rows, strict=False)


class TestMarketStructure(unittest.TestCase):
    def test_pivot_high_confirms_only_after_right_bars(self):
        bars = _pivot_fixture()
        highs = confirmed_pivot_highs(bars, left=2, right=2)
        self.assertTrue(any(p["price"] == 105.0 for p in highs))
        # The pivot at index 5 should be present (right bars exist in frame)
        self.assertGreaterEqual(len(highs), 1)

    def test_pivot_not_confirmed_without_right(self):
        bars = _pivot_fixture()
        # Trim so that right of pivot 5 is missing (only up to index 6)
        short = bars.slice(0, 7)
        highs = confirmed_pivot_highs(short, left=2, right=2)
        self.assertEqual(len(highs), 0)  # not yet confirmed

    def test_latest_confirmed_respects_asof(self):
        bars = _pivot_fixture()
        # At asof_index = 6 (before full confirm), should have no latest yet for the main pivot
        p = latest_confirmed_pivot_high(bars, asof_index=6, left=2, right=2)
        self.assertIsNone(p)  # confirm at 5+2=7
        p = latest_confirmed_pivot_high(bars, asof_index=7, left=2, right=2)
        self.assertIsNotNone(p)
        self.assertEqual(p["price"], 105.0)

    def test_pivot_low(self):
        bars = _pivot_fixture()
        lows = confirmed_pivot_lows(bars, left=2, right=2)
        self.assertTrue(any(p["price"] == 95.0 for p in lows))

    def test_no_lookahead_on_asof(self):
        bars = _pivot_fixture()
        full_latest = latest_confirmed_pivot_high(bars, asof_index=10, left=2, right=2)
        # Drop future bars after asof
        trimmed = bars.slice(0, 11)
        trim_latest = latest_confirmed_pivot_high(trimmed, asof_index=10, left=2, right=2)
        self.assertEqual(full_latest, trim_latest)


if __name__ == "__main__":
    unittest.main()
