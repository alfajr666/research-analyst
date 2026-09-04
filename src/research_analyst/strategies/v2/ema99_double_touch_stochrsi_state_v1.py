"""5m EMA99 double-touch strategy with true 1m trigger state."""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from strategy_v2_context import (
    cutoff_from_id,
    ema_series,
    evaluation_symbols,
    has_active_event,
    load_bars_for_interval,
    stoch_rsi,
    wilder_atr,
    wilder_rsi,
)
from strategies.v2.dual_zone_follower_v2 import _dmi_adx


STRATEGY_ID = config.EMA99_DOUBLE_TOUCH_STRATEGY_ID
PLUGIN_VERSION = "v1"


def _stoch_values(values: list[float]):
    return stoch_rsi(
        values,
        config.EMA99_DOUBLE_TOUCH_STOCH_RSI_LENGTH,
        config.EMA99_DOUBLE_TOUCH_STOCH_LENGTH,
        config.EMA99_DOUBLE_TOUCH_K_LENGTH,
        config.EMA99_DOUBLE_TOUCH_D_LENGTH,
    )


def _rsi_series(values: list[float]):
    return wilder_rsi(values, config.EMA99_DOUBLE_TOUCH_RSI1_LENGTH)


def _rsi5_series(values: list[float]):
    return wilder_rsi(values, config.EMA99_DOUBLE_TOUCH_RSI5_LENGTH)


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _at_cutoff(bars, cutoff: datetime):
    if bars.is_empty():
        return bars
    return bars.filter(bars["timestamp"] <= _utc(cutoff)).sort("timestamp")


def _fresh_completed(bars, cutoff: datetime, max_age_seconds: float) -> bool:
    if bars.is_empty():
        return False
    latest = _utc(bars["timestamp"][-1])
    age = (_utc(cutoff) - latest).total_seconds()
    return 0 <= age <= max_age_seconds


def _crossed_up(k, d, index: int | None = None) -> bool:
    index = len(k) - 1 if index is None else index
    if index < 1 or index >= len(k) or index >= len(d):
        return False
    values = (k[index - 1], d[index - 1], k[index], d[index])
    return all(value is not None for value in values) and k[index - 1] <= d[index - 1] and k[index] > d[index]


def _crossed_down(k, d, index: int | None = None) -> bool:
    index = len(k) - 1 if index is None else index
    if index < 1 or index >= len(k) or index >= len(d):
        return False
    values = (k[index - 1], d[index - 1], k[index], d[index])
    return all(value is not None for value in values) and k[index - 1] >= d[index - 1] and k[index] < d[index]


def _adx_series(bars1h):
    """Return confirmed ADX values aligned to each completed 1H bar."""
    values = [None] * bars1h.height
    length = config.EMA99_DOUBLE_TOUCH_ADX_LENGTH
    for index in range(length * 2 + length, bars1h.height):
        dmi = _dmi_adx(bars1h[: index + 1], length, length)
        values[index] = dmi[0] if dmi is not None else None
    return values


def _replay_touch_state(bars1m, adx_by_bar):
    """Replay distinct touch state using only bars whose ADX gate passes."""
    closes = [float(value) for value in bars1m["close"].to_list()]
    highs = [float(value) for value in bars1m["high"].to_list()]
    lows = [float(value) for value in bars1m["low"].to_list()]
    emas = ema_series(closes, config.EMA99_DOUBLE_TOUCH_EMA_LENGTH)
    state = {
        "short_touch1": False, "short_touch2": False, "short_first_high": None,
        "short_first_index": None, "long_touch1": False, "long_touch2": False,
        "long_first_low": None, "long_first_index": None,
    }
    for index, ema in enumerate(emas):
        if ema is None or index >= len(adx_by_bar) or adx_by_bar[index] is None:
            continue
        if adx_by_bar[index] < config.EMA99_DOUBLE_TOUCH_ADX_MIN:
            state.update({
                "short_touch1": False, "short_touch2": False, "short_first_high": None,
                "short_first_index": None, "long_touch1": False, "long_touch2": False,
                "long_first_low": None, "long_first_index": None,
            })
            continue
        near = abs(closes[index] - ema) / ema * 100 <= config.EMA99_DOUBLE_TOUCH_PROXIMITY_PCT if ema else False
        short_touch = highs[index] >= ema or near
        long_touch = lows[index] <= ema or near
        if not state["short_touch1"] and short_touch and closes[index] <= ema:
            state["short_touch1"] = True
            state["short_first_high"] = highs[index]
            state["short_first_index"] = index
        if state["short_touch1"] and closes[index] > ema:
            state.update({"short_touch1": False, "short_touch2": False, "short_first_high": None, "short_first_index": None})
        if (state["short_touch1"] and not state["short_touch2"]
                and index > state["short_first_index"] and short_touch and closes[index] <= ema):
            state["short_touch2"] = True

        if not state["long_touch1"] and long_touch and closes[index] >= ema:
            state["long_touch1"] = True
            state["long_first_low"] = lows[index]
            state["long_first_index"] = index
        if state["long_touch1"] and closes[index] < ema:
            state.update({"long_touch1": False, "long_touch2": False, "long_first_low": None, "long_first_index": None})
        if (state["long_touch1"] and not state["long_touch2"]
                and index > state["long_first_index"] and long_touch and closes[index] >= ema):
            state["long_touch2"] = True
    return state


