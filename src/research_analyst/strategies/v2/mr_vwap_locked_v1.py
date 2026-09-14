"""MR VWAP locked-v1 port: persistent volume-peak anchored VWAP mean reversion.

Authority: repo-final/scripts/backtest_vwap_rsi_prototype.py (engine +
signals(d, 'mean_reversion', ...)) with scripts/mr_volume_anchor.py and the
frozen config in backtest_results/mr/mr_locked_v1/strategy.json
(see engine_handoff/all_families_engine_specs.md section 5 — REJECTED at the
acceptance gate; ported per operator decision as the family baseline).

Self-contained plugin: RSI comes from the native ``wilder_rsi`` engine, ATR
from ``wilder_atr_series``; the persistent volume-peak VWAP anchor, sigma
bands, and the sequential confirmation state machine are private to this
strategy.

Frame: 15m signals resampled from canonical completed 5m bars (exact 15-bar
groups only, via the shared resampler). Anchor: highest completed-hourly base
volume, seeded from the prior 72 completed hours (oldest tie wins), reset only
when a strictly higher completed hourly base volume appears; VWAP and
volume-weighted population sigma from completed 15m HLC3 since anchor; bands
±1σ/±2σ.

Setup (long): low < -2σ AND RSI14 < 40 (short: high > +2σ AND RSI14 > 60).
Confirmation (rsi_then_price): within 3 completed 15m bars, RSI recovers and
stays recovered, then a close back inside the 2σ band. Regime gate:
|ΔVWAP(3h)/ATR14(1h)| < 0.25 at the same anchor. Entry: next completed 15m
open, price still beyond the frozen ±1σ bound. Stop: setup-through-
confirmation extreme ± 0.5*ATR14(15m), fixed. Target: confirmation VWAP or 2R,
whichever is NEARER; planned R:R >= 1.2. Intrabar TP/SL on 5m bars is
executor-bracket territory (stop priority, gap fills at open).
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

STRATEGY_ID = config.MR_VWAP_STRATEGY_ID
PLUGIN_VERSION = "v1"


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _hourly_base_volume(bars15: pl.DataFrame) -> list[tuple[datetime, float]]:
    """Completed-hourly base volume, end-stamped (strategy-private grouping)."""
    rows = sorted(
        ((_utc(r["timestamp"]), r) for r in bars15.to_dicts()),
        key=lambda item: item[0],
    )
    buckets: dict[datetime, list[tuple[datetime, dict]]] = {}
    for stamp, row in rows:
        offset = int(stamp.timestamp() % 3600)
        bucket_start = stamp - timedelta(seconds=3600 if offset == 0 else offset)
        buckets.setdefault(bucket_start, []).append((stamp, row))
    out = []
    for bucket_start in sorted(buckets):
        members = buckets[bucket_start]
        if len(members) != 4:
            continue
        expected = [bucket_start + timedelta(minutes=15 * (i + 1)) for i in range(4)]
        if [stamp for stamp, _ in members] != expected:
            continue
        out.append((bucket_start + timedelta(hours=1),
                    sum(float(r.get("volume") or 0.0) for _, r in members)))
    return out


def _persistent_peak_anchors(hourly: list[tuple[datetime, float]],
                             seed_hours: int) -> list[datetime | None]:
    """Seed from the trailing window; retain the anchor until its volume is exceeded.

    Authority mr_volume_anchor.persistent_peak_anchors: oldest tie wins; a
    strictly greater completed hourly volume replaces the anchor at that hour.
    Returns the anchor bucket START per completed hour (None until seeded).
    """
    result: list[datetime | None] = []
    anchor: datetime | None = None
    threshold = -math.inf
    for index, (hour_end, volume) in enumerate(hourly):
        if anchor is None:
            if index + 1 >= seed_hours:
                window = hourly[index + 1 - seed_hours:index + 1]
                best_volume = max(v for _, v in window)
                anchor_end = next(end for end, v in window if v == best_volume)
                anchor = anchor_end - timedelta(hours=1)
                threshold = best_volume
        elif volume > threshold:
            anchor = hour_end - timedelta(hours=1)
            threshold = volume
        result.append(anchor)
    return result


def evaluate_symbol(bars5m, bars1h, *, asset: str, symbol: str, cutoff: datetime) -> dict | None:
    cutoff = _utc(cutoff)
    if bars5m.is_empty() or bars1h.is_empty():
        return None
    bars5m = bars5m.filter(bars5m["timestamp"] <= cutoff).sort("timestamp")
    if not _fresh(bars5m, cutoff, 5 * 60 + config.DATA_FRESHNESS_MAX_SECONDS):
        return None
    bars15 = resample_ohlcv(bars5m, "15m")
    if bars15.is_empty() or bars15.height < config.MR_VWAP_ANCHOR_SEED_HOURS * 4 + 30:
        return None
    bars1h = bars1h.filter(bars1h["timestamp"] <= cutoff).sort("timestamp")

    timestamps = [_utc(v) for v in bars15["timestamp"].to_list()]
    highs = [float(v) for v in bars15["high"].to_list()]
    lows = [float(v) for v in bars15["low"].to_list()]
    closes = [float(v) for v in bars15["close"].to_list()]
    volumes = [float(v or 0.0) for v in bars15["volume"].to_list()]
    p = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(len(closes))]

    from polars_indicators import wilder_atr_series

    rsi = wilder_rsi(closes, 14)
    atr15 = wilder_atr_series(bars15, 14).to_list()
    atr1h = wilder_atr_series(bars1h, 14).to_list()

    hourly = _hourly_base_volume(bars15)
    if len(hourly) < config.MR_VWAP_ANCHOR_SEED_HOURS:
        return None
    anchors = _persistent_peak_anchors(hourly, config.MR_VWAP_ANCHOR_SEED_HOURS)

    # Cumulative volume / weighted price / weighted square since the anchor
    # start (authority anchored_sums: begin at the first 15m bar strictly after
    # the anchor bucket starts; sums run causally to the current bar).
    vwap: list[float | None] = [None] * len(closes)
    sigma: list[float | None] = [None] * len(closes)
    upper1: list[float | None] = [None] * len(closes)
    lower1: list[float | None] = [None] * len(closes)
    upper2: list[float | None] = [None] * len(closes)
    lower2: list[float | None] = [None] * len(closes)
    begin_index = 0
    for index, anchor_start in enumerate(anchors):
        if anchor_start is None:
            continue
        begin_target = anchor_start + timedelta(minutes=15)
        while begin_index < index and timestamps[begin_index] < begin_target:
            begin_index += 1
        if begin_index >= len(closes) or timestamps[begin_index] < begin_target:
            continue
        cum_v = sum(volumes[begin_index:index + 1])
        if cum_v <= 0:
            continue
        cum_vp = sum(p[i] * volumes[i] for i in range(begin_index, index + 1))
        cum_vpp = sum(p[i] ** 2 * volumes[i] for i in range(begin_index, index + 1))
        mean = cum_vp / cum_v
        variance = cum_vpp / cum_v - mean ** 2
        s = math.sqrt(variance) if variance > 0 else 0.0
        vwap[index] = mean
        sigma[index] = s
        upper1[index] = mean + s
        lower1[index] = mean - s
        upper2[index] = mean + 2 * s
        lower2[index] = mean - 2 * s

    # Hourly regime slope: (VWAP - VWAP 3h ago) / ATR14(1h), same anchor both
    # endpoints; sampled at completed hours and held over the following hour.
    slope_by_15m: list[float | None] = [None] * len(closes)
    hourly_index: dict[datetime, int] = {}
    for index, (hour_end, _) in enumerate(_hourly_base_volume(bars15)):
        hourly_index[hour_end] = index
    for index in range(len(closes)):
        hour_end = timestamps[index].replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        if hour_end not in hourly_index:
            continue
        h_index = hourly_index[hour_end]
        if h_index < 3 or anchors[h_index] != anchors[h_index - 3]:
            continue
        current = _hourly_vwap(vwap, timestamps, hour_end)
        past = _hourly_vwap(vwap, timestamps, hour_end - timedelta(hours=3))
        ends1 = [_utc(v) for v in bars1h["timestamp"].to_list()]
        a_index = bisect_right(ends1, hour_end) - 1
        atr_value = atr1h[a_index] if a_index >= 0 else None
        if current is None or past is None or atr_value is None or atr_value <= 0:
            continue
        slope_by_15m[index] = (current - past) / atr_value

    # Sequential per-side state machine (mr_confirmation='rsi_then_price',
    # mr_entry='2sigma', mr_target='vwap', no time exit).
    pending: dict[int, dict | None] = {1: None, -1: None}
    candidates: list[dict] = []
    last = len(closes) - 1
    for index in range(len(closes)):
        if (index == 0 or None in (slope_by_15m[index], rsi[index], atr15[index], sigma[index])
                or sigma[index] <= 0):
            continue
        for direction in (1, -1):
            regime = abs(slope_by_15m[index]) < config.MR_VWAP_RANGE_SLOPE
            band = lower2[index] if direction == 1 else upper2[index]
            state = pending[direction]
            if state is not None:
                expired = index - state["index"] > config.MR_VWAP_CONFIRMATION_WINDOW_BARS
                if expired or not regime:
                    pending[direction] = state = None
            if state is not None:
                state["low"] = min(state["low"], lows[index])
                state["high"] = max(state["high"], highs[index])
                threshold = config.MR_VWAP_RSI_LOWER if direction == 1 else config.MR_VWAP_RSI_UPPER
                rsi_valid = direction * (rsi[index] - threshold) > 0
                rsi_cross = rsi_valid and direction * (rsi[index - 1] - threshold) <= 0
                if not rsi_valid:
                    state.pop("rsi_cross_index", None)
                elif rsi_cross:
                    state["rsi_cross_index"] = index
                price_valid = closes[index] > band if direction == 1 else closes[index] < band
                if (state.get("rsi_cross_index") is not None and rsi_valid and price_valid):
                    stop = (state["low"] - config.MR_VWAP_ATR_BUFFER * atr15[index] if direction == 1
                            else state["high"] + config.MR_VWAP_ATR_BUFFER * atr15[index])
                    candidates.append({
                        "confirmation_index": index,
                        "direction": direction,
                        "stop": stop,
                        "vwap": vwap[index],
                        "entry_bound": lower1[index] if direction == 1 else upper1[index],
                        "rsi": rsi[index],
                        "slope": slope_by_15m[index],
                    })
                    pending[direction] = None
                    continue
            if pending[direction] is None and regime:
                touched = lows[index] < band if direction == 1 else highs[index] > band
                setup_rsi = rsi[index] < config.MR_VWAP_RSI_LOWER if direction == 1 else \
                    rsi[index] > config.MR_VWAP_RSI_UPPER
                if touched and setup_rsi:
                    pending[direction] = {"index": index, "low": lows[index], "high": highs[index]}

    if not candidates:
        return None
    # Only the most recent confirmation is still executable at this cutoff.
    candidate = candidates[-1]
    if candidate["confirmation_index"] != last:
        return None
    direction = "long" if candidate["direction"] == 1 else "short"
    stop = candidate["stop"]
    # Entry: NEXT completed 15m open; the ±1σ bound is re-checked on that open
    # by the executor. The reference fill here is the confirmation close.
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
    signal_target = candidate["vwap"]
    cap = entry_ref + (config.MR_VWAP_TARGET_R_CAP * risk if direction == "long"
                       else -config.MR_VWAP_TARGET_R_CAP * risk)
    nearer_is_cap = (cap < signal_target) if direction == "long" else (cap > signal_target)
    target = cap if nearer_is_cap else signal_target
    if abs(target - entry_ref) / risk < config.MR_VWAP_MINIMUM_RR:
        return None

    observed = timestamps[last]
    return {
        "schema_version": 1,
        "strategy_id": STRATEGY_ID,
        "plugin_version": PLUGIN_VERSION,
        "asset": asset.upper(),
        "direction": direction,
        "setup_class": "mr_vwap_band_reversion",
        "phase": "rsi_then_price_confirmation",
        "observed_at": observed.isoformat(),
        "valid_until": (observed + timedelta(minutes=config.MR_VWAP_ENTRY_VALIDITY_MINUTES)).isoformat(),
        "horizon_minutes": config.MR_VWAP_ENTRY_VALIDITY_MINUTES,
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
            "stop_policy": "setup_extreme_plus_0.5atr15_fixed",
            "target_policy": "vwap_or_2r_whichever_nearer",
            "bracket_spec": {
                "version": "mr_locked_v1",
                "stop": stop,
                "target": target,
                "stop_semantics": "intrabar on 5m bars, gap-through fills at open",
                "priority": "stop checked before target in the same bar",
            },
        },
        "feature_snapshot": {
            "source_symbol": symbol,
            "signal_timeframe": "15m",
            "vwap": candidate["vwap"],
            "sigma": sigma[last],
            "entry_bound_1sigma": candidate["entry_bound"],
            "rsi14_15m": candidate["rsi"],
            "regime_slope": candidate["slope"],
            "atr14_15m": atr15[last],
            "stop": stop,
            "target": target,
            "minimum_rr": config.MR_VWAP_MINIMUM_RR,
            "cutoff": cutoff.isoformat(),
        },
    }


def _hourly_vwap(vwap: list[float | None], timestamps: list[datetime],
                 hour_end: datetime) -> float | None:
    """Last 15m VWAP value belonging to the completed hour ending at hour_end."""
    window_start = hour_end - timedelta(hours=1)
    values = [vwap[i] for i in range(len(vwap))
              if window_start < timestamps[i] <= hour_end and vwap[i] is not None]
    return values[-1] if values else None


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
