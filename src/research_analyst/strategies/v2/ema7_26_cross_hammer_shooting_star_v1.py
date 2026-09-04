"""5m EMA7/26 cross strategy with candle setup and confirmed 1h ADX."""

from __future__ import annotations

import math
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


STRATEGY_ID = config.EMA7_26_CROSS_HAMMER_STRATEGY_ID
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


def _valid_bar_history(bars, interval_minutes: int) -> bool:
    required = {"timestamp", "open", "high", "low", "close"}
    if bars.is_empty() or not required.issubset(bars.columns):
        return False
    try:
        timestamps = [_utc(value) for value in bars["timestamp"].to_list()]
    except (TypeError, ValueError, OverflowError):
        return False
    for index, row in enumerate(bars.iter_rows(named=True)):
        try:
            open_price = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
        except (TypeError, ValueError):
            return False
        if (
            not all(math.isfinite(value) for value in (open_price, high, low, close))
            or min(open_price, high, low, close) <= 0
            or high < max(open_price, close)
            or low > min(open_price, close)
            or high < low
        ):
            return False
        if index and (timestamps[index] - timestamps[index - 1]).total_seconds() != interval_minutes * 60:
            return False
    return True


def _ready_history(bars, cutoff: datetime, interval_minutes: int, max_age_seconds: float,
                   require_current: bool = False) -> bool:
    if not _valid_bar_history(bars, interval_minutes) or not _fresh_completed(bars, cutoff, max_age_seconds):
        return False
    return not require_current or _utc(bars["timestamp"][-1]) == _utc(cutoff)


def _rsi_series(values: list[float]):
    return wilder_rsi(values, config.EMA7_26_CROSS_RSI_LENGTH)


def _candle_pattern(open_price: float, high: float, low: float, close: float) -> dict[str, bool] | None:
    values = (open_price, high, low, close)
    if not all(math.isfinite(value) for value in values):
        return None
    if high < max(open_price, close) or low > min(open_price, close) or high < low:
        return None
    body = abs(close - open_price)
    if body <= config.EMA7_26_CROSS_MIN_BODY:
        return None
    upper_wick = high - max(open_price, close)
    lower_wick = min(open_price, close) - low
    if upper_wick < 0 or lower_wick < 0:
        return None
    return {
        "shooting_star": (
            upper_wick >= body * config.EMA7_26_CROSS_STAR_UPPER_WICK_RATIO
            and lower_wick <= body * config.EMA7_26_CROSS_STAR_LOWER_WICK_RATIO
        ),
        "hammer": (
            lower_wick >= body * config.EMA7_26_CROSS_HAMMER_LOWER_WICK_RATIO
            and upper_wick <= body * config.EMA7_26_CROSS_HAMMER_UPPER_WICK_RATIO
        ),
    }


def _near_ema26(close: float, ema26: float) -> bool:
    return (
        math.isfinite(close)
        and math.isfinite(ema26)
        and ema26 > 0
        and abs(close - ema26) / ema26 <= config.EMA7_26_CROSS_EMA_PROXIMITY_PCT / 100
    )


def _find_setup(bars, direction: str) -> dict[str, Any] | None:
    if bars.height < 2:
        return None
    opens = [float(value) for value in bars["open"].to_list()]
    highs = [float(value) for value in bars["high"].to_list()]
    lows = [float(value) for value in bars["low"].to_list()]
    closes = [float(value) for value in bars["close"].to_list()]
    ema26 = ema_series(closes, config.EMA7_26_CROSS_SLOW_EMA)
    last_index = bars.height - 1
    first_index = max(0, last_index - config.EMA7_26_CROSS_SETUP_LOOKBACK)
    for index in range(last_index - 1, first_index - 1, -1):
        if ema26[index] is None:
            continue
        pattern = _candle_pattern(opens[index], highs[index], lows[index], closes[index])
        if pattern is None or not _near_ema26(closes[index], ema26[index]):
            continue
        if direction == "long" and pattern["hammer"]:
            return {"index": index, "timestamp": _utc(bars["timestamp"][index]), "low": lows[index]}
        if direction == "short" and pattern["shooting_star"]:
            return {"index": index, "timestamp": _utc(bars["timestamp"][index]), "high": highs[index]}
    return None


