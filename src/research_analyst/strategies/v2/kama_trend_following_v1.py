"""KAMA trend-following port: 30m execution with completed-4h KAMA filter.

Authority: repo-final/vectorbt/strategies/kama_trend_following.py
(see engine_handoff/all_families_engine_specs.md section 3 — watchlist,
insufficient sample, ported per operator decision).

Self-contained plugin: ADX/Choppiness/BB-width use the repository's native
engines where they exist (``dmi_adx_series``, ``build_feature_frame``); the
Jesse-Rust KAMA recurrence and the 30m/4h groupers are private to this
strategy because no native kernel exists.

Entry (long): close > KAMA(14,2,30) AND ADX14 > 50 AND close > KAMA(4h
completed) AND Choppiness(14) < 50 AND BB-width%(20, 2.0) < 7.0. Short
mirrored. Cooldown: no new entry within 10 bars of the last closed trade —
replayed conservatively from completed bars. Exits: symmetric 2.5*ATR14
bracket, intrabar, gap-pessimistic, stop-before-target. The backtest ×3
leverage multiplier is NOT reproduced (handoff cross-family note 5).
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
    evaluation_symbols,
    has_active_event,
    load_bars_for_interval,
    strategy_market_connection,
)

STRATEGY_ID = config.KAMA_TREND_STRATEGY_ID
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
    """Group completed end-stamped bars into exact UTC-aligned buckets."""
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


def _kama_series(values: list[float], period: int, fast_length: int,
                 slow_length: int) -> list[float | None]:
    """Jesse-Rust KAMA recurrence including its trailing volatility window.

    The authority returns raw source values for indices <= period; those bars
    carry no valid KAMA and are exposed as None here (signals require finite
    KAMA comparisons, so this only shortens effective history by `period`
    bars and never changes a completed-bar decision). Strategy-private: no
    native RA kernel exists.
    """
    source = [float(v) for v in values]
    n = len(source)
    if n == 0:
        return []
    if n <= period:
        return [None] * n
    fast_alpha = 2.0 / (fast_length + 1.0)
    slow_alpha = 2.0 / (slow_length + 1.0)
    alpha_diff = fast_alpha - slow_alpha
    out: list[float | None] = [None] * period
    result = source[period - 1]
    volatility = float(sum(abs(source[i] - source[i - 1]) for i in range(1, period)))
    for i in range(period, n):
        volatility += abs(source[i] - source[i - 1])
        if i > period:
            volatility -= abs(source[i - period] - source[i - period - 1])
        change = abs(source[i] - source[i - period])
        er = change / volatility if volatility != 0 else 0.0
        smoothing = (er * alpha_diff + slow_alpha) ** 2
        result = result + smoothing * (source[i] - result)
        out.append(result)
    return out


def _choppiness_series(bars: pl.DataFrame, period: int) -> list[float | None]:
    """Choppiness index (strategy-private, no native RA kernel).

    Formula: 100 * log10(sum(TR, period) / (high_max - low_min)) / log10(period).
    """
    if bars.is_empty() or period <= 0 or bars.height < period + 1:
        return [None] * bars.height
    frame = bars.with_columns(
        tr=pl.max_horizontal(
            (pl.col("high") - pl.col("low")),
            (pl.col("high") - pl.col("close").shift(1)).abs(),
            (pl.col("low") - pl.col("close").shift(1)).abs(),
        ).alias("tr")
    ).with_columns(
        tr_sum=pl.col("tr").rolling_sum(period, min_samples=period),
        range_high=pl.col("high").rolling_max(period, min_samples=period),
        range_low=pl.col("low").rolling_min(period, min_samples=period),
    )
    result: list[float | None] = []
    log_period = math.log10(period)
    for tr_sum, high, low in zip(frame["tr_sum"].to_list(),
                                 frame["range_high"].to_list(),
                                 frame["range_low"].to_list()):
        if tr_sum is None or high is None or low is None:
            result.append(None)
            continue
        range_value = high - low
        ratio = tr_sum / range_value if range_value > 0 else None
        if ratio is None or ratio <= 0:
            result.append(None)
            continue
        result.append(100.0 * math.log10(ratio) / log_period)
    return result


def evaluate_symbol(bars5m, bars1h, *, asset: str, symbol: str, cutoff: datetime) -> dict | None:
    cutoff = _utc(cutoff)
    if bars5m.is_empty() or bars1h.is_empty():
        return None
    bars5m = bars5m.filter(bars5m["timestamp"] <= cutoff).sort("timestamp")
    if not _fresh(bars5m, cutoff, 5 * 60 + config.DATA_FRESHNESS_MAX_SECONDS):
        return None
    bars30 = _group_bars(bars5m, 1800, 6)
    if bars30.is_empty() or bars30.height < config.KAMA_TREND_COOLDOWN_BARS + config.KAMA_TREND_KAMA_PERIOD + 2:
        return None
    h4 = _group_bars(bars30, 14400, 8)
    if h4.is_empty():
        return None

    closes = [float(v) for v in bars30["close"].to_list()]
    features30 = build_feature_frame(
        bars30,
        bollinger={"bb": (config.KAMA_TREND_BB_WIDTH_PERIOD, config.KAMA_TREND_BB_WIDTH_MULT)},
    )
    mid = features30["bb_middle"].to_list()
    width = features30["bb_width"].to_list()
    bb_width_pct: list[float | None] = [
        None if (m is None or m == 0 or w is None) else w / abs(m) * 100.0
        for m, w in zip(mid, width)
    ]
    from polars_indicators import dmi_adx_series

    adx30, _, _ = dmi_adx_series(bars30, config.KAMA_TREND_ADX_PERIOD, config.KAMA_TREND_ADX_PERIOD)
    atr30_series = build_feature_frame(bars30, atr={"atr14": config.KAMA_TREND_ATR_PERIOD})["atr14"].to_list()
    chop30 = _choppiness_series(bars30, config.KAMA_TREND_CHOP_PERIOD)

    closes4 = [float(v) for v in h4["close"].to_list()]
    kama30 = _kama_series(closes, config.KAMA_TREND_KAMA_PERIOD,
                          config.KAMA_TREND_KAMA_FAST_LENGTH, config.KAMA_TREND_KAMA_SLOW_LENGTH)
    kama4 = _kama_series(closes4, config.KAMA_TREND_KAMA_PERIOD,
                         config.KAMA_TREND_KAMA_FAST_LENGTH, config.KAMA_TREND_KAMA_SLOW_LENGTH)
    ends4 = [_utc(v) for v in h4["timestamp"].to_list()]
    exec_timestamps = [_utc(v) for v in bars30["timestamp"].to_list()]
    long_term_kama = [
        kama4[bisect_right(ends4, stamp) - 1] if bisect_right(ends4, stamp) - 1 >= 0 else None
        for stamp in exec_timestamps
    ]

    last = len(closes) - 1
    if (last < 1 or kama30[last] is None or atr30_series[last] is None
            or last >= len(adx30) or adx30[last] is None
            or chop30[last] is None or bb_width_pct[last] is None
            or long_term_kama[last] is None or kama30[last - 1] is None):
        return None
    if not _cooldown_ok(closes, kama30, adx30, chop30, bb_width_pct, long_term_kama,
                        atr30_series, last):
        return None

    close = closes[last]
    long_entry = (
        close > kama30[last]
        and adx30[last] > config.KAMA_TREND_ADX_THRESHOLD
        and close > long_term_kama[last]
        and chop30[last] < config.KAMA_TREND_CHOP_THRESHOLD
        and bb_width_pct[last] < config.KAMA_TREND_BB_WIDTH_THRESHOLD_PCT
    )
    short_entry = (
        close < kama30[last]
        and adx30[last] > config.KAMA_TREND_ADX_THRESHOLD
        and close < long_term_kama[last]
        and chop30[last] < config.KAMA_TREND_CHOP_THRESHOLD
        and bb_width_pct[last] < config.KAMA_TREND_BB_WIDTH_THRESHOLD_PCT
    )
    if not (long_entry or short_entry):
        return None
    direction = "long" if long_entry else "short"
    atr_value = atr30_series[last]
    if not atr_value or atr_value <= 0:
        return None
    stop_distance = config.KAMA_TREND_ATR_STOP_MULT * atr_value
    entry_ref = close
    stop = entry_ref - stop_distance if direction == "long" else entry_ref + stop_distance
    target = entry_ref + config.KAMA_TREND_ATR_TARGET_MULT * atr_value if direction == "long" \
        else entry_ref - config.KAMA_TREND_ATR_TARGET_MULT * atr_value
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
        "setup_class": "kama_trend_following",
        "phase": "kama_trend_alignment",
        "observed_at": observed.isoformat(),
        "valid_until": (observed + timedelta(minutes=config.KAMA_TREND_ENTRY_VALIDITY_MINUTES)).isoformat(),
        "horizon_minutes": config.KAMA_TREND_ENTRY_VALIDITY_MINUTES,
        "confidence": 0.5,
        "confidence_status": "uncalibrated",
        "entry_condition": {"type": "market_on_signal_bar_close", "price": entry_ref},
        "entry_price": entry_ref,
        "invalidation_price": stop,
        "targets": [target],
        "metadata": {
            "execution_timeframe": "30m",
            "filter_timeframe": "4h",
            "entry_timing": "event_execution_on_signal_bar",
            "stop_policy": "symmetric_atr_bracket",
            "target_policy": "symmetric_atr_bracket_1to1",
            "strategy_exits": {
                "bracket_rule": (
                    f"stop = fill -+ {config.KAMA_TREND_ATR_STOP_MULT}*ATR14(30m); "
                    f"target = fill +- {config.KAMA_TREND_ATR_TARGET_MULT}*ATR14(30m); "
                    "intrabar, gap-pessimistic, stop checked before target"
                ),
                "cooldown_bars": config.KAMA_TREND_COOLDOWN_BARS,
            },
            "sizing_note": (
                "backtest 3%-risk x3 multiplier is the backtest leverage assumption; "
                "executor sizing is independent per repository policy"
            ),
        },
        "feature_snapshot": {
            "source_symbol": symbol,
            "execution_timeframe": "30m",
            "kama30": kama30[last],
            "kama_4h_completed": long_term_kama[last],
            "adx14_30m": adx30[last],
            "chop14_30m": chop30[last],
            "bb_width_pct_30m": bb_width_pct[last],
            "atr14_30m": atr_value,
            "stop": stop,
            "target": target,
            "cutoff": cutoff.isoformat(),
        },
    }


def _cooldown_ok(closes, kama30, adx30, chop30, bb_width_pct, long_term_kama,
                 atr30_series, last: int) -> bool:
    """Replay the 10-bar entry cooldown from completed bars.

    The backtest suppresses entries within `cooldown_bars` of the last closed
    trade. Exact closed-trade timing requires portfolio simulation; this replay
    approximates it by locating the most recent prior bar that itself qualified
    for entry (the only bar the backtest could have entered on) and suppressing
    a new entry while that position's bracket could not yet have resolved. This
    is intentionally conservative: only bars within cooldown distance from a
    prior qualifying entry can suppress.
    """
    for prior in range(last - 1, max(-1, last - 1 - config.KAMA_TREND_COOLDOWN_BARS), -1):
        if (kama30[prior] is None or prior >= len(adx30) or adx30[prior] is None
                or chop30[prior] is None or bb_width_pct[prior] is None
                or long_term_kama[prior] is None):
            continue
        close_prior = closes[prior]
        qualified = (
            (close_prior > kama30[prior] and adx30[prior] > config.KAMA_TREND_ADX_THRESHOLD
             and close_prior > long_term_kama[prior]
             and chop30[prior] < config.KAMA_TREND_CHOP_THRESHOLD
             and bb_width_pct[prior] < config.KAMA_TREND_BB_WIDTH_THRESHOLD_PCT)
            or (close_prior < kama30[prior] and adx30[prior] > config.KAMA_TREND_ADX_THRESHOLD
                and close_prior < long_term_kama[prior]
                and chop30[prior] < config.KAMA_TREND_CHOP_THRESHOLD
                and bb_width_pct[prior] < config.KAMA_TREND_BB_WIDTH_THRESHOLD_PCT)
        )
        if not qualified:
            continue
        atr_prior = atr30_series[prior]
        if atr_prior is None or atr_prior <= 0:
            return True
        stop_distance = config.KAMA_TREND_ATR_STOP_MULT * atr_prior
        high_after = max(closes[prior + 1:last + 1])
        low_after = min(closes[prior + 1:last + 1])
        # A 2.5-ATR symmetric bracket: closed if either level was touchable.
        bracket_closed = (low_after <= close_prior - stop_distance
                          or high_after >= close_prior + stop_distance)
        if not bracket_closed:
            return True
        return False
    return True


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
