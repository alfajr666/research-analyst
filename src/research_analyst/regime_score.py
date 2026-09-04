"""Pure, continuous regime scoring for historical and shadow evaluation."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import config


REGIME_SCORE_VERSION = "regime-score-v1"
_REQUIRED_INPUTS = (
    "adx_1h", "adx_4h", "realized_vol_recent", "realized_vol_prior",
)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _utc(value: Any) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except (TypeError, ValueError, OverflowError):
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _transition_discount(current_time: Any) -> float:
    timestamp = _utc(current_time)
    if timestamp is None:
        return 0.0
    center = int(getattr(config, "REGIME_SCORE_TRANSITION_CENTER_UTC_MINUTE", 13 * 60))
    width = int(getattr(config, "REGIME_SCORE_TRANSITION_WIDTH_MINUTES", 60))
    floor = float(getattr(config, "REGIME_SCORE_TRANSITION_MIN_DISCOUNT", 0.5))
    if width <= 0:
        return 1.0
    minute = timestamp.hour * 60 + timestamp.minute + timestamp.second / 60
    distance = abs(minute - center)
    if distance >= width:
        return 1.0
    proximity = 1.0 - distance / width
    return floor + (1.0 - floor) * (1.0 - proximity)


def _missing_inputs(market_data: dict[str, Any]) -> list[str]:
    return [
        key for key in _REQUIRED_INPUTS
        if not _finite(market_data.get(key)) or market_data[key] <= 0
    ]


def _column_values(bars: Any, column: str) -> list[float]:
    if bars is None:
        return []
    if hasattr(bars, "get_column"):
        if column not in getattr(bars, "columns", ()):
            return []
        values = bars.get_column(column).to_list()
    elif isinstance(bars, dict):
        values = bars.get(column, [])
    else:
        values = [row.get(column) for row in bars if isinstance(row, dict)]
    return [float(value) for value in values if _finite(value)]


def _adx_series(bars: Any, length: int, smoothing: int) -> list[float]:
    high = _column_values(bars, "high")
    low = _column_values(bars, "low")
    close = _column_values(bars, "close")
    if min(len(high), len(low), len(close)) < length * 2 + smoothing + 1:
        return []
    true_ranges = []
    plus_moves = []
    minus_moves = []
    for index in range(1, len(close)):
        true_ranges.append(max(
            high[index] - low[index],
            abs(high[index] - close[index - 1]),
            abs(low[index] - close[index - 1]),
        ))
        up = high[index] - high[index - 1]
        down = low[index - 1] - low[index]
        plus_moves.append(up if up > down and up > 0 else 0.0)
        minus_moves.append(down if down > up and down > 0 else 0.0)

    atr = sum(true_ranges[:length])
    plus = sum(plus_moves[:length])
    minus = sum(minus_moves[:length])
    dx = []
    for index in range(length, len(true_ranges)):
        atr = atr - atr / length + true_ranges[index]
        plus = plus - plus / length + plus_moves[index]
        minus = minus - minus / length + minus_moves[index]
        plus_di = 100 * plus / atr if atr else 0.0
        minus_di = 100 * minus / atr if atr else 0.0
        denominator = plus_di + minus_di
        dx.append(100 * abs(plus_di - minus_di) / denominator if denominator else 0.0)
    if len(dx) < smoothing:
        return []
    current = sum(dx[:smoothing]) / smoothing
    series = [current]
    for value in dx[smoothing:]:
        current = (current * (smoothing - 1) + value) / smoothing
        series.append(current)
    return series


def _realized_volatility(bars: Any, window: int) -> tuple[float | None, float | None]:
    closes = _column_values(bars, "close")
    if window <= 0 or len(closes) < window * 2 + 1:
        return None, None
    returns = [math.log(closes[index] / closes[index - 1]) for index in range(1, len(closes))
               if closes[index] > 0 and closes[index - 1] > 0]
    if len(returns) < window * 2:
        return None, None
    prior = returns[-window * 2:-window]
    recent = returns[-window:]
    return math.sqrt(sum(value * value for value in recent)), math.sqrt(sum(value * value for value in prior))


def _source_observation_ids(*bars_frames: Any) -> list[str]:
    identifiers = set()
    for bars in bars_frames:
        if bars is None:
            continue
        if hasattr(bars, "columns") and "source_observation_ids" in bars.columns:
            values = bars.get_column("source_observation_ids").to_list()
        elif isinstance(bars, dict):
            values = bars.get("source_observation_ids", [])
        else:
            values = [row.get("source_observation_ids", []) for row in bars if isinstance(row, dict)]
        for value in values:
            if isinstance(value, (list, tuple, set)):
                identifiers.update(str(item) for item in value if item)
            elif value:
                identifiers.add(str(value))
    return sorted(identifiers)


def market_data_from_bars(bars_1h: Any, bars_4h: Any, bars_vol: Any) -> dict[str, Any]:
    """Build score inputs from one asset's completed bars only."""
    length = int(getattr(config, "REGIME_SCORE_ADX_LENGTH", 14))
    smoothing = int(getattr(config, "REGIME_SCORE_ADX_SMOOTHING", 14))
    adx_1h = _adx_series(bars_1h, length, smoothing)
    adx_4h = _adx_series(bars_4h, length, smoothing)
    recent_vol, prior_vol = _realized_volatility(
        bars_vol, int(getattr(config, "REGIME_SCORE_VOL_WINDOW_BARS", 12))
    )
    return {
        "adx_1h": adx_1h[-1] if adx_1h else None,
        "adx_4h": adx_4h[-1] if adx_4h else None,
        "adx_1h_previous": adx_1h[-2] if len(adx_1h) > 1 else None,
        "adx_4h_previous": adx_4h[-2] if len(adx_4h) > 1 else None,
        "realized_vol_recent": recent_vol,
        "realized_vol_prior": prior_vol,
    }