def _recent_cross(fast, slow, direction: str) -> bool:
    cross_index = None
    for index in range(1, len(fast)):
        crossed = (_crossed_up(fast, slow, index) if direction == "long"
                   else _crossed_down(fast, slow, index))
        if crossed:
            cross_index = index
    return cross_index is not None and len(fast) - 1 - cross_index <= config.EMA99_DOUBLE_TOUCH_CROSS_LOOKBACK


def evaluate_symbol(bars1m, bars5m, bars1h, *, asset: str, symbol: str,
                    cutoff: datetime) -> dict | None:
    """Evaluate true 1M state at one completed 5M execution cutoff."""
    cutoff = _utc(cutoff)
    bars1m = _at_cutoff(bars1m, cutoff)
    bars5m = _at_cutoff(bars5m, cutoff)
    bars1h = _at_cutoff(bars1h, cutoff)
    if (bars1m.is_empty() or bars5m.is_empty() or bars1h.is_empty()
            or not _fresh_completed(bars1m, cutoff, config.DATA_FRESHNESS_MAX_SECONDS)
            or not _fresh_completed(bars5m, cutoff, 5 * 60 + config.DATA_FRESHNESS_MAX_SECONDS)
            or not _fresh_completed(bars1h, cutoff, 60 * 60 + config.DATA_FRESHNESS_MAX_SECONDS)):
        return None

    dmi = _dmi_adx(
        bars1h, config.EMA99_DOUBLE_TOUCH_ADX_LENGTH,
        config.EMA99_DOUBLE_TOUCH_ADX_LENGTH,
    )
    if dmi is None or dmi[0] < config.EMA99_DOUBLE_TOUCH_ADX_MIN:
        return None

    closes1m = [float(value) for value in bars1m["close"].to_list()]
    raw1m, k1m, d1m = _stoch_values(closes1m)
    rsi1m = _rsi_series(closes1m)
    if (len(k1m) < 2 or len(d1m) < 2 or rsi1m[-1] is None
            or any(value is None for value in (k1m[-2], d1m[-2], k1m[-1], d1m[-1]))):
        return None

    state = _replay_touch_state(bars1m, _expand_adx_to_1m(bars1m, bars1h))
    fast = ema_series([float(value) for value in bars5m["close"].to_list()], config.EMA99_DOUBLE_TOUCH_FAST_EMA)
    slow = ema_series([float(value) for value in bars5m["close"].to_list()], config.EMA99_DOUBLE_TOUCH_SLOW_EMA)
    closes5m = [float(value) for value in bars5m["close"].to_list()]
    rsi5m = _rsi5_series(closes5m)
    atr5m = wilder_atr(bars5m, config.EMA99_DOUBLE_TOUCH_ATR_LENGTH)
    if (fast[-1] is None or slow[-1] is None or rsi5m[-1] is None
            or atr5m is None or atr5m <= 0):
        return None

    long_signal = (state["long_touch2"] and k1m[-1] <= config.EMA99_DOUBLE_TOUCH_OVERSOLD
                   and _crossed_up(k1m, d1m) and config.EMA99_DOUBLE_TOUCH_RSI1_MIN <= rsi1m[-1] <= config.EMA99_DOUBLE_TOUCH_RSI1_MAX
                   and _recent_cross(fast, slow, "long"))
    short_signal = (state["short_touch2"] and k1m[-1] >= config.EMA99_DOUBLE_TOUCH_OVERBOUGHT
                    and _crossed_down(k1m, d1m) and config.EMA99_DOUBLE_TOUCH_RSI1_MIN <= rsi1m[-1] <= config.EMA99_DOUBLE_TOUCH_RSI1_MAX
                    and _recent_cross(fast, slow, "short"))
    if not (long_signal or short_signal):
        return None

    direction = "long" if long_signal else "short"
    entry = closes5m[-1]
    stop = (state["long_first_low"] - atr5m * config.EMA99_DOUBLE_TOUCH_ATR_MULTIPLIER
            if direction == "long" else
            state["short_first_high"] + atr5m * config.EMA99_DOUBLE_TOUCH_ATR_MULTIPLIER)
    if entry <= 0 or stop <= 0 or (direction == "long" and stop >= entry) or (direction == "short" and stop <= entry):
        return None
    observed_at = _utc(bars5m["timestamp"][-1])
    return {
        "schema_version": 1, "strategy_id": STRATEGY_ID, "plugin_version": PLUGIN_VERSION,
        "asset": asset.upper(), "direction": direction,
        "setup_class": "ema99_double_touch_stochrsi_state",
        "phase": "double_touch_long" if direction == "long" else "double_touch_short",
        "observed_at": observed_at.isoformat(),
        "valid_until": (observed_at + timedelta(minutes=config.EMA99_DOUBLE_TOUCH_ENTRY_VALIDITY_MINUTES)).isoformat(),
        "horizon_minutes": config.EMA99_DOUBLE_TOUCH_ENTRY_VALIDITY_MINUTES,
         "confidence": 0.5, "confidence_status": "uncalibrated",
         "entry_condition": {"type": "market", "price": entry}, "entry_price": entry,
         "invalidation_price": stop,
         "targets": [],
         "metadata": {
            "execution_timeframe": "5m", "trigger_timeframe": "1m", "trend_timeframe": "1h",
            "target_policy": "executor_derived_2r", "stop_policy": "first_touch_extreme_plus_atr",
            "strategy_exits": {
                "long": "5m RSI > 70 and close >= EMA26 * 1.03",
                "short": "5m RSI < 30 and close <= EMA26 * 0.97",
            },
        },
        "feature_snapshot": {
            "source_symbol": symbol, "adx_1h": dmi[0], "+di_1h": dmi[1], "-di_1h": dmi[2],
            "rsi_1m": rsi1m[-1], "stochrsi_raw_1m": raw1m[-1],
            "stochrsi_k_1m": k1m[-1], "stochrsi_d_1m": d1m[-1],
            "ema99_1m": ema_series(closes1m, config.EMA99_DOUBLE_TOUCH_EMA_LENGTH)[-1],
            "ema7_5m": fast[-1], "ema26_5m": slow[-1], "rsi14_5m": rsi5m[-1],
            "atr14_5m": atr5m, "long_touch2": state["long_touch2"],
            "short_touch2": state["short_touch2"], "cutoff": cutoff.isoformat(),
        },
    }


