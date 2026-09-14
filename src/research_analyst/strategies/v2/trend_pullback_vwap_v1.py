"""Trend pullback VWAP v1 port: sequential 15m state machine, fixed-2R target.

Authority: repo-final/scripts/trend_pullback_v1.py + the universe runner
scripts/backtest_trend_pullback_universe.py
(see engine_handoff/all_families_engine_specs.md section 6 — REJECTED,
median PF 0.871; ported per operator decision so behavior stays reproducible).

Self-contained plugin: RSI from the native ``wilder_rsi`` engine, ATR from
``wilder_atr_series``, 15m frame from the shared ``resample_ohlcv``; the
rolling-24h VWAP, hourly regime layer, and the sequential state machine are
private to this strategy.

Frame: 5m execution bars; all signal indicators on completed 15m groups (exact
15/15 sub-bars). VWAP mode rolling_24h: 96 completed 15m HLC3 observations,
volume-weighted mean and population sigma.

Sequential per-side state machine (authority trend_pullback_v1.signals):
1. Regime: hourly VWAP slope > +0.5 (long) / < -0.5 (short) ATR-normalized AND
   price on the trend side of 2 hourly closes.
2. Impulse: within the prior 8 bars a close beyond the +1σ (long) / -1σ band.
3. Setup: pullback touch — long: low <= +1σ AND high >= VWAP AND RSI14 in
   [40, 50] (short: [50, 60]) AND price still valid-side of VWAP.
4. Pending lives <= 4 bars; cancel on regime loss, VWAP cross, or expiry;
   low/high extremes accumulate while pending.
5. Confirmation: RSI crosses back through 50 and stays recovered, then a close
   beyond the previous 15m candle's high (long) / low (short). One
   confirmation consumes the pending state.
6. Order: enter next 15m bar inside the 2σ band; stop = accumulated pullback
   extreme ∓ 0.5*ATR14(15m); simulated target = fixed 2R; planned R:R >= 1.2.
   Intrabar 5m TP/SL fills are executor-bracket territory.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Any

import polars as pl

import config
from strategy_v2_context import (
    cutoff_from_id,
    evaluation_symbols,
    has_active_event,
    load_bars_for_interval,
    resample_ohlcv,
    strategy_market_connection,
    wilder_rsi,
)

STRATEGY_ID = config.TREND_PULLBACK_STRATEGY_ID
PLUGIN_VERSION = "v1"


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _rolling_vwap(d: pl.DataFrame) -> tuple[list[float | None], list[float | None]]:
    """rolling_24h VWAP: 96 completed 15m HLC3 obs, volume-weighted, with sigma."""
    rows = d.to_dicts()
    p = [(float(r["high"]) + float(r["low"]) + float(r["close"])) / 3.0 for r in rows]
    v = [float(r.get("volume") or 0.0) for r in rows]
    frame = pl.DataFrame({
        "v": pl.Series(v, dtype=pl.Float64),
        "pv": pl.Series([p[i] * v[i] for i in range(len(p))], dtype=pl.Float64),
        "p2v": pl.Series([p[i] ** 2 * v[i] for i in range(len(p))], dtype=pl.Float64),
    })
    sums = frame.select([
        pl.col("v").rolling_sum(96, min_samples=96).alias("vs"),
        pl.col("pv").rolling_sum(96, min_samples=96).alias("pvs"),
        pl.col("p2v").rolling_sum(96, min_samples=96).alias("p2vs"),
    ])
    vwap: list[float | None] = []
    sigma: list[float | None] = []
    for vs, pvs, p2vs in zip(sums["vs"].to_list(), sums["pvs"].to_list(), sums["p2vs"].to_list()):
        if not vs or vs <= 0:
            vwap.append(None)
            sigma.append(None)
            continue
        mean = pvs / vs
        variance = p2vs / vs - mean ** 2
        vwap.append(mean)
        sigma.append(math.sqrt(variance) if variance > 0 else 0.0)
    return vwap, sigma


def evaluate_symbol(bars5m, bars1h, *, asset: str, symbol: str, cutoff: datetime) -> dict | None:
    cutoff = _utc(cutoff)
    if bars5m.is_empty() or bars1h.is_empty():
        return None
    bars5m = bars5m.filter(bars5m["timestamp"] <= cutoff).sort("timestamp")
    if not _fresh(bars5m, cutoff, 5 * 60 + config.DATA_FRESHNESS_MAX_SECONDS):
        return None
    bars15 = resample_ohlcv(bars5m, "15m")
    if bars15.is_empty() or bars15.height < 96 + 30:
        return None
    bars1h = bars1h.filter(bars1h["timestamp"] <= cutoff).sort("timestamp")

    from polars_indicators import wilder_atr_series

    timestamps = [_utc(v) for v in bars15["timestamp"].to_list()]
    highs = [float(v) for v in bars15["high"].to_list()]
    lows = [float(v) for v in bars15["low"].to_list()]
    closes = [float(v) for v in bars15["close"].to_list()]

    vwap, sigma = _rolling_vwap(bars15)
    upper1 = [None if (v is None or s is None) else v + s for v, s in zip(vwap, sigma)]
    lower1 = [None if (v is None or s is None) else v - s for v, s in zip(vwap, sigma)]
    upper2 = [None if (v is None or s is None) else v + 2 * s for v, s in zip(vwap, sigma)]
    lower2 = [None if (v is None or s is None) else v - 2 * s for v, s in zip(vwap, sigma)]

    rsi = wilder_rsi(closes, 14)
    atr15 = wilder_atr_series(bars15, 14).to_list()
    atr1h = wilder_atr_series(bars1h, 14).to_list()

    # Hourly layer sampled at 15m completion (engine: h[["slope","above","below"]]
    # reindex-ffilled onto the 15m frame; slope needs the same 24h VWAP).
    slope_by_15m: list[float | None] = [None] * len(closes)
    above_by_15m: list[bool] = [False] * len(closes)
    below_by_15m: list[bool] = [False] * len(closes)
    hourly_closes: dict[datetime, float] = {}
    hourly_vwap: dict[datetime, float | None] = {}
    for index in range(len(closes)):
        hour_end = timestamps[index].replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        if timestamps[index] != hour_end:
            continue
        hourly_closes[hour_end] = closes[index]
        hourly_vwap[hour_end] = vwap[index]
    for index in range(len(closes)):
        hour_end = timestamps[index].replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        previous = hour_end - timedelta(hours=3)
        if hour_end not in hourly_closes or previous not in hourly_closes:
            continue
        current_vwap = hourly_vwap.get(hour_end)
        past_vwap = hourly_vwap.get(previous)
        ends1 = [_utc(v) for v in bars1h["timestamp"].to_list()]
        a_index = bisect_right(ends1, hour_end) - 1
        atr_value = atr1h[a_index] if a_index >= 0 else None
        if current_vwap is None or past_vwap is None or atr_value is None or atr_value <= 0:
            continue
        slope_by_15m[index] = (current_vwap - past_vwap) / atr_value
    for index in range(len(closes)):
        hour_end = timestamps[index].replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        previous = hour_end - timedelta(hours=1)
        if hour_end not in hourly_closes or previous not in hourly_closes:
            continue
        current_vwap = hourly_vwap.get(hour_end)
        previous_vwap = hourly_vwap.get(previous)
        if current_vwap is None or previous_vwap is None:
            continue
        above_by_15m[index] = (hourly_closes[hour_end] > current_vwap
                               and hourly_closes[previous] > previous_vwap)
        below_by_15m[index] = (hourly_closes[hour_end] < current_vwap
                               and hourly_closes[previous] < previous_vwap)

    # Sequential per-side state machine (authority trend_pullback_v1.signals).
    pending: dict[int, dict | None] = {1: None, -1: None}
    candidates: list[dict] = []
    last = len(closes) - 1
    for index in range(len(closes)):
        if (index == 0 or None in (slope_by_15m[index], rsi[index], atr15[index], sigma[index])
                or sigma[index] <= 0):
            continue
        for side in (1, -1):
            regime = (slope_by_15m[index] > config.TREND_PULLBACK_TREND_SLOPE and above_by_15m[index]) \
                if side == 1 else (slope_by_15m[index] < -config.TREND_PULLBACK_TREND_SLOPE and below_by_15m[index])
            valid = side * (closes[index] - vwap[index]) >= 0
            state = pending[side]
            if state is not None:
                if (index - state["index"] > 4 or not regime or not valid):
                    pending[side] = state = None
            if state is not None:
                state["low"] = min(state["low"], lows[index])
                state["high"] = max(state["high"], highs[index])
                recovered = side * (rsi[index] - 50.0) > 0
                if not recovered:
                    state.pop("cross", None)
                elif side * (rsi[index - 1] - 50.0) <= 0:
                    state["cross"] = index
                price_break = closes[index] > highs[index - 1] if side == 1 else closes[index] < lows[index - 1]
                if recovered and "cross" in state and price_break:
                    stop = (state["low"] - config.TREND_PULLBACK_ATR_BUFFER * atr15[index] if side == 1
                            else state["high"] + config.TREND_PULLBACK_ATR_BUFFER * atr15[index])
                    candidates.append({
                        "confirmation_index": index,
                        "direction": side,
                        "stop": stop,
                        "vwap": vwap[index],
                        "entry_bound": upper2[index] if side == 1 else lower2[index],
                        "rsi": rsi[index],
                        "slope": slope_by_15m[index],
                    })
                    pending[side] = None
                    continue
            touch = (lows[index] <= upper1[index] and highs[index] >= vwap[index]) if side == 1 \
                else (highs[index] >= lower1[index] and lows[index] <= vwap[index])
            momentum = 40.0 <= rsi[index] <= 50.0 if side == 1 else 50.0 <= rsi[index] <= 60.0
            impulse = _impulse(closes, upper1, lower1, side, index)
            if pending[side] is None and regime and valid and touch and momentum and impulse:
                pending[side] = {"index": index, "low": lows[index], "high": highs[index]}

    if not candidates:
        return None
    candidate = candidates[-1]
    if candidate["confirmation_index"] != last:
        return None
    direction = "long" if candidate["direction"] == 1 else "short"
    stop = candidate["stop"]
    entry_ref = closes[last]
    if direction == "long" and entry_ref >= candidate["entry_bound"]:
        return None
    if direction == "short" and entry_ref <= candidate["entry_bound"]:
        return None
    if direction == "long" and stop >= entry_ref:
        return None
    if direction == "short" and stop <= entry_ref:
        return None
    risk = abs(entry_ref - stop)
    if risk <= 0:
        return None
    # Simulated policy: fixed 2R target (target_r_fixed=2.0 in the runner).
    target = entry_ref + config.TREND_PULLBACK_TARGET_R_FIXED * risk if direction == "long" \
        else entry_ref - config.TREND_PULLBACK_TARGET_R_FIXED * risk
    if abs(target - entry_ref) / risk < config.TREND_PULLBACK_MINIMUM_RR:
        return None

    observed = timestamps[last]
    return {
        "schema_version": 1,
        "strategy_id": STRATEGY_ID,
        "plugin_version": PLUGIN_VERSION,
        "asset": asset.upper(),
        "direction": direction,
        "setup_class": "trend_pullback_vwap",
        "phase": "momentum_price_confirmation",
        "observed_at": observed.isoformat(),
        "valid_until": (observed + timedelta(minutes=config.TREND_PULLBACK_ENTRY_VALIDITY_MINUTES)).isoformat(),
        "horizon_minutes": config.TREND_PULLBACK_ENTRY_VALIDITY_MINUTES,
        "confidence": 0.5,
        "confidence_status": "uncalibrated",
        "entry_condition": {"type": "market_next_bar_open", "price": entry_ref},
        "entry_price": entry_ref,
        "invalidation_price": stop,
        "targets": [target],
        "metadata": {
            "execution_timeframe": "5m",
            "signal_timeframe": "15m",
            "entry_timing": "next_completed_15m_open",
            "stop_policy": "accumulated_pullback_extreme_plus_0.5atr15",
            "target_policy": "fixed_2r",
            "bracket_spec": {
                "version": "trend_pullback_v1",
                "stop": stop,
                "target": target,
                "stop_semantics": "intrabar on 5m bars, gap-through fills at open",
                "priority": "stop checked before target in the same bar",
            },
        },
        "feature_snapshot": {
            "source_symbol": symbol,
            "signal_timeframe": "15m",
            "vwap_15m": candidate["vwap"],
            "sigma_15m": sigma[last],
            "entry_bound_2sigma": candidate["entry_bound"],
            "rsi14_15m": candidate["rsi"],
            "regime_slope": candidate["slope"],
            "atr14_15m": atr15[last],
            "stop": stop,
            "target": target,
            "minimum_rr": config.TREND_PULLBACK_MINIMUM_RR,
            "cutoff": cutoff.isoformat(),
        },
    }


def _impulse(closes: list[float], upper1: list[float | None], lower1: list[float | None],
             side: int, index: int) -> bool:
    """Within the prior 8 bars a close beyond the ±1σ band (shifted 1 bar)."""
    start = max(0, index - 8)
    for prior in range(start, index):
        band = upper1[prior] if side == 1 else lower1[prior]
        if band is None:
            continue
        if side == 1 and closes[prior] > band:
            return True
        if side == -1 and closes[prior] < band:
            return True
    return False


def _fresh(bars, cutoff: datetime, max_age_seconds: float) -> bool:
    latest = _utc(bars["timestamp"][-1])
    age = (cutoff - latest).total_seconds()
    return 0 <= age <= max_age_seconds


def run_plugin(cutoff_id: str, snapshot: dict) -> list[dict]:
    cutoff = cutoff_from_id(str(snapshot.get("cutoff_at") or cutoff_id), snapshot.get("now"))
    if cutoff.minute % 15:
        return []
    conn, owns_conn = strategy_market_connection(snapshot.get("market_db_path"))
    try:
        events = []
        for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
            bars5m = load_bars_for_interval(conn, symbol, "5m", cutoff)
            bars1h = load_bars_for_interval(conn, symbol, "1h", cutoff)
            event = evaluate_symbol(bars5m, bars1h, asset=asset, symbol=symbol, cutoff=cutoff)
            if event is not None and not has_active_event(STRATEGY_ID, asset, event["direction"], now=cutoff):
                event["input_snapshot_id"] = cutoff_id
                events.append(event)
        return events
    finally:
        if owns_conn:
            conn.close()