def _cross_direction(fast, slow) -> str | None:
    if len(fast) < 2 or len(slow) < 2:
        return None
    previous = (fast[-2], slow[-2])
    current = (fast[-1], slow[-1])
    if any(value is None for value in (*previous, *current)):
        return None
    if previous[0] <= previous[1] and current[0] > current[1]:
        return "long"
    if previous[0] >= previous[1] and current[0] < current[1]:
        return "short"
    return None


def evaluate_symbol(bars5m, bars1h, *, asset: str, symbol: str, cutoff: datetime) -> dict | None:
    """Evaluate one completed 5m cutoff using confirmed 1h trend data."""
    cutoff = _utc(cutoff)
    bars5m = _at_cutoff(bars5m, cutoff)
    bars1h = _at_cutoff(bars1h, cutoff)
    if (
        bars5m.is_empty()
        or bars1h.is_empty()
        or not _ready_history(
            bars5m, cutoff, 5, 5 * 60 + config.DATA_FRESHNESS_MAX_SECONDS, require_current=True
        )
        or not _ready_history(bars1h, cutoff, 60, 60 * 60 + config.DATA_FRESHNESS_MAX_SECONDS)
    ):
        return None

    dmi = _dmi_adx(
        bars1h,
        config.EMA7_26_CROSS_ADX_LENGTH,
        config.EMA7_26_CROSS_ADX_SMOOTHING,
    )
    try:
        dmi_valid = dmi is not None and len(dmi) == 3 and all(math.isfinite(float(value)) for value in dmi)
    except (TypeError, ValueError):
        dmi_valid = False
    if not dmi_valid or dmi[0] < config.EMA7_26_CROSS_ADX_MIN:
        return None

    closes = [float(value) for value in bars5m["close"].to_list()]
    fast = ema_series(closes, config.EMA7_26_CROSS_FAST_EMA)
    slow = ema_series(closes, config.EMA7_26_CROSS_SLOW_EMA)
    rsi = _rsi_series(closes)
    atr = wilder_atr(bars5m, config.EMA7_26_CROSS_ATR_LENGTH)
    if (
        len(rsi) < 1
        or fast[-1] is None
        or slow[-1] is None
        or not math.isfinite(float(fast[-1]))
        or not math.isfinite(float(slow[-1]))
        or slow[-1] <= 0
        or rsi[-1] is None
        or not math.isfinite(float(rsi[-1]))
        or atr is None
        or atr <= 0
        or not math.isfinite(float(atr))
    ):
        return None

    direction = _cross_direction(fast, slow)
    if direction is None or not (
        config.EMA7_26_CROSS_ENTRY_RSI_MIN <= rsi[-1] <= config.EMA7_26_CROSS_ENTRY_RSI_MAX
    ):
        return None
    setup = _find_setup(bars5m, direction)
    if setup is None:
        return None

    entry = closes[-1]
    stop = (
        setup["low"] - atr * config.EMA7_26_CROSS_ATR_STOP_MULTIPLIER
        if direction == "long"
        else setup["high"] + atr * config.EMA7_26_CROSS_ATR_STOP_MULTIPLIER
    )
    if (
        entry <= 0
        or stop <= 0
        or (direction == "long" and stop >= entry)
        or (direction == "short" and stop <= entry)
    ):
        return None

    observed_at = _utc(bars5m["timestamp"][-1])
    spread_bps = abs(fast[-1] - slow[-1]) / slow[-1] * 10000 if slow[-1] > 0 else None
    return {
        "schema_version": 1,
        "strategy_id": STRATEGY_ID,
        "plugin_version": PLUGIN_VERSION,
        "asset": asset.upper(),
        "direction": direction,
        "setup_class": "ema7_26_cross_hammer_shooting_star",
        "phase": "golden_cross" if direction == "long" else "death_cross",
        "observed_at": observed_at.isoformat(),
        "valid_until": (observed_at + timedelta(minutes=config.EMA7_26_CROSS_ENTRY_VALIDITY_MINUTES)).isoformat(),
        "horizon_minutes": config.EMA7_26_CROSS_ENTRY_VALIDITY_MINUTES,
        "confidence": 0.5,
        "confidence_status": "uncalibrated",
        "entry_condition": {"type": "market", "price": entry},
        "entry_price": entry,
        "invalidation_price": stop,
        "targets": [],
        "metadata": {
            "execution_timeframe": "5m",
            "setup_timeframe": "5m",
            "trend_timeframe": "1h",
            "target_policy": "executor_derived_2r",
            "stop_policy": "setup_extreme_plus_atr",
            "strategy_exits": {
                "long": "5m RSI < 28 and EMA spread > 50 bps",
                "short": "5m RSI > 72 and EMA spread > 50 bps",
            },
        },
        "feature_snapshot": {
            "source_symbol": symbol,
            "setup_timestamp": setup["timestamp"].isoformat(),
            "setup_high": setup.get("high"),
            "setup_low": setup.get("low"),
            "ema7_5m": fast[-1],
            "ema26_5m": slow[-1],
            "rsi14_5m": rsi[-1],
            "atr14_5m": atr,
            "adx14_1h": dmi[0],
            "plus_di14_1h": dmi[1],
            "minus_di14_1h": dmi[2],
            "ema_spread_bps": spread_bps,
            "cutoff": cutoff.isoformat(),
        },
    }


