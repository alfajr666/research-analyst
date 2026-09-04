"""1m EMA9/StochRSI entries gated by confirmed 1h ADX and 5m structure."""

from __future__ import annotations

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


STRATEGY_ID = config.EMA9_ADX_STRATEGY_ID
PLUGIN_VERSION = "v1"


def _stoch_values(values: list[float]):
    return stoch_rsi(
        values,
        config.EMA9_ADX_RSI_LENGTH,
        config.EMA9_ADX_STOCH_LENGTH,
        config.EMA9_ADX_K_LENGTH,
        config.EMA9_ADX_D_LENGTH,
    )


def _rsi_series(values: list[float]):
    return wilder_rsi(values, config.EMA9_ADX_RSI_LENGTH)


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _at_cutoff(bars, cutoff: datetime):
    """Keep the cutoff explicit even when callers provide an unbounded frame."""
    if bars.is_empty():
        return bars
    cutoff = _utc(cutoff)
    return bars.filter(bars["timestamp"] <= cutoff).sort("timestamp")


def _latest_timestamp(bars) -> datetime | None:
    if bars.is_empty():
        return None
    return _utc(bars["timestamp"][-1])


def _fresh_completed(bars, cutoff: datetime, max_age_seconds: float) -> bool:
    latest = _latest_timestamp(bars)
    if latest is None:
        return False
    cutoff = _utc(cutoff)
    age = (cutoff - latest).total_seconds()
    return 0 <= age <= max_age_seconds


def _crossed_up(k: list[float | None], d: list[float | None]) -> bool:
    if len(k) < 2 or len(d) < 2:
        return False
    return all(value is not None for value in (k[-2], d[-2], k[-1], d[-1])) and k[-2] <= d[-2] and k[-1] > d[-1]


def _crossed_down(k: list[float | None], d: list[float | None]) -> bool:
    if len(k) < 2 or len(d) < 2:
        return False
    return all(value is not None for value in (k[-2], d[-2], k[-1], d[-1])) and k[-2] >= d[-2] and k[-1] < d[-1]


def _structure(bars5):
    count = config.EMA9_ADX_STRUCTURE_BARS
    if bars5.height < count:
        return None
    closes = [float(value) for value in bars5["close"].to_list()]
    emas = ema_series(closes, config.EMA9_ADX_EMA_LENGTH)
    window = list(zip(closes[-count:], emas[-count:]))
    if any(ema is None for _, ema in window):
        return None
    lows = [float(value) for value in bars5["low"].to_list()[-count:]]
    highs = [float(value) for value in bars5["high"].to_list()[-count:]]
    return {
        "long": all(close >= ema for close, ema in window),
        "short": all(close <= ema for close, ema in window),
        "low": min(lows),
        "high": max(highs),
        "ema": emas[-1],
    }


def evaluate_symbol(bars1m, bars5m, bars1h, *, asset: str, symbol: str,
                    cutoff: datetime) -> dict | None:
    """Evaluate one completed 1m cutoff without mutating strategy state."""
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
        bars1h,
        config.EMA9_ADX_ADX_LENGTH,
        config.EMA9_ADX_ADX_LENGTH,
    )
    if dmi is None or dmi[0] <= config.EMA9_ADX_ADX_MIN:
        return None

    closes1m = [float(value) for value in bars1m["close"].to_list()]
    raw1m, k1m, d1m = _stoch_values(closes1m)
    ema1m = ema_series(closes1m, config.EMA9_ADX_EMA_LENGTH)[-1]
    if (len(k1m) < 2 or len(d1m) < 2 or ema1m is None
            or any(value is None for value in (k1m[-2], d1m[-2], k1m[-1], d1m[-1]))):
        return None

    structure = _structure(bars5m)
    atr5m = wilder_atr(bars5m, config.EMA9_ADX_ATR_LENGTH)
    if structure is None or atr5m is None or atr5m <= 0:
        return None

    entry = closes1m[-1]
    long_trigger = _crossed_up(k1m, d1m) and entry > ema1m
    short_trigger = _crossed_down(k1m, d1m) and entry < ema1m
    long_signal = long_trigger and structure["long"]
    short_signal = short_trigger and structure["short"]
    if not (long_signal or short_signal):
        return None

    direction = "long" if long_signal else "short"
    stop = (structure["low"] - config.EMA9_ADX_ATR_MULTIPLIER * atr5m
            if direction == "long" else
            structure["high"] + config.EMA9_ADX_ATR_MULTIPLIER * atr5m)
    if entry <= 0 or stop <= 0 or (direction == "long" and stop >= entry) or (direction == "short" and stop <= entry):
        return None

    observed_at = _latest_timestamp(bars1m)
    assert observed_at is not None
    return {
        "schema_version": 1,
        "strategy_id": STRATEGY_ID,
        "plugin_version": PLUGIN_VERSION,
        "asset": asset.upper(),
        "direction": direction,
        "setup_class": "ema9_adx_stochrsi_state",
        "phase": "long_trigger" if direction == "long" else "short_trigger",
        "observed_at": observed_at.isoformat(),
        "valid_until": (observed_at + timedelta(minutes=config.EMA9_ADX_ENTRY_VALIDITY_MINUTES)).isoformat(),
        "horizon_minutes": config.EMA9_ADX_ENTRY_VALIDITY_MINUTES,
        "confidence": 0.5,
        "confidence_status": "uncalibrated",
        "entry_condition": {"type": "market", "price": entry},
        "entry_price": entry,
        "invalidation_price": stop,
        "targets": [],
        "metadata": {
            "execution_timeframe": "1m",
            "structure_timeframe": "5m",
            "trend_timeframe": "1h",
            "target_policy": "executor_derived_2r",
            "stop_policy": "structure_extreme_plus_atr",
            "strategy_exits": {
                "long_extension": "5m close >= EMA9 * 1.05 and K >= 80 and K crosses above D",
                "short_extension": "5m close <= EMA9 * 0.95 and K <= 20 and K crosses below D",
                "long_momentum": "1m K reached 80, then crosses below D while 5m RSI > 75",
                "short_momentum": "1m K reached 20, then crosses above D while 5m RSI < 25",
            },
        },
        "feature_snapshot": {
            "source_symbol": symbol,
            "adx_1h": dmi[0],
            "+di_1h": dmi[1],
            "-di_1h": dmi[2],
            "rsi_1m": _rsi_series(closes1m)[-1],
            "stochrsi_raw_1m": raw1m[-1],
            "stochrsi_k_1m": k1m[-1],
            "stochrsi_d_1m": d1m[-1],
            "ema9_1m": ema1m,
            "ema9_5m": structure["ema"],
            "structure_low_5m": structure["low"],
            "structure_high_5m": structure["high"],
            "atr14_5m": atr5m,
            "atr_stop_multiplier": config.EMA9_ADX_ATR_MULTIPLIER,
            "structure_bars": config.EMA9_ADX_STRUCTURE_BARS,
            "cutoff": cutoff.isoformat(),
        },
    }