def regime_score_for_asset(conn: Any, asset: str, cutoff: Any) -> dict[str, Any]:
    """Score a rotated asset using only that asset's completed market bars."""
    from strategy_v2_context import load_bars_for_interval, resample_ohlcv

    bars_vol = load_bars_for_interval(conn, asset, "5m", cutoff)
    bars_1h = resample_ohlcv(bars_vol, "1h")
    bars_4h = resample_ohlcv(bars_vol, "4h")
    market_data = market_data_from_bars(bars_1h, bars_4h, bars_vol)
    result = regime_score(cutoff, market_data)
    result["asset"] = str(asset).upper()
    result["market_data"] = market_data
    result["source_observation_ids"] = _source_observation_ids(bars_vol)
    return result


def regime_score(current_time: Any, market_data: dict[str, Any]) -> dict[str, Any]:
    """Return continuous strategy-family weights for one completed-bar snapshot.

    The parameter defaults are deliberately provisional. This function is a
    research seam only; it does not change strategy admission or sizing.
    """
    missing = _missing_inputs(market_data)
    transition_discount = _transition_discount(current_time)
    if missing or transition_discount <= 0:
        return {
            "regime_score_version": REGIME_SCORE_VERSION,
            "status": "insufficient_data",
            "trend_weight": 0.0,
            "mean_reversion_weight": 0.0,
            "reversal_weight": 0.0,
            "confidence": 0.0,
            "components": {"missing_inputs": missing},
        }

    adx_scale = float(getattr(config, "REGIME_SCORE_ADX_NORMALIZATION", 50.0))
    adx_1h = _clamp(float(market_data["adx_1h"]) / adx_scale)
    adx_4h = _clamp(float(market_data["adx_4h"]) / adx_scale)
    trend_strength = (adx_1h + adx_4h) / 2.0
    tf_agreement = 1.0 - abs(adx_1h - adx_4h)

    recent_vol = float(market_data["realized_vol_recent"])
    prior_vol = float(market_data["realized_vol_prior"])
    vol_ratio = recent_vol / prior_vol
    vol_regime_clarity = _clamp(1.0 / vol_ratio)

    confidence = _clamp(tf_agreement * vol_regime_clarity * transition_discount)

    trend_decay = 0.0
    previous_1h = market_data.get("adx_1h_previous")
    previous_4h = market_data.get("adx_4h_previous")
    if _finite(previous_1h) and _finite(previous_4h):
        previous_strength = (
            _clamp(float(previous_1h) / adx_scale) +
            _clamp(float(previous_4h) / adx_scale)
        ) / 2.0
        trend_decay = max(0.0, previous_strength - trend_strength)
        reversal_activation = float(getattr(config, "REGIME_SCORE_REVERSAL_MIN_PRIOR_TREND", 0.55))
        reversal_decay = float(getattr(config, "REGIME_SCORE_REVERSAL_DECAY_MIN", 0.15))
        reversal_signal = (
            _clamp(trend_decay / reversal_decay)
            if previous_strength >= reversal_activation and reversal_decay > 0 else 0.0
        )
    else:
        reversal_signal = 0.0

    return {
        "regime_score_version": REGIME_SCORE_VERSION,
        "status": "ok",
        "trend_weight": confidence * trend_strength,
        "mean_reversion_weight": confidence * (1.0 - trend_strength),
        "reversal_weight": confidence * reversal_signal,
        "confidence": confidence,
        "components": {
            "trend_strength": trend_strength,
            "tf_agreement": tf_agreement,
            "vol_ratio": vol_ratio,
            "vol_regime_clarity": vol_regime_clarity,
            "transition_discount": transition_discount,
            "trend_decay": trend_decay,
        },
    }
