"""TrendWall v5 port: 30m execution with completed-1h EMA99 wall structure.

Authority: repo-final/vectorbt/strategies/trend_wall.py
(see engine_handoff/all_families_engine_specs.md section 7 — REJECTED,
median PF 0.85; ported per operator decision so behavior stays reproducible;
distinct from the repo's existing 15m `trend-wall-v1` plugin).

Self-contained plugin: EMA7/EMA26/EMA99 from the native ``ema_series`` engine,
RSI from ``wilder_rsi``, ATR from ``wilder_atr_series``; the 30m grouping,
volume ratio, and completed-1h mapping are private to this strategy.

Frame: 30m execution (causal grouping of completed 5m bars); structure on
completed 1h bars only. Indicators (30m): ATR16 (Wilder), RSI14, RSI-MA3,
volume/mean20(volume) ratio shifted 1 bar. Structure (1h): EMA7, EMA26, EMA99
wall, ADX14 (native seeded-RMA DMI).

Setup (long): close > wall AND |close-wall|/wall < 1% AND low <= wall AND
1h EMA7 > 1h EMA26 AND 1h ADX > 20. Short mirrored. Confirmation: RSI < 40
AND RSI crossing up through RSI-MA3 AND volume ratio > 0.5 (short: RSI > 60
crossing down). Entry = next 30m open after confirmation.

Exits: structure exit — long close < wall - 0.5*ATR16 (short mirrored),
applied next bar; stop = (confirmation-candle high if below wall else wall)
- 1*ATR16 for long (short mirrored), executed as an ATR-fraction stop by the
portfolio layer in the backtest and as an executor bracket here.
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
    ema_series,
    evaluation_symbols,
    has_active_event,
    load_bars_for_interval,
    strategy_market_connection,
    wilder_rsi,
)

STRATEGY_ID = config.TREND_WALL_V5_STRATEGY_ID
PLUGIN_VERSION = "v1"


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _group_bars_30m(bars: pl.DataFrame) -> pl.DataFrame:
    """Group completed end-stamped 5m bars into exact UTC-aligned 30m buckets.

    A bar ENDING exactly at a bucket boundary closes that bucket; incomplete
    buckets are dropped (fail-closed, mirroring resample_ohlcv semantics).
    Strategy-private: no native 30m kernel exists.
    """
    if bars.is_empty():
        return bars
    timestamps = [_utc(v) for v in bars["timestamp"].to_list()]
    rows = bars.to_dicts()
    buckets: dict[datetime, list[dict]] = {}
    for stamp, row in zip(timestamps, rows):
        offset = int(stamp.timestamp() % 1800)
        bucket_start = stamp - timedelta(seconds=1800 if offset == 0 else offset)
        buckets.setdefault(bucket_start, []).append(row)
    deltas = sorted({int((timestamps[i] - timestamps[i - 1]).total_seconds())
                     for i in range(1, len(timestamps)) if timestamps[i] > timestamps[i - 1]})
    base_seconds = next((d for d in deltas if d > 0), 0)
    if not base_seconds or 1800 % base_seconds:
        return bars.head(0)
    required = 1800 // base_seconds
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
            "timestamp": bucket_start + timedelta(seconds=1800),
            "open": float(sorted_members[0]["open"]),
            "high": max(float(m["high"]) for m in sorted_members),
            "low": min(float(m["low"]) for m in sorted_members),
            "close": float(sorted_members[-1]["close"]),
            "volume": sum(float(m.get("volume") or 0.0) for m in sorted_members),
        })
    return pl.DataFrame(out) if out else bars.head(0)


def _adx_1h(bars1h: pl.DataFrame) -> list[float | None]:
    from polars_indicators import dmi_adx_series

    adx, _, _ = dmi_adx_series(bars1h, 14, 14)
    return adx


def _map_completed(bars1h: pl.DataFrame, series: list[Any],
                   exec_timestamps: list[datetime]) -> list[Any]:
    ends = [_utc(v) for v in bars1h["timestamp"].to_list()]
    out: list[Any] = []
    for stamp in exec_timestamps:
        index = bisect_right(ends, _utc(stamp)) - 1
        out.append(series[index] if index >= 0 else None)
    return out


def evaluate_symbol(bars5m, bars1h, *, asset: str, symbol: str, cutoff: datetime) -> dict | None:
    cutoff = _utc(cutoff)
    if bars5m.is_empty() or bars1h.is_empty():
        return None
    bars5m = bars5m.filter(bars5m["timestamp"] <= cutoff).sort("timestamp")
    bars1h = bars1h.filter(bars1h["timestamp"] <= cutoff).sort("timestamp")
    if not _fresh(bars5m, cutoff, 5 * 60 + config.DATA_FRESHNESS_MAX_SECONDS):
        return None
    bars30 = _group_bars_30m(bars5m)
    if bars30.is_empty() or bars30.height < 25 or bars1h.height < 30:
        return None

    from polars_indicators import wilder_atr_series

    closes30 = [float(v) for v in bars30["close"].to_list()]
    highs30 = [float(v) for v in bars30["high"].to_list()]
    lows30 = [float(v) for v in bars30["low"].to_list()]
    volumes30 = [float(v or 0.0) for v in bars30["volume"].to_list()]

    atr30 = wilder_atr_series(bars30, config.TREND_WALL_V5_ATR_LENGTH).to_list()
    rsi30 = wilder_rsi(closes30, 14)
    rsi_ma3 = [
        None if any(rsi30[i] is None for i in (index - 2, index - 1, index)) or index < 2
        else (rsi30[index] + rsi30[index - 1] + rsi30[index - 2]) / 3.0
        for index in range(len(rsi30))
    ]
    volume_ma20: list[float | None] = []
    for index in range(len(volumes30)):
        if index < 20:
            volume_ma20.append(None)
        else:
            window = volumes30[index - 20:index]
            volume_ma20.append(sum(window) / 20.0)
    volume_ratio: list[float | None] = []
    for index in range(len(volumes30)):
        if index == 0 or volume_ma20[index - 1] in (None, 0.0):
            volume_ratio.append(None)
            continue
        volume_ratio.append(volumes30[index] / volume_ma20[index - 1])

    closes1 = [float(v) for v in bars1h["close"].to_list()]
    ema7_1h = ema_series(closes1, 7)
    ema26_1h = ema_series(closes1, 26)
    wall_1h = ema_series(closes1, 99)
    adx1_series = _adx_1h(bars1h)
    exec_timestamps = [_utc(v) for v in bars30["timestamp"].to_list()]
    ema7_30 = _map_completed(bars1h, ema7_1h, exec_timestamps)
    ema26_30 = _map_completed(bars1h, ema26_1h, exec_timestamps)
    wall_30 = _map_completed(bars1h, wall_1h, exec_timestamps)
    adx_30 = _map_completed(bars1h, adx1_series, exec_timestamps)

    last = len(closes30) - 1
    if (atr30[last] is None or atr30[last] <= 0 or rsi30[last] is None
            or rsi30[last - 1] is None or rsi_ma3[last] is None
            or rsi_ma3[last - 1] is None or volume_ratio[last] is None
            or wall_30[last] is None or ema7_30[last] is None
            or ema26_30[last] is None or adx_30[last] is None):
        return None
    close = closes30[last]
    wall = wall_30[last]
    if wall <= 0:
        return None
    near_wall = abs(close - wall) / wall < config.TREND_WALL_V5_WALL_PROXIMITY
    setup_long = (close > wall and near_wall and lows30[last] <= wall
                  and ema7_30[last] > ema26_30[last] and adx_30[last] > config.TREND_WALL_V5_ADX_MIN)
    setup_short = (close < wall and near_wall and highs30[last] >= wall
                   and ema7_30[last] < ema26_30[last] and adx_30[last] > config.TREND_WALL_V5_ADX_MIN)
    rsi_up = (rsi30[last] < 40.0 and rsi30[last] > rsi_ma3[last]
              and rsi30[last - 1] <= rsi_ma3[last - 1])
    rsi_down = (rsi30[last] > 60.0 and rsi30[last] < rsi_ma3[last]
                and rsi30[last - 1] >= rsi_ma3[last - 1])
    confirmation_long = setup_long and rsi_up and volume_ratio[last] > 0.5
    confirmation_short = setup_short and rsi_down and volume_ratio[last] > 0.5
    if not (confirmation_long or confirmation_short):
        return None

    direction = "long" if confirmation_long else "short"
    atr = atr30[last]
    # Backtest stop: confirmation-candle high if below wall else wall, minus
    # 1*ATR16 (long); mirrored for short. Entry fills next 30m open.
    if direction == "long":
        stop_base = highs30[last] if highs30[last] < wall else wall
        stop = stop_base - config.TREND_WALL_V5_ATR_STOP_MULTIPLIER * atr
    else:
        stop_base = lows30[last] if lows30[last] > wall else wall
        stop = stop_base + config.TREND_WALL_V5_ATR_STOP_MULTIPLIER * atr
    entry_ref = close
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
        "setup_class": "trend_wall_v5",
        "phase": "wall_reclaim_confirmation",
        "observed_at": observed.isoformat(),
        "valid_until": (observed + timedelta(minutes=config.TREND_WALL_V5_ENTRY_VALIDITY_MINUTES)).isoformat(),
        "horizon_minutes": config.TREND_WALL_V5_ENTRY_VALIDITY_MINUTES,
        "confidence": 0.5,
        "confidence_status": "uncalibrated",
        "entry_condition": {"type": "market_next_bar_open", "price": entry_ref},
        "entry_price": entry_ref,
        "invalidation_price": stop,
        "targets": [],
        "metadata": {
            "execution_timeframe": "30m",
            "structure_timeframe": "1h",
            "entry_timing": "next_completed_30m_open",
            "stop_policy": "confirmation_extreme_or_wall_minus_1atr16",
            "target_policy": "none_structure_exit_only",
            "strategy_exits": {
                "structure_exit": (
                    f"30m close {'<' if direction == 'long' else '>'} wall - 0.5*ATR16 "
                    f"(long) / wall + 0.5*ATR16 (short), applied next completed bar"
                ),
            },
            "bracket_spec": {
                "version": "trend_wall_v5",
                "stop": stop,
                "structure_exit": (
                    f"wall {'-' if direction == 'long' else '+'} "
                    f"{config.TREND_WALL_V5_ATR_EXIT_MULTIPLIER}*ATR16 on completed 30m close"
                ),
            },
        },
        "feature_snapshot": {
            "source_symbol": symbol,
            "execution_timeframe": "30m",
            "wall_1h": wall,
            "ema7_1h": ema7_30[last],
            "ema26_1h": ema26_30[last],
            "adx14_1h": adx_30[last],
            "rsi14_30m": rsi30[last],
            "rsi_ma3_30m": rsi_ma3[last],
            "volume_ratio_30m": volume_ratio[last],
            "atr16_30m": atr,
            "wall_proximity": abs(close - wall) / wall,
            "stop": stop,
            "cutoff": cutoff.isoformat(),
        },
    }


def evaluate_exit(bars30, bars1h, *, side: str, cutoff: datetime) -> dict | None:
    """Structure exit: long close < wall - 0.5*ATR16; short mirrored."""
    cutoff = _utc(cutoff)
    bars30 = bars30.filter(bars30["timestamp"] <= cutoff).sort("timestamp")
    bars1h = bars1h.filter(bars1h["timestamp"] <= cutoff).sort("timestamp")
    if bars30.is_empty() or bars1h.is_empty() or bars30.height < 5:
        return None
    from polars_indicators import wilder_atr_series

    closes30 = [float(v) for v in bars30["close"].to_list()]
    atr30 = wilder_atr_series(bars30, config.TREND_WALL_V5_ATR_LENGTH).to_list()
    closes1 = [float(v) for v in bars1h["close"].to_list()]
    wall_1h = ema_series(closes1, 99)
    wall_30 = _map_completed(bars1h, wall_1h, [_utc(v) for v in bars30["timestamp"].to_list()])
    last = len(closes30) - 1
    if atr30[last] is None or wall_30[last] is None or atr30[last] <= 0:
        return None
    close = closes30[last]
    wall = wall_30[last]
    if side == "long" and close < wall - config.TREND_WALL_V5_ATR_EXIT_MULTIPLIER * atr30[last]:
        return {"action": "exit", "side": side, "rule_name": "wall_structure_exit",
                "cutoff": cutoff.isoformat(),
                "inputs": {"close_30m": close, "wall_1h": wall, "atr16_30m": atr30[last]}}
    if side == "short" and close > wall + config.TREND_WALL_V5_ATR_EXIT_MULTIPLIER * atr30[last]:
        return {"action": "exit", "side": side, "rule_name": "wall_structure_exit",
                "cutoff": cutoff.isoformat(),
                "inputs": {"close_30m": close, "wall_1h": wall, "atr16_30m": atr30[last]}}
    return None


def _rolling_mean_unused(values: list[float], period: int) -> list[float | None]:
    series = pl.Series("v", values, dtype=pl.Float64)
    return series.rolling_mean(period, min_samples=period).to_list()


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
