"""Fundamo EMA99 retest strategy with a confirmed 1H ADX filter."""

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
    wilder_atr,
    wilder_rsi,
)
from strategies.v2.dual_zone_follower_v2 import _dmi_adx


STRATEGY_ID = config.EMA99_RETEST_STRATEGY_ID
PLUGIN_VERSION = "v1"


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


def _adx_series(bars1h):
    values = [None] * bars1h.height
    for index in range(config.EMA99_RETEST_ADX_LENGTH * 2 + config.EMA99_RETEST_ADX_SMOOTHING,
                       bars1h.height):
        dmi = _dmi_adx(
            bars1h[: index + 1],
            config.EMA99_RETEST_ADX_LENGTH,
            config.EMA99_RETEST_ADX_SMOOTHING,
        )
        values[index] = dmi if dmi is not None else None
    return values


def _expand_adx_to_5m(bars5m, bars1h):
    adx_values = _adx_series(bars1h)
    timestamps = [_utc(value) for value in bars1h["timestamp"].to_list()]
    expanded = []
    for value in bars5m["timestamp"].to_list():
        index = bisect_right(timestamps, _utc(value)) - 1
        expanded.append(adx_values[index] if index >= 0 else None)
    return expanded


def _replay_cross_state(bars5m, adx_by_bar, fast=None, slow=None):
    closes = [float(value) for value in bars5m["close"].to_list()]
    highs = [float(value) for value in bars5m["high"].to_list()]
    lows = [float(value) for value in bars5m["low"].to_list()]
    fast = fast if fast is not None else ema_series(closes, config.EMA99_RETEST_FAST_EMA_LENGTH)
    slow = slow if slow is not None else ema_series(closes, config.EMA99_RETEST_SLOW_EMA_LENGTH)
    state = {
        "waiting_long": False,
        "waiting_short": False,
        "traded_long": False,
        "traded_short": False,
        "long_trigger_low": None,
        "short_trigger_high": None,
        "entry": None,
    }
    for index in range(1, len(closes)):
        if fast[index] is None or slow[index] is None or fast[index - 1] is None or slow[index - 1] is None:
            continue
        dmi = adx_by_bar[index] if index < len(adx_by_bar) else None
        adx_value = dmi[0] if isinstance(dmi, (tuple, list)) else dmi
        trend_ok = adx_value is not None and adx_value > config.EMA99_RETEST_MIN_ADX
        golden = fast[index] > slow[index] and fast[index - 1] <= slow[index - 1]
        death = fast[index] < slow[index] and fast[index - 1] >= slow[index - 1]
        if golden and trend_ok:
            state.update({
                "waiting_long": True,
                "waiting_short": False,
                "traded_long": False,
                "entry": None,
            })
            continue
        elif death and trend_ok:
            state.update({
                "waiting_short": True,
                "waiting_long": False,
                "traded_short": False,
                "entry": None,
            })
            continue

        if state["waiting_long"] and not state["traded_long"]:
            distance = (closes[index] - slow[index]) / slow[index]
            if lows[index] <= slow[index] and closes[index] >= slow[index] and distance <= config.EMA99_RETEST_MAX_RETEST_DISTANCE_PCT / 100:
                state.update({
                    "traded_long": True,
                    "waiting_long": False,
                    "long_trigger_low": lows[index],
                    "entry": {"direction": "long", "index": index},
                })
        if state["waiting_short"] and not state["traded_short"]:
            distance = (slow[index] - closes[index]) / slow[index]
            if highs[index] >= slow[index] and closes[index] <= slow[index] and distance <= config.EMA99_RETEST_MAX_RETEST_DISTANCE_PCT / 100:
                state.update({
                    "traded_short": True,
                    "waiting_short": False,
                    "short_trigger_high": highs[index],
                    "entry": {"direction": "short", "index": index},
                })
    return state