def evaluate_exit(bars1m, bars5m, *, side: str, opened_at: datetime,
                  cutoff: datetime) -> dict | None:
    """Return the first deterministic strategy exit, without placing orders."""
    cutoff = _utc(cutoff)
    opened_at = _utc(opened_at)
    bars1m = _at_cutoff(bars1m, cutoff)
    bars5m = _at_cutoff(bars5m, cutoff)
    if (bars1m.is_empty() or bars5m.is_empty()
            or not _fresh_completed(bars1m, cutoff, config.DATA_FRESHNESS_MAX_SECONDS)
            or not _fresh_completed(bars5m, cutoff, 5 * 60 + config.DATA_FRESHNESS_MAX_SECONDS)):
        return None

    closes5m = [float(value) for value in bars5m["close"].to_list()]
    _, k5m, d5m = _stoch_values(closes5m)
    ema5m = ema_series(closes5m, config.EMA9_ADX_EMA_LENGTH)
    rsi5m = _rsi_series(closes5m)
    if not rsi5m or rsi5m[-1] is None:
        return None
    close5 = closes5m[-1]
    if (len(k5m) >= 2 and len(d5m) >= 2
            and not any(value is None for value in (k5m[-2], d5m[-2], k5m[-1], d5m[-1], ema5m[-1]))):
        long_extension = (
            side == "long"
            and close5 >= ema5m[-1] * (1 + config.EMA9_ADX_EXTENSION_OFFSET_PCT / 100)
            and k5m[-1] >= config.EMA9_ADX_EXTENSION_LONG_K
            and _crossed_up(k5m, d5m)
        )
        short_extension = (
            side == "short"
            and close5 <= ema5m[-1] * (1 - config.EMA9_ADX_EXTENSION_OFFSET_PCT / 100)
            and k5m[-1] <= config.EMA9_ADX_EXTENSION_SHORT_K
            and _crossed_down(k5m, d5m)
        )
        if long_extension or short_extension:
            return {
                "action": "exit",
                "side": side,
                "rule_name": f"{side}_extension_tp",
                "cutoff": cutoff.isoformat(),
                "inputs": {"close_5m": close5, "ema9_5m": ema5m[-1], "k_5m": k5m[-1], "d_5m": d5m[-1]},
            }

    closes1m = [float(value) for value in bars1m["close"].to_list()]
    _, k1m, d1m = _stoch_values(closes1m)
    timestamps1m = [_utc(value) for value in bars1m["timestamp"].to_list()]
    post_entry = [index for index, timestamp in enumerate(timestamps1m) if timestamp > opened_at]
    if not post_entry:
        return None
    extreme = (
        any(k1m[index] is not None and k1m[index] >= config.EMA9_ADX_MOMENTUM_LONG_K for index in post_entry)
        if side == "long" else
        any(k1m[index] is not None and k1m[index] <= config.EMA9_ADX_MOMENTUM_SHORT_K for index in post_entry)
    )
    current = post_entry[-1]
    previous = current - 1
    if previous < 0:
        return None
    if any(value is None for value in (k1m[previous], d1m[previous], k1m[current], d1m[current])):
        return None
    momentum = (
        side == "long"
        and extreme
        and k1m[previous] >= d1m[previous]
        and k1m[current] < d1m[current]
        and rsi5m[-1] > config.EMA9_ADX_MOMENTUM_LONG_RSI
    ) or (
        side == "short"
        and extreme
        and k1m[previous] <= d1m[previous]
        and k1m[current] > d1m[current]
        and rsi5m[-1] < config.EMA9_ADX_MOMENTUM_SHORT_RSI
    )
    if not momentum:
        return None
    return {
        "action": "exit",
        "side": side,
        "rule_name": f"{side}_momentum_exit",
        "cutoff": cutoff.isoformat(),
        "inputs": {"k_1m": k1m[current], "d_1m": d1m[current], "rsi_5m": rsi5m[-1]},
    }


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
                asset=asset,
                symbol=symbol,
                cutoff=cutoff,
            )
            if event and not has_active_event(STRATEGY_ID, asset, event["direction"], now=cutoff):
                event["input_snapshot_id"] = cutoff_id
                events.append(event)
        return events
    finally:
        conn.close()
