"""MACD-EMA port: 1h long-only trend entry with dataframe exit and ATR stop.

Authority: repo-final/vectorbt/strategies/macd_ema.py
(see engine_handoff/all_families_engine_specs.md section 4 — REJECTED family,
median symbol -38%; ported disabled-by-default per operator decision so the
exact backtested behavior stays reproducible in production form).

Self-contained plugin using the repository's native engines (``ema_series``
for the Jesse-recurrence EMAs, ``wilder_atr_series`` for ATR); the MACD line/
signal construction is private to this strategy.

Entry: close > EMA100 AND MACD(12,26,9) line > signal AND ATR valid. Exits:
(a) close < EMA100 AND line < signal at that candle's close; (b) fixed stop =
entry-candle low - 2*ATR14, live intrabar, gap-pessimistic (executor bracket).
No TP. The backtest's full-sleeve sizing is engine-side.
"""

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
    strategy_market_connection,
)

STRATEGY_ID = config.MACD_EMA_STRATEGY_ID
PLUGIN_VERSION = "v1"


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _macd(closes: list[float]) -> tuple[list[float | None], list[float | None]]:
    """MACD line and signal from native ema_series (strategy-private assembly)."""
    fast = ema_series(closes, config.MACD_EMA_FAST_PERIOD)
    slow = ema_series(closes, config.MACD_EMA_SLOW_PERIOD)
    line = [None if (f is None or s is None) else f - s for f, s in zip(fast, slow)]
    signal = ema_series([v if v is not None else 0.0 for v in line],
                        config.MACD_EMA_SIGNAL_PERIOD)
    return line, signal


def evaluate_symbol(bars1h, bars5m, *, asset: str, symbol: str, cutoff: datetime) -> dict | None:
    cutoff = _utc(cutoff)
    if bars1h.is_empty() or bars5m.is_empty():
        return None
    bars1h = bars1h.filter(bars1h["timestamp"] <= cutoff).sort("timestamp")
    # 1h decisions fire only on completed hourly boundaries (ema20 pattern).
    if cutoff.minute % 60:
        return None
    if bars1h.height < max(config.MACD_EMA_EMA_PERIOD, config.MACD_EMA_SLOW_PERIOD
                           + config.MACD_EMA_SIGNAL_PERIOD) + 2:
        return None
    if not _fresh(bars1h, cutoff, 60 * 60 + config.DATA_FRESHNESS_MAX_SECONDS):
        return None

    from strategy_features import build_feature_frame

    features = build_feature_frame(bars1h, atr={"atr14": config.MACD_EMA_ATR_PERIOD})
    atr = features["atr14"].to_list()

    closes = [float(v) for v in bars1h["close"].to_list()]
    lows = [float(v) for v in bars1h["low"].to_list()]
    ema100 = ema_series(closes, config.MACD_EMA_EMA_PERIOD)
    line, signal = _macd(closes)

    last = len(closes) - 1
    if any(series[last] is None for series in (ema100, line, signal, atr)):
        return None
    if atr[last] <= 0:
        return None
    close = closes[last]

    long_entry = close > ema100[last] and line[last] > signal[last] and atr[last] > 0
    if not long_entry:
        return None
    # Entry-candle stop: last completed 1h bar's low - 2*ATR14. Fill happens at
    # the next 1h open (executor market order); stop stays frozen.
    stop = lows[last] - config.MACD_EMA_STOP_ATR_MULT * atr[last]
    entry_ref = close
    if stop >= entry_ref or entry_ref <= 0:
        return None

    observed = _utc(bars1h["timestamp"][-1])
    return {
        "schema_version": 1,
        "strategy_id": STRATEGY_ID,
        "plugin_version": PLUGIN_VERSION,
        "asset": asset.upper(),
        "direction": "long",
        "setup_class": "macd_ema_trend",
        "phase": "ema_macd_alignment",
        "observed_at": observed.isoformat(),
        "valid_until": (observed + timedelta(minutes=config.MACD_EMA_ENTRY_VALIDITY_MINUTES)).isoformat(),
        "horizon_minutes": config.MACD_EMA_ENTRY_VALIDITY_MINUTES,
        "confidence": 0.5,
        "confidence_status": "uncalibrated",
        "entry_condition": {"type": "market_next_bar_open", "price": entry_ref},
        "entry_price": entry_ref,
        "invalidation_price": stop,
        "targets": [],
        "metadata": {
            "execution_timeframe": "1h",
            "signal_timeframe": "1h",
            "direction_scope": "long_only",
            "entry_timing": "next_completed_1h_open",
            "stop_policy": "entry_candle_low_minus_2atr_frozen",
            "target_policy": "none_dataframe_exit_only",
            "strategy_exits": {
                "dataframe_exit": (
                    "1h close < EMA100 AND MACD line < signal -> exit at that candle's close "
                    "(executor-side rule; stop bracket remains active until then)"
                ),
            },
            "bracket_spec": {
                "version": "macd_ema",
                "stop": stop,
                "stop_semantics": "live intrabar every bar, gap-through fills at open",
            },
        },
        "feature_snapshot": {
            "source_symbol": symbol,
            "execution_timeframe": "1h",
            "ema100_1h": ema100[last],
            "macd_line": line[last],
            "macd_signal": signal[last],
            "atr14_1h": atr[last],
            "entry_candle_low": lows[last],
            "stop": stop,
            "cutoff": cutoff.isoformat(),
        },
    }


def evaluate_exit(bars1h, *, side: str, cutoff: datetime) -> dict | None:
    """Dataframe exit: close < EMA100 AND MACD line < signal (long side only)."""
    cutoff = _utc(cutoff)
    if side != "long":
        return None
    bars1h = bars1h.filter(bars1h["timestamp"] <= cutoff).sort("timestamp")
    if bars1h.height < config.MACD_EMA_EMA_PERIOD + 2:
        return None
    closes = [float(v) for v in bars1h["close"].to_list()]
    ema100 = ema_series(closes, config.MACD_EMA_EMA_PERIOD)
    line, signal = _macd(closes)
    last = len(closes) - 1
    if ema100[last] is None or line[last] is None or signal[last] is None:
        return None
    if closes[last] < ema100[last] and line[last] < signal[last]:
        return {
            "action": "exit",
            "side": side,
            "rule_name": "macd_ema_dataframe_exit",
            "cutoff": cutoff.isoformat(),
            "inputs": {"close_1h": closes[last], "ema100_1h": ema100[last],
                       "macd_line": line[last], "macd_signal": signal[last]},
        }
    return None


def _fresh(bars, cutoff: datetime, max_age_seconds: float) -> bool:
    latest = _utc(bars["timestamp"][-1])
    age = (cutoff - latest).total_seconds()
    return 0 <= age <= max_age_seconds


def run_plugin(cutoff_id: str, snapshot: dict) -> list[dict]:
    cutoff = cutoff_from_id(str(snapshot.get("cutoff_at") or cutoff_id), snapshot.get("now"))
    if cutoff.minute % 60:
        return []
    conn, owns_conn = strategy_market_connection(snapshot.get("market_db_path"))
    try:
        events = []
        for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
            bars1h = load_bars_for_interval(conn, symbol, "1h", cutoff)
            bars5m = load_bars_for_interval(conn, symbol, "5m", cutoff)
            event = evaluate_symbol(bars1h, bars5m, asset=asset, symbol=symbol, cutoff=cutoff)
            if event is not None and not has_active_event(STRATEGY_ID, asset, "long", now=cutoff):
                event["input_snapshot_id"] = cutoff_id
                events.append(event)
        return events
    finally:
        if owns_conn:
            conn.close()
