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
    if left <= 0 or right <= 0 or bars_15m.height < left + right + 1:
        return []
    indexed = bars_15m.with_row_index("index").with_columns(
        pl.col("high").cast(pl.Float64).shift(1).rolling_max(
            left, min_samples=left,
        ).alias("left_max"),
        pl.col("high").cast(pl.Float64).shift(-right).rolling_max(
            right, min_samples=right,
        ).alias("right_max"),
    )
    return indexed.filter(
        (pl.col("high") > pl.col("left_max"))
        & (pl.col("high") > pl.col("right_max"))
    ).select(
        "index", "timestamp", pl.col("high").cast(pl.Float64).alias("price")
    ).rename({"timestamp": "ts"}).to_dicts()


def confirmed_pivot_lows(bars_15m: pl.DataFrame, left: int = 2, right: int = 2) -> list[dict[str, Any]]:
    if left <= 0 or right <= 0 or bars_15m.height < left + right + 1:
        return []
    indexed = bars_15m.with_row_index("index").with_columns(
        pl.col("low").cast(pl.Float64).shift(1).rolling_min(
            left, min_samples=left,
        ).alias("left_min"),
        pl.col("low").cast(pl.Float64).shift(-right).rolling_min(
            right, min_samples=right,
        ).alias("right_min"),
    )
    return indexed.filter(
        (pl.col("low") < pl.col("left_min"))
        & (pl.col("low") < pl.col("right_min"))
    ).select(
        "index", "timestamp", pl.col("low").cast(pl.Float64).alias("price")
    ).rename({"timestamp": "ts"}).to_dicts()


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
