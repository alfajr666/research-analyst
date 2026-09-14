"""BB/Keltner squeeze trend port: 4h squeeze release, 30m event execution.

Authority: repo-final/vectorbt/strategies/bb_squeeze_trend.py
(see engine_handoff/all_families_engine_specs.md section 2 — watchlist,
insufficient sample, ported disabled-by-default per operator decision).

Self-contained plugin using only the repository's native indicator engines
(``strategy_features``/``polars_indicators``) plus private per-plugin helpers
for the two indicators that have no native kernel (4h linear-regression slope
and the 4h frame grouping).

Execution idiom preserved: EVENT-EXECUTION on the 30m bar that receives the
fire event (not next-open). The 30m frame is built by causal grouping of
completed 5m bars; all signal indicators live on the grouped completed 4h bars.
Stop-only management: initial = fill ∓ 1.74*ATR14(4h) frozen at the fire bar,
then a ratcheting trail at extreme ∓/± 3.71*ATR14(4h continuously mapped).
Sizing (3% risk, 95% margin cap, leverage) is engine-side and never reproduced.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Any

import polars as pl

import config
from strategy_features import build_feature_frame
from strategy_v2_context import (
    cutoff_from_id,
    ema_series,
    evaluation_symbols,
    has_active_event,
    load_bars_for_interval,
    strategy_market_connection,
)

STRATEGY_ID = config.BB_SQUEEZE_TREND_STRATEGY_ID
PLUGIN_VERSION = "v1"


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _group_bars(bars: pl.DataFrame, span_seconds: int, required: int) -> pl.DataFrame:
    """Group completed end-stamped bars into exact UTC-aligned buckets.

    A bar ENDING exactly at a bucket boundary closes that bucket; incomplete
    buckets are dropped (fail-closed, mirroring resample_ohlcv semantics).
    """
    if bars.is_empty():
        return bars
    timestamps = [_utc(v) for v in bars["timestamp"].to_list()]
    rows = bars.to_dicts()
    buckets: dict[datetime, list[dict]] = {}
    for stamp, row in zip(timestamps, rows):
        offset = int(stamp.timestamp() % span_seconds)
        bucket_start = stamp - timedelta(seconds=span_seconds if offset == 0 else offset)
        buckets.setdefault(bucket_start, []).append(row)
    deltas = sorted({int((timestamps[i] - timestamps[i - 1]).total_seconds())
                     for i in range(1, len(timestamps)) if timestamps[i] > timestamps[i - 1]})
    base_seconds = next((d for d in deltas if d > 0), 0)
    if not base_seconds or span_seconds % base_seconds:
        return bars.head(0)
    out = []
    for bucket_start in sorted(buckets):
        members = buckets[bucket_start]
        if len(members) != required:
            continue
        expected = [bucket_start + timedelta(seconds=base_seconds * (i + 1)) for i in range(required)]
        actual = sorted(_utc(m["timestamp"]) for m in members)
        if actual != expected:
            continue
        sorted_members = sorted(members, key=lambda r: _utc(r["timestamp"]))
        out.append({
            "timestamp": bucket_start + timedelta(seconds=span_seconds),
            "open": float(sorted_members[0]["open"]),
            "high": max(float(m["high"]) for m in sorted_members),
            "low": min(float(m["low"]) for m in sorted_members),
            "close": float(sorted_members[-1]["close"]),
            "volume": sum(float(m.get("volume") or 0.0) for m in sorted_members),
        })
    return pl.DataFrame(out) if out else bars.head(0)


def _linreg_slope_series(values: list[float], period: int) -> list[float | None]:
    """Rolling least-squares slope of close (strategy-private, no native kernel)."""
    if period <= 1:
        return [None] * len(values)
    x_centered = [i - (period - 1) / 2.0 for i in range(period)]
    denominator = sum(x * x for x in x_centered)
    out: list[float | None] = [None] * (period - 1)
    for end in range(period, len(values) + 1):
        window = values[end - period:end]
        y_mean = sum(window) / period
        numerator = sum(xc * (y - y_mean) for xc, y in zip(x_centered, window))
        out.append(numerator / denominator)
    return out


def evaluate_symbol(bars5m, bars1h, *, asset: str, symbol: str, cutoff: datetime) -> dict | None:
    cutoff = _utc(cutoff)
    if bars5m.is_empty() or bars1h.is_empty():
        return None
    bars5m = bars5m.filter(bars5m["timestamp"] <= cutoff).sort("timestamp")
    if not _fresh(bars5m, cutoff, 5 * 60 + config.DATA_FRESHNESS_MAX_SECONDS):
        return None
    bars30 = _group_bars(bars5m, 1800, 6)
    if bars30.is_empty():
        return None
    h4 = _group_bars(bars30, 14400, 8)
    if h4.height < 2 * config.BB_SQUEEZE_BB_PERIOD + config.BB_SQUEEZE_SLOPE_PERIOD:
        return None

    features4 = build_feature_frame(
        h4,
        bollinger={"bb": (config.BB_SQUEEZE_BB_PERIOD, config.BB_SQUEEZE_BB_DEV)},
        ema={"kc_mid": config.BB_SQUEEZE_BB_PERIOD},
        atr={"atr14": 14},
    )
    mid4 = features4["bb_middle"].to_list()
    bb_upper4 = features4["bb_upper"].to_list()
    bb_lower4 = features4["bb_lower"].to_list()
    kc_mid4 = features4["kc_mid"].to_list()
    atr4 = features4["atr14"].to_list()
    closes4 = [float(v) for v in h4["close"].to_list()]
    slope4 = _linreg_slope_series(closes4, config.BB_SQUEEZE_SLOPE_PERIOD)
    from polars_indicators import dmi_adx_series

    adx4, _, _ = dmi_adx_series(h4, 14, 14)
    kc_upper4 = [None if (m is None or a is None) else m + config.BB_SQUEEZE_KC_MULT * a
                 for m, a in zip(kc_mid4, atr4)]
    kc_lower4 = [None if (m is None or a is None) else m - config.BB_SQUEEZE_KC_MULT * a
                 for m, a in zip(kc_mid4, atr4)]
    squeeze_on = [
        bool(u is not None and l is not None and ku is not None and kl is not None
             and u < ku and l > kl)
        for u, l, ku, kl in zip(bb_upper4, bb_lower4, kc_upper4, kc_lower4)
    ]

    # squeeze_fired: release after >= min_squeeze_bars of consecutive squeeze.
    squeeze_fired: list[bool] = [False] * h4.height
    squeeze_run_at_fire: list[int] = [0] * h4.height
    run = 0
    for index in range(1, h4.height):
        run = run + 1 if squeeze_on[index - 1] else 0
        if (squeeze_on[index - 1] and not squeeze_on[index]
                and run >= config.BB_SQUEEZE_MIN_SQUEEZE_BARS):
            squeeze_fired[index] = True
            squeeze_run_at_fire[index] = run

    exec_timestamps = [_utc(v) for v in bars30["timestamp"].to_list()]
    ends4 = [_utc(v) for v in h4["timestamp"].to_list()]

    def _map_completed(series: list[Any]) -> list[Any]:
        out = []
        for stamp in exec_timestamps:
            index = bisect_right(ends4, stamp) - 1
            out.append(series[index] if index >= 0 else None)
        return out

    long_fire_4h = [
        bool(fire and adx is not None and adx >= config.BB_SQUEEZE_ADX_THRESHOLD
             and slope is not None and slope > 0)
        for fire, adx, slope in zip(squeeze_fired, adx4, slope4)
    ]
    short_fire_4h = [
        bool(fire and adx is not None and adx >= config.BB_SQUEEZE_ADX_THRESHOLD
             and slope is not None and slope < 0)
        for fire, adx, slope in zip(squeeze_fired, adx4, slope4)
    ]
    long_fire = [bool(long_fire_4h[bisect_right(ends4, s) - 1]) if bisect_right(ends4, s) - 1 >= 0 else False
                 for s in exec_timestamps]
    short_fire = [bool(short_fire_4h[bisect_right(ends4, s) - 1]) if bisect_right(ends4, s) - 1 >= 0 else False
                  for s in exec_timestamps]
    atr_exec = _map_completed(atr4)
    atr_initial = _map_completed(
        [atr4[index] if fire else None for index, fire in enumerate(squeeze_fired)]
    )

    last = len(exec_timestamps) - 1
    if not (long_fire[last] or short_fire[last]):
        return None
    direction = "long" if long_fire[last] else "short"
    entry_ref = float(bars30["close"][-1])
    atr_at_fire = atr_initial[last]
    if atr_at_fire is None or atr_at_fire <= 0 or entry_ref <= 0:
        return None
    stop = (entry_ref - config.BB_SQUEEZE_ATR_STOP_MULT * atr_at_fire if direction == "long"
            else entry_ref + config.BB_SQUEEZE_ATR_STOP_MULT * atr_at_fire)
    if direction == "long" and stop >= entry_ref:
        return None
    if direction == "short" and stop <= entry_ref:
        return None

    observed = exec_timestamps[last]
    return {
        "schema_version": 1,
        "strategy_id": STRATEGY_ID,
        "plugin_version": PLUGIN_VERSION,
        "asset": asset.upper(),
        "direction": direction,
        "setup_class": "bb_keltner_squeeze_release",
        "phase": "squeeze_fire",
        "observed_at": observed.isoformat(),
        "valid_until": (observed + timedelta(minutes=config.BB_SQUEEZE_ENTRY_VALIDITY_MINUTES)).isoformat(),
        "horizon_minutes": config.BB_SQUEEZE_ENTRY_VALIDITY_MINUTES,
        "confidence": 0.5,
        "confidence_status": "uncalibrated",
        "entry_condition": {"type": "market_on_fire_bar", "price": entry_ref},
        "entry_price": entry_ref,
        "invalidation_price": stop,
        "targets": [],
        "metadata": {
            "execution_timeframe": "30m",
            "structure_timeframe": "4h",
            "entry_timing": "event_execution_on_fire_bar",
            "stop_policy": "frozen_fire_bar_atr_initial_then_ratcheting_trail",
            "target_policy": "none_stop_only",
            "strategy_exits": {
                "trail_rule": (
                    f"ratcheting trail: long peak - {config.BB_SQUEEZE_ATR_TRAIL_MULT}*ATR14(4h), "
                    f"short trough + {config.BB_SQUEEZE_ATR_TRAIL_MULT}*ATR14(4h); "
                    "only ever tightens; ATR mapped continuously from completed 4h bars"
                ),
            },
            "bracket_spec": {
                "version": "bb_squeeze_trend",
                "stop": stop,
                "trail_atr_mult": config.BB_SQUEEZE_ATR_TRAIL_MULT,
                "trail_atr_source": "4h",
                "trail_semantics": "stop-only management; no TP levels",
            },
        },
        "feature_snapshot": {
            "source_symbol": symbol,
            "execution_timeframe": "30m",
            "squeeze_bars": _last_fire_run(squeeze_run_at_fire),
            "adx14_4h": adx4[last] if last < len(adx4) else None,
            "slope11_4h": slope4[last],
            "atr14_4h": atr_exec[last],
            "atr_initial_4h": atr_at_fire,
            "stop": stop,
            "cutoff": cutoff.isoformat(),
        },
    }


def _last_fire_run(squeeze_run_at_fire: list[int]) -> int:
    for value in reversed(squeeze_run_at_fire):
        if value:
            return value
    return 0


def _fresh(bars, cutoff: datetime, max_age_seconds: float) -> bool:
    latest = _utc(bars["timestamp"][-1])
    age = (cutoff - latest).total_seconds()
    return 0 <= age <= max_age_seconds


def run_plugin(cutoff_id: str, snapshot: dict) -> list[dict]:
    cutoff = cutoff_from_id(str(snapshot.get("cutoff_at") or cutoff_id), snapshot.get("now"))
    if cutoff.minute % 30:
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