def _ema_rsi_spread(bars5m):
    closes = [float(value) for value in bars5m["close"].to_list()]
    fast = ema_series(closes, config.EMA7_26_CROSS_FAST_EMA)
    slow = ema_series(closes, config.EMA7_26_CROSS_SLOW_EMA)
    rsi = _rsi_series(closes)
    if (
        not fast or not slow or not rsi or fast[-1] is None or slow[-1] is None
        or rsi[-1] is None or slow[-1] <= 0
        or not all(math.isfinite(float(value)) for value in (fast[-1], slow[-1], rsi[-1]))
    ):
        return None
    spread_bps = abs(fast[-1] - slow[-1]) / slow[-1] * 10000
    return rsi[-1], fast[-1], slow[-1], spread_bps


def evaluate_exit(bars5m, *, side: str, cutoff: datetime) -> dict | None:
    """Return the intact TA exit policy without placing an order."""
    cutoff = _utc(cutoff)
    bars5m = _at_cutoff(bars5m, cutoff)
    if not _ready_history(bars5m, cutoff, 5, 5 * 60 + config.DATA_FRESHNESS_MAX_SECONDS):
        return None
    values = _ema_rsi_spread(bars5m)
    if values is None or side not in {"long", "short"}:
        return None
    rsi, fast, slow, spread_bps = values
    long_exit = side == "long" and rsi < config.EMA7_26_CROSS_LONG_EXIT_RSI and spread_bps > config.EMA7_26_CROSS_EXIT_SPREAD_BPS
    short_exit = side == "short" and rsi > config.EMA7_26_CROSS_SHORT_EXIT_RSI and spread_bps > config.EMA7_26_CROSS_EXIT_SPREAD_BPS
    if not (long_exit or short_exit):
        return None
    return {
        "action": "exit",
        "side": side,
        "rule_name": f"{side}_rsi_ema_spread_exit",
        "cutoff": cutoff.isoformat(),
        "inputs": {"rsi5m": rsi, "ema7_5m": fast, "ema26_5m": slow, "spread_bps": spread_bps},
    }


def run_plugin(cutoff_id: str, snapshot: dict) -> list[dict]:
    cutoff = cutoff_from_id(str(snapshot.get("cutoff_at") or cutoff_id), snapshot.get("now"))
    conn = config.get_db_connection(read_only=True, db_path=snapshot.get("market_db_path"))
    try:
        events = []
        for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
            event = evaluate_symbol(
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