def _expand_adx_to_1m(bars1m, bars1h):
    adx_values = _adx_series(bars1h)
    timestamps = [_utc(value) for value in bars1h["timestamp"].to_list()]
    return [adx_values[bisect_right(timestamps, _utc(value)) - 1]
            if bisect_right(timestamps, _utc(value)) else None
            for value in bars1m["timestamp"].to_list()]


def evaluate_exit(bars5m, *, side: str, cutoff: datetime) -> dict | None:
    """Evaluate the completed-5M RSI/EMA26 strategy exit."""
    cutoff = _utc(cutoff)
    bars5m = _at_cutoff(bars5m, cutoff)
    if not _fresh_completed(bars5m, cutoff, 5 * 60 + config.DATA_FRESHNESS_MAX_SECONDS):
        return None
    closes = [float(value) for value in bars5m["close"].to_list()]
    slow = ema_series(closes, config.EMA99_DOUBLE_TOUCH_SLOW_EMA)
    rsi = _rsi5_series(closes)
    if slow[-1] is None or rsi[-1] is None:
        return None
    long_exit = side == "long" and rsi[-1] > config.EMA99_DOUBLE_TOUCH_LONG_TP_RSI and closes[-1] >= slow[-1] * (1 + config.EMA99_DOUBLE_TOUCH_TP_EMA_PCT / 100)
    short_exit = side == "short" and rsi[-1] < config.EMA99_DOUBLE_TOUCH_SHORT_TP_RSI and closes[-1] <= slow[-1] * (1 - config.EMA99_DOUBLE_TOUCH_TP_EMA_PCT / 100)
    if not (long_exit or short_exit):
        return None
    return {"action": "exit", "side": side, "rule_name": f"{side}_ema26_rsi_exit", "cutoff": cutoff.isoformat(),
            "inputs": {"close_5m": closes[-1], "ema26_5m": slow[-1], "rsi5m": rsi[-1]}}


def run_plugin(cutoff_id: str, snapshot: dict) -> list[dict]:
    cutoff = cutoff_from_id(str(snapshot.get("cutoff_at") or cutoff_id), snapshot.get("now"))
    conn = config.get_db_connection(read_only=True, db_path=snapshot.get("market_db_path"))
    try:
        events = []
        for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
            event = evaluate_symbol(
                load_bars_for_interval(conn, symbol, "1m", cutoff),
                load_bars_for_interval(conn, symbol, "5m", cutoff),
                load_bars_for_interval(conn, symbol, "1h", cutoff),
                asset=asset, symbol=symbol, cutoff=cutoff,
            )
            if event and not has_active_event(STRATEGY_ID, asset, event["direction"], now=cutoff):
                event["input_snapshot_id"] = cutoff_id
                events.append(event)
        return events
    finally:
        conn.close()