def evaluate_symbol(bars5m, bars1h, *, asset: str, symbol: str, cutoff: datetime) -> dict | None:
    """Evaluate both directions at one completed 5m cutoff."""
    cutoff = _utc(cutoff)
    bars5m = _at_cutoff(bars5m, cutoff)
    bars1h = _at_cutoff(bars1h, cutoff)
    if (bars5m.is_empty() or bars1h.is_empty()
            or not _fresh_completed(bars5m, cutoff, 5 * 60 + config.DATA_FRESHNESS_MAX_SECONDS)
            or not _fresh_completed(bars1h, cutoff, 60 * 60 + config.DATA_FRESHNESS_MAX_SECONDS)):
        return None

    closes = [float(value) for value in bars5m["close"].to_list()]
    fast = ema_series(closes, config.EMA99_RETEST_FAST_EMA_LENGTH)
    slow = ema_series(closes, config.EMA99_RETEST_SLOW_EMA_LENGTH)
    rsi = wilder_rsi(closes, config.EMA99_RETEST_RSI_LENGTH)
    atr = wilder_atr(bars5m, config.EMA99_RETEST_ATR_LENGTH)
    if fast[-1] is None or slow[-1] is None or atr is None or atr <= 0:
        return None

    state = _replay_cross_state(
        bars5m, _expand_adx_to_5m(bars5m, bars1h), fast=fast, slow=slow,
    )
    entry = state.get("entry")
    if not entry or entry["index"] != bars5m.height - 1:
        return None

    direction = entry["direction"]
    trigger = state["long_trigger_low"] if direction == "long" else state["short_trigger_high"]
    entry_price = closes[-1]
    stop = (trigger - atr * config.EMA99_RETEST_ATR_STOP_MULTIPLIER
            if direction == "long" else
            trigger + atr * config.EMA99_RETEST_ATR_STOP_MULTIPLIER)
    if entry_price <= 0 or stop <= 0 or (direction == "long" and stop >= entry_price) or (direction == "short" and stop <= entry_price):
        return None
    dmi = _expand_adx_to_5m(bars5m, bars1h)[-1]
    adx_value = dmi[0] if isinstance(dmi, (tuple, list)) else dmi
    plus_di = dmi[1] if isinstance(dmi, (tuple, list)) and len(dmi) > 1 else None
    minus_di = dmi[2] if isinstance(dmi, (tuple, list)) and len(dmi) > 2 else None
    observed_at = _utc(bars5m["timestamp"][-1])
    distance = abs(entry_price - slow[-1]) / slow[-1] * 100
    return {
        "schema_version": 1,
        "strategy_id": STRATEGY_ID,
        "plugin_version": PLUGIN_VERSION,
        "asset": asset.upper(),
        "direction": direction,
        "setup_class": "ema99_retest_adx",
        "phase": f"{direction}_retest",
        "observed_at": observed_at.isoformat(),
        "valid_until": (observed_at + timedelta(minutes=5)).isoformat(),
        "horizon_minutes": 5,
        "confidence": 0.5,
        "confidence_status": "uncalibrated",
        "entry_condition": {"type": "market", "price": entry_price},
        "entry_price": entry_price,
        "invalidation_price": stop,
        "targets": [],
        "metadata": {
            "execution_timeframe": "5m",
            "trend_timeframe": "1h",
            "target_policy": "executor_derived_2r",
            "stop_policy": "trigger_extreme_plus_atr",
            "trigger_extreme": trigger,
            "strategy_exits": {
                "long": "5m RSI > 72 and close > EMA26 by 0.5%",
                "short": "5m RSI < 28 and close < EMA26 by 0.5%",
            },
        },
        "feature_snapshot": {
            "source_symbol": symbol,
            "ema26_5m": fast[-1],
            "ema99_5m": slow[-1],
            "rsi14_5m": rsi[-1],
            "atr14_5m": atr,
            "adx_1h": adx_value,
            "+di_1h": plus_di,
            "-di_1h": minus_di,
            "cross": "golden" if direction == "long" else "death",
            "retest_distance_pct": distance,
            "trigger_extreme": trigger,
            "cutoff": cutoff.isoformat(),
        },
    }


