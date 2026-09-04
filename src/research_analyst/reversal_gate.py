"""Pure completed-1h reversal-family activation gate."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from strategy_v2_context import wilder_rsi


REVERSAL_GATE_VERSION = "reversal-gate-v1"
RSI_LENGTH = 14
FRACTAL_RADIUS = 2
DIVERGENCE_LOOKBACK = 48
ADX_RECENT_LOOKBACK = 20
ADX_RECENT_THRESHOLD = 25.0
ADX_DECAY_LOOKBACK = 5


def _column_values(bars: Any, column: str) -> list[Any]:
    if hasattr(bars, "get_column"):
        return bars.get_column(column).to_list() if column in bars.columns else []
    if isinstance(bars, dict):
        return list(bars.get(column, []))
    return [row.get(column) for row in bars if isinstance(row, dict)]


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _pivot_id(bars: Any, index: int) -> str | int:
    timestamps = _column_values(bars, "timestamp")
    return str(timestamps[index]) if index < len(timestamps) and timestamps[index] is not None else index


def _source_ids(bars: Any, index: int) -> list[str]:
    values = _column_values(bars, "source_observation_ids")
    if index < 0 or index >= len(values) or values[index] is None:
        return []
    value = values[index]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return [str(value)]


def _pivots(bars: Any, rsi: Sequence[float | None]) -> tuple[list[int], list[int]]:
    highs = _column_values(bars, "high")
    lows = _column_values(bars, "low")
    high_pivots = []
    low_pivots = []
    for index in range(FRACTAL_RADIUS, min(len(highs), len(lows), len(rsi)) - FRACTAL_RADIUS):
        if not _finite(rsi[index]) or not _finite(highs[index]) or not _finite(lows[index]):
            continue
        before_high = highs[index - FRACTAL_RADIUS:index]
        after_high = highs[index + 1:index + FRACTAL_RADIUS + 1]
        before_low = lows[index - FRACTAL_RADIUS:index]
        after_low = lows[index + 1:index + FRACTAL_RADIUS + 1]
        if all(float(highs[index]) > float(value) for value in (*before_high, *after_high)):
            high_pivots.append(index)
        if all(float(lows[index]) < float(value) for value in (*before_low, *after_low)):
            low_pivots.append(index)
    return high_pivots, low_pivots


def _latest_divergence(bars: Any, rsi: Sequence[float | None]) -> dict[str, Any]:
    highs, lows = _pivots(bars, rsi)
    prices_high = _column_values(bars, "high")
    prices_low = _column_values(bars, "low")
    start = max(0, len(rsi) - DIVERGENCE_LOOKBACK)
    candidates = []
    for pivots, prices, kind, condition in (
        (highs, prices_high, "regular_bearish", lambda old, new, old_r, new_r: new > old and new_r < old_r),
        (lows, prices_low, "regular_bullish", lambda old, new, old_r, new_r: new < old and new_r > old_r),
    ):
        recent = [index for index in pivots if index >= start]
        for older_position, older in enumerate(recent[:-1]):
            for newer in recent[older_position + 1:]:
                if condition(float(prices[older]), float(prices[newer]), float(rsi[older]), float(rsi[newer])):
                    candidates.append((newer, older, kind))
    if not candidates:
        return {"type": "none", "direction": "none", "pivots": [], "indices": []}
    kinds = {candidate[2] for candidate in candidates}
    if len(kinds) > 1:
        return {
            "type": "ambiguous",
            "direction": "none",
            "pivots": [],
            "indices": [],
        }
    newer, older, kind = max(candidates)
    return {
        "type": kind,
        "direction": "short" if kind == "regular_bearish" else "long",
        "pivots": [_pivot_id(bars, older), _pivot_id(bars, newer)],
        "indices": [older, newer],
        "price_values": [
            float((prices_high if kind == "regular_bearish" else prices_low)[older]),
            float((prices_high if kind == "regular_bearish" else prices_low)[newer]),
        ],
        "rsi_values": [float(rsi[older]), float(rsi[newer])],
        "source_ids": sorted(set(_source_ids(bars, older) + _source_ids(bars, newer))),
    }


def _ols_slope(values: Sequence[float]) -> float | None:
    if len(values) < 2 or not all(_finite(value) for value in values):
        return None
    x_mean = (len(values) - 1) / 2
    y_mean = sum(float(value) for value in values) / len(values)
    denominator = sum((index - x_mean) ** 2 for index in range(len(values)))
    return sum((index - x_mean) * (float(value) - y_mean) for index, value in enumerate(values)) / denominator


def reversal_gate(
    asset: str,
    cutoff: Any,
    bars_1h: Any,
    adx_1h: Sequence[float | None],
    rsi_1h: Sequence[float | None] | None = None,
) -> dict[str, Any]:
    """Return deterministic reversal activation from completed 1h evidence."""
    closes = _column_values(bars_1h, "close")
    rsi = list(rsi_1h) if rsi_1h is not None else wilder_rsi([float(value) for value in closes], RSI_LENGTH)
    adx = list(adx_1h)
    timestamps = _column_values(bars_1h, "timestamp")
    cutoff_time = None
    try:
        cutoff_time = cutoff if isinstance(cutoff, datetime) else datetime.fromisoformat(
            str(cutoff).replace("Z", "+00:00")
        )
        cutoff_time = (cutoff_time if cutoff_time.tzinfo else cutoff_time.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass
    data_reasons = []
    if not timestamps:
        data_reasons.append("missing_1h_data")
    if not any(_finite(value) for value in rsi):
        data_reasons.append("missing_rsi_1h")
    if not any(_finite(value) for value in adx):
        data_reasons.append("missing_adx_1h")
    parsed_timestamps = []
    for value in timestamps:
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            parsed_timestamps.append((parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc))
        except (TypeError, ValueError, OverflowError):
            parsed_timestamps = []
            break
    if timestamps and not parsed_timestamps:
        data_reasons.append("malformed_1h_data")
    if cutoff_time and any(value > cutoff_time for value in parsed_timestamps):
        data_reasons.append("future_1h_data")
    if parsed_timestamps and cutoff_time and cutoff_time - parsed_timestamps[-1] > timedelta(hours=2):
        data_reasons.append("stale_1h_data")
    if len(parsed_timestamps) > 1 and any(
        right - left != timedelta(hours=1)
        for left, right in zip(parsed_timestamps, parsed_timestamps[1:])
    ):
        data_reasons.append("missing_1h_data")
    divergence = _latest_divergence(bars_1h, rsi)
    recent_values = adx[-ADX_RECENT_LOOKBACK:]
    recent = (
        len(recent_values) == ADX_RECENT_LOOKBACK
        and all(_finite(value) for value in recent_values)
        and max(float(value) for value in recent_values) >= ADX_RECENT_THRESHOLD
    )
    decay_values = adx[-ADX_DECAY_LOOKBACK:]
    slope = _ols_slope(decay_values) if len(decay_values) == ADX_DECAY_LOOKBACK else None
    decay = slope is not None and slope < 0
    reasons = []
    if divergence["type"] == "none":
        reasons.append("regular_divergence_missing")
    elif divergence["type"] == "ambiguous":
        reasons.append("ambiguous_divergence")
    if not recent:
        reasons.append("recent_trend_adx_missing")
    if not decay:
        reasons.append("adx_decay_missing")
    reasons = data_reasons + reasons
    active = not reasons
    recent_max = max((float(value) for value in recent_values if _finite(value)), default=None)
    return {
        "asset": str(asset).upper(),
        "cutoff_at": str(cutoff),
        "active": active,
        "direction": divergence["direction"] if active else "none",
        "divergence_type": divergence["type"],
        "divergence_detected": divergence["type"] != "none",
        "recent_trend_detected": recent,
        "adx_decay_detected": decay,
        "adx_slope": slope,
        "pivot_ids": divergence["pivots"],
        "price_pivot_ids": divergence["pivots"],
        "rsi_pivot_ids": divergence["pivots"],
        "price_pivot_values": divergence.get("price_values", []),
        "rsi_pivot_values": divergence.get("rsi_values", []),
        "working_timeframe": "1h",
        "rsi_period": RSI_LENGTH,
        "fractal_width": 5,
        "lookback_bars": DIVERGENCE_LOOKBACK,
        "adx_length": 14,
        "adx_smoothing": 14,
        "adx_recent_threshold": ADX_RECENT_THRESHOLD,
        "adx_recent_lookback": ADX_RECENT_LOOKBACK,
        "adx_decay_lookback": ADX_DECAY_LOOKBACK,
        "adx_recent_max": recent_max,
        "adx_decay_slope": slope,
        "source_observation_ids": divergence.get("source_ids", _source_ids(bars_1h, len(timestamps) - 1)),
        "reasons": reasons,
        "gate_version": REVERSAL_GATE_VERSION,
    }
