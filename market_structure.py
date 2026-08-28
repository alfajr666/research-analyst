"""market_structure.py — confirmed 2-left/2-right pivots for BOS (no lookahead).

Matches specs/strategy-liquidity-sweep-reversal-v1.md M2 contract.
"""

from __future__ import annotations

from typing import Any

import polars as pl


def _strict_pivot_high(highs: list[float], i: int, left: int = 2, right: int = 2) -> bool:
    """Return True if index i is a confirmed pivot high at the moment when right bars exist."""
    n = len(highs)
    if i < left or i + right >= n:
        return False
    h = highs[i]
    # left side (strict >)
    for k in range(1, left + 1):
        if highs[i - k] >= h:
            return False
    # right side
    for k in range(1, right + 1):
        if highs[i + k] >= h:
            return False
    return True


def _strict_pivot_low(lows: list[float], i: int, left: int = 2, right: int = 2) -> bool:
    n = len(lows)
    if i < left or i + right >= n:
        return False
    l = lows[i]
    for k in range(1, left + 1):
        if lows[i - k] <= l:
            return False
    for k in range(1, right + 1):
        if lows[i + k] <= l:
            return False
    return True


def confirmed_pivot_highs(bars_15m: pl.DataFrame, left: int = 2, right: int = 2) -> list[dict[str, Any]]:
    """Return list of confirmed pivot highs. Confirmation requires right bars to exist in the frame."""
    if bars_15m.height < left + right + 1:
        return []
    highs = bars_15m["high"].to_list()
    ts = bars_15m["timestamp"].to_list()
    pivots = []
    for i in range(len(highs)):
        if _strict_pivot_high(highs, i, left, right):
            pivots.append({
                "index": i,
                "ts": ts[i],
                "price": float(highs[i]),
            })
    return pivots


def confirmed_pivot_lows(bars_15m: pl.DataFrame, left: int = 2, right: int = 2) -> list[dict[str, Any]]:
    if bars_15m.height < left + right + 1:
        return []
    lows = bars_15m["low"].to_list()
    ts = bars_15m["timestamp"].to_list()
    pivots = []
    for i in range(len(lows)):
        if _strict_pivot_low(lows, i, left, right):
            pivots.append({
                "index": i,
                "ts": ts[i],
                "price": float(lows[i]),
            })
    return pivots


def latest_confirmed_pivot_high(bars_15m: pl.DataFrame, asof_index: int, left: int = 2, right: int = 2) -> dict[str, Any] | None:
    """Most recent confirmed pivot high whose confirmation bar <= asof_index."""
    pivots = confirmed_pivot_highs(bars_15m, left, right)
    valid = [p for p in pivots if p["index"] + right <= asof_index]
    if not valid:
        return None
    return max(valid, key=lambda p: p["index"])


def latest_confirmed_pivot_low(bars_15m: pl.DataFrame, asof_index: int, left: int = 2, right: int = 2) -> dict[str, Any] | None:
    pivots = confirmed_pivot_lows(bars_15m, left, right)
    valid = [p for p in pivots if p["index"] + right <= asof_index]
    if not valid:
        return None
    return max(valid, key=lambda p: p["index"])
