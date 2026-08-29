"""liquidity_sweep.py — sweep + reclaim, BOS, impulse, invalidation for LSR.

Pure geometry per specs/strategy-liquidity-sweep-reversal-v1.md M3.
No M1/M2 imports; caller supplies pdh/pdl + structure_level.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import polars as pl


@dataclass
class SweepQual:
    depth: float
    depth_atr: float
    extreme: float  # SweepLow for long, SweepHigh for short
    close_location: float


@dataclass
class SweepState:
    direction: str  # "long" | "short"
    sweep_index: int
    structure_level: float
    sweep_extreme: float
    sweep_atr: float
    status: str  # "armed" | "cancelled_extreme" | "cancelled_expiry" | "bos_confirmed"
    bos_index: int | None = None


def _atr_at(bars: pl.DataFrame, idx: int, period: int = 14) -> float:
    if idx < 1:
        return 0.0
    # Simple ATR using last 'period' true ranges up to idx
    highs = bars["high"].to_list()
    lows = bars["low"].to_list()
    closes = bars["close"].to_list()
    trs = []
    for i in range(max(1, idx - period + 1), idx + 1):
        tr1 = highs[i] - lows[i]
        tr2 = abs(highs[i] - closes[i - 1])
        tr3 = abs(lows[i] - closes[i - 1])
        trs.append(max(tr1, tr2, tr3))
    if not trs:
        return 0.0
    return sum(trs) / len(trs)


def qualify_bullish_sweep(bar: dict, pdl: float, atr: float, min_atr: float = 0.10, max_atr: float = 1.00) -> SweepQual | None:
    """bar: dict with low, close, high, open (for location)"""
    low = float(bar["low"])
    close = float(bar["close"])
    high = float(bar.get("high", close))
    if pdl is None or atr <= 0:
        return None
    if not (low < pdl < close):
        return None
    depth = pdl - low
    depth_atr = depth / atr
    if not (min_atr <= depth_atr <= max_atr):
        return None
    cl = (close - low) / (high - low) if high > low else 0.0
    return SweepQual(depth=depth, depth_atr=depth_atr, extreme=low, close_location=cl)


def qualify_bearish_sweep(bar: dict, pdh: float, atr: float, min_atr: float = 0.10, max_atr: float = 1.00) -> SweepQual | None:
    high = float(bar["high"])
    close = float(bar["close"])
    low = float(bar.get("low", close))
    if pdh is None or atr <= 0:
        return None
    if not (high > pdh > close):
        return None
    depth = high - pdh
    depth_atr = depth / atr
    if not (min_atr <= depth_atr <= max_atr):
        return None
    cl = (close - low) / (high - low) if high > low else 0.0
    return SweepQual(depth=depth, depth_atr=depth_atr, extreme=high, close_location=cl)


def arm_long_sweep(bars: pl.DataFrame, sweep_index: int, structure_level: float, sweep_atr: float) -> SweepState:
    extreme = float(bars["low"][sweep_index])
    return SweepState(
        direction="long",
        sweep_index=sweep_index,
        structure_level=structure_level,
        sweep_extreme=extreme,
        sweep_atr=sweep_atr,
        status="armed",
    )


def arm_short_sweep(bars: pl.DataFrame, sweep_index: int, structure_level: float, sweep_atr: float) -> SweepState:
    extreme = float(bars["high"][sweep_index])
    return SweepState(
        direction="short",
        sweep_index=sweep_index,
        structure_level=structure_level,
        sweep_extreme=extreme,
        sweep_atr=sweep_atr,
        status="armed",
    )


def advance_sweep_state(state: SweepState, bars: pl.DataFrame, through_index: int, bos_window: int = 8) -> SweepState:
    if state.status != "armed":
        return state
    n = bars.height
    end = min(through_index, n - 1)
    # Check extreme break before BOS
    if state.direction == "long":
        for i in range(state.sweep_index + 1, end + 1):
            if float(bars["low"][i]) < state.sweep_extreme:
                s = SweepState(**{**state.__dict__, "status": "cancelled_extreme"})
                return s
    else:
        for i in range(state.sweep_index + 1, end + 1):
            if float(bars["high"][i]) > state.sweep_extreme:
                s = SweepState(**{**state.__dict__, "status": "cancelled_extreme"})
                return s

    # Check expiry
    if (end - state.sweep_index) > bos_window:
        s = SweepState(**{**state.__dict__, "status": "cancelled_expiry"})
        return s

    # Check BOS on any bar up to end (caller will decide emit only on last bar)
    for i in range(state.sweep_index + 1, end + 1):
        close = float(bars["close"][i])
        if state.direction == "long" and close > state.structure_level:
            s = SweepState(**{**state.__dict__, "status": "bos_confirmed", "bos_index": i})
            return s
        if state.direction == "short" and close < state.structure_level:
            s = SweepState(**{**state.__dict__, "status": "bos_confirmed", "bos_index": i})
            return s

    return state


def bos_long(close: float, structure_level: float) -> bool:
    return close > structure_level


def bos_short(close: float, structure_level: float) -> bool:
    return close < structure_level


def impulse_long(bars: pl.DataFrame, sweep_index: int, bos_index: int, sweep_low: float) -> dict[str, float]:
    rng = bars[sweep_index : bos_index + 1]
    ih = float(rng["high"].max())
    return {"impulse_low": sweep_low, "impulse_high": ih}


def impulse_short(bars: pl.DataFrame, sweep_index: int, bos_index: int, sweep_high: float) -> dict[str, float]:
    rng = bars[sweep_index : bos_index + 1]
    il = float(rng["low"].min())
    return {"impulse_low": il, "impulse_high": sweep_high}


def entry_mid(impulse_low: float, impulse_high: float, retrace_pct: float = 0.50) -> float:
    return impulse_low + retrace_pct * (impulse_high - impulse_low)


def invalidation_long(sweep_low: float, sweep_atr: float, buf: float = 0.15) -> float:
    return sweep_low - buf * sweep_atr


def invalidation_short(sweep_high: float, sweep_atr: float, buf: float = 0.15) -> float:
    return sweep_high + buf * sweep_atr


def displacement_ok(bar: dict, avg_body_20: float, mult: float = 1.50, direction: str = "long") -> bool:
    o = float(bar["open"])
    c = float(bar["close"])
    body = abs(c - o)
    if direction == "long":
        return c > o and body > mult * avg_body_20
    else:
        return c < o and body > mult * avg_body_20


def close_location(bar: dict) -> float | None:
    o = float(bar["open"])
    c = float(bar["close"])
    h = float(bar["high"])
    l = float(bar["low"])
    if h <= l:
        return None
    return (c - l) / (h - l)
