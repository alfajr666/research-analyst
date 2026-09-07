"""Pure, continuous regime scoring for historical and shadow evaluation."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import config
from polars_indicators import dmi_adx, realized_volatility


REGIME_SCORE_VERSION = "regime-score-v3"
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
    series, _plus_di, _minus_di = dmi_adx(bars, length, smoothing)
    return series


def _realized_volatility(bars: Any, window: int) -> tuple[float | None, float | None]:
    return realized_volatility(bars, window)


def _source_observation_ids(*bars_frames: Any, limit: int | None = None) -> list[str]:
    identifiers = []
    seen = set()
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
                for item in value:
                    item = str(item)
                    if item and item not in seen:
                        seen.add(item)
                        identifiers.append(item)
            elif value:
                value = str(value)
                if value and value not in seen:
                    seen.add(value)
                    identifiers.append(value)
    return identifiers[-limit:] if limit is not None and limit > 0 else identifiers


def _provenance_tail(bars: Any, limit: int) -> Any:
    """Keep provenance proportional to the observations used by the score."""
    if bars is None or limit <= 0:
        return bars
    if hasattr(bars, "tail"):
        return bars.tail(limit)
    if isinstance(bars, dict):
        return {
            key: values[-limit:] if isinstance(values, list) else values
            for key, values in bars.items()
        }
    if isinstance(bars, list):
        return bars[-limit:]
    return bars


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


def regime_score_for_asset(
    conn: Any,
    asset: str,
    cutoff: Any,
    *,
    regime_conn: Any | None = None,
    history_fetcher: Any | None = None,
    history_1h_fetcher: Any | None = None,
) -> dict[str, Any]:
    """Score one asset from market volatility and regime-owned 1h/4h history."""
    from strategy_v2_context import load_bars_for_interval
    from regime_history import (
        ensure_asset_1h_ready,
        ensure_asset_ready,
        load_regime_1h_bars,
        load_regime_4h_bars,
    )

    bars_vol = load_bars_for_interval(conn, asset, "5m", cutoff)
    if regime_conn is None:
        history_1h = {"status": "retryable", "reason": "regime_history_connection_missing"}
        history_4h = {"status": "retryable", "reason": "regime_history_connection_missing"}
        bars_1h = {}
        bars_4h = None
    else:
        history_1h = ensure_asset_1h_ready(
            regime_conn, asset, cutoff, fetcher=history_1h_fetcher
        )
        history_4h = ensure_asset_ready(
            regime_conn, asset, cutoff, fetcher=history_fetcher
        )
        bars_1h = (
            load_regime_1h_bars(regime_conn, asset, cutoff)
            if history_1h["status"] == "ready" else {}
        )
        bars_4h = (
            load_regime_4h_bars(regime_conn, asset, cutoff)
            if history_4h["status"] == "ready" else None
        )
    history = {
        "status": "ready" if history_1h["status"] == history_4h["status"] == "ready" else "retryable",
        "1h": history_1h,
        "4h": history_4h,
    }
    if regime_conn is None:
        history["reason"] = "regime_history_connection_missing"
    market_data = market_data_from_bars(bars_1h, bars_4h, bars_vol)
    result = regime_score(cutoff, market_data)
    result["asset"] = str(asset).upper()
    result["market_data"] = market_data
    result["market_5m_bars"] = (
        int(bars_vol.height) if hasattr(bars_vol, "height") else len(bars_vol or [])
    )
    result["regime_history"] = history
    result.setdefault("components", {})["regime_history"] = history
    vol_provenance = _provenance_tail(
        bars_vol,
        2 * int(getattr(config, "REGIME_SCORE_VOL_WINDOW_BARS", 12)) + 1,
    )
    provenance_limit = int(getattr(config, "REGIME_PROVENANCE_MAX_IDS", 128))
    result["source_observation_ids"] = _source_observation_ids(
        vol_provenance, bars_1h, limit=provenance_limit
    )
    result["source_references"] = {
        "market_5m_volatility_ids": _source_observation_ids(
            vol_provenance, limit=provenance_limit
        ),
        "regime_1h_bar_ids": (
            [str(value) for value in bars_1h["bar_id"].to_list()][-provenance_limit:]
            if hasattr(bars_1h, "columns") and "bar_id" in bars_1h.columns else []
        ),
        "regime_4h_bar_ids": (
            [str(value) for value in bars_4h["bar_id"].to_list()][-provenance_limit:]
            if bars_4h is not None and "bar_id" in bars_4h.columns else []
        ),
    }
    from reversal_gate import reversal_gate

    adx_values = _adx_series(
        bars_1h,
        int(getattr(config, "REGIME_SCORE_ADX_LENGTH", 14)),
        int(getattr(config, "REGIME_SCORE_ADX_SMOOTHING", 14)),
    )
    result["reversal_gate"] = reversal_gate(asset, cutoff, bars_1h, adx_values)
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