def evaluate_exit(bars5m, *, side: str, cutoff: datetime) -> dict | None:
    """Evaluate the deterministic RSI/EMA26 exit on completed 5m bars."""
    cutoff = _utc(cutoff)
    bars5m = _at_cutoff(bars5m, cutoff)
    if not _fresh_completed(bars5m, cutoff, 5 * 60 + config.DATA_FRESHNESS_MAX_SECONDS):
        return None
    closes = [float(value) for value in bars5m["close"].to_list()]
    ema26 = ema_series(closes, config.EMA99_RETEST_FAST_EMA_LENGTH)
    rsi = wilder_rsi(closes, config.EMA99_RETEST_RSI_LENGTH)
    if ema26[-1] is None or rsi[-1] is None:
        return None
    spread = (closes[-1] - ema26[-1]) / ema26[-1] * 100
    long_exit = side == "long" and rsi[-1] > config.EMA99_RETEST_LONG_EXIT_RSI and spread > config.EMA99_RETEST_EXIT_SPREAD_PCT
    short_exit = side == "short" and rsi[-1] < config.EMA99_RETEST_SHORT_EXIT_RSI and spread < -config.EMA99_RETEST_EXIT_SPREAD_PCT
    if not (long_exit or short_exit):
        return None
    return {
        "action": "exit",
        "side": side,
        "rule_name": f"{side}_rsi_ema26_spread_exit",
        "cutoff": cutoff.isoformat(),
        "inputs": {
            "close_5m": closes[-1],
            "ema26_5m": ema26[-1],
            "rsi14_5m": rsi[-1],
            "spread_pct": spread,
        },
    }


def evaluate_stop_revision(bars5m, *, side: str, trigger_extreme: float, cutoff: datetime) -> dict | None:
    """Return the current closed-bar ATR stop for an open position."""
    cutoff = _utc(cutoff)
    bars5m = _at_cutoff(bars5m, cutoff)
    if not _fresh_completed(bars5m, cutoff, 5 * 60 + config.DATA_FRESHNESS_MAX_SECONDS):
        return None
    atr = wilder_atr(bars5m, config.EMA99_RETEST_ATR_LENGTH)
    if atr is None or atr <= 0 or trigger_extreme <= 0:
        return None
    stop = (trigger_extreme - atr * config.EMA99_RETEST_ATR_STOP_MULTIPLIER
            if side == "long" else
            trigger_extreme + atr * config.EMA99_RETEST_ATR_STOP_MULTIPLIER)
    return {
        "action": "update_stop",
        "side": side,
        "stop_loss": stop,
        "rule_name": f"{side}_atr_stop_revision",
        "cutoff": cutoff.isoformat(),
        "inputs": {"atr14_5m": atr, "trigger_extreme": trigger_extreme},
    }


def run_plugin(cutoff_id: str, snapshot: dict) -> list[dict]:
    cutoff = cutoff_from_id(str(snapshot.get("cutoff_at") or cutoff_id), snapshot.get("now"))
    conn = config.get_db_connection(read_only=True, db_path=snapshot.get("market_db_path"))
    try:
        events = []
        for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
            event = evaluate_symbol(
                load_bars_for_interval(conn, symbol, "5m", cutoff),
                load_bars_for_interval(conn, symbol, config.EMA99_RETEST_ADX_TIMEFRAME, cutoff),
                asset=asset, symbol=symbol, cutoff=cutoff,
            )
            if event and not has_active_event(STRATEGY_ID, asset, event["direction"], now=cutoff):
                event["input_snapshot_id"] = cutoff_id
                events.append(event)
        return events
    finally:
        conn.close()
