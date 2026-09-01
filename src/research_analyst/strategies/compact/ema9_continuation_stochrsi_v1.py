"""5m EMA9 continuation setup with a 1m StochRSI trigger."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from strategy_v2_context import completed_cycle_for, evaluation_symbols, has_active_event, last_completed_bar_fresh, load_bars_for_interval

STRATEGY_ID = "ema9-continuation-stochrsi-v1"
SETUP_CLASS = "ema9_continuation"
PHASE = "stochrsi_trigger"
PLUGIN_VERSION = "v1"


def _rsi(values: list[float], length: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    gains = losses = 0.0
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        gains = (gains * (length - 1) + max(delta, 0.0)) / length
        losses = (losses * (length - 1) + max(-delta, 0.0)) / length
        out[i] = 100.0 if losses == 0 else 100.0 - 100.0 / (1.0 + gains / losses)
    return out


def _stoch_rsi(values: list[float]) -> tuple[list[float | None], list[float | None], list[float | None]]:
    rsi = _rsi(values)
    raw: list[float | None] = [None] * len(values)
    for i in range(13, len(values)):
        window = [x for x in rsi[i - 13 : i + 1] if x is not None]
        if len(window) < 14:
            continue
        lo, hi = min(window), max(window)
        raw[i] = 0.0 if hi == lo else 100.0 * (rsi[i] - lo) / (hi - lo)
    k = [None] * len(values)
    d = [None] * len(values)
    for i in range(15, len(values)):
        chunk = [x for x in raw[i - 2 : i + 1] if x is not None]
        if len(chunk) == 3:
            k[i] = sum(chunk) / 3
    for i in range(17, len(values)):
        chunk = [x for x in k[i - 2 : i + 1] if x is not None]
        if len(chunk) == 3:
            d[i] = sum(chunk) / 3
    return k, d, rsi


def _atr(rows, length: int = 14) -> float:
    """Calculate Wilder ATR from either plugin rows or a Polars frame."""
    if hasattr(rows, "to_dicts"):
        rows = rows.to_dicts()
    rows = list(rows)
    if not rows:
        return 0.0
    tr = []
    for i, row in enumerate(rows):
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        prev = float(rows[i - 1]["close"]) if i else close
        tr.append(max(high - low, abs(high - prev), abs(low - prev)))
    value = tr[0]
    for current in tr[1:]:
        value = (value * (length - 1) + current) / length
    return value


def evaluate_symbol(bars_5m, bars_1m, *, asset: str, symbol: str, cutoff: datetime) -> dict | None:
    if bars_5m.is_empty() or bars_1m.is_empty():
        return None
    if not last_completed_bar_fresh(bars_5m, cutoff) or bars_5m.height < 30 or bars_1m.height < 40:
        return None
    five = bars_5m.to_dicts()
    window = five[-15:]
    closes = [float(x["close"]) for x in five]
    ema = closes[0]
    emas = []
    for close in closes:
        ema = close * 0.2 + ema * 0.8
        emas.append(ema)
    ema_window = emas[-15:]
    above = all(float(row["close"]) >= ema_window[i] for i, row in enumerate(window))
    below = all(float(row["close"]) <= ema_window[i] for i, row in enumerate(window))
    if not (above or below):
        return None
    direction = "long" if above else "short"
    atr = _atr(five)
    entry = float(five[-1]["close"])
    raw_stop = min(float(x["low"]) for x in window) - 2 * atr if above else max(float(x["high"]) for x in window) + 2 * atr
    stop_distance = max(abs(entry - raw_stop), entry * 0.001)
    stop = entry - stop_distance if above else entry + stop_distance
    one = bars_1m.to_dicts()
    vals = [float(x["close"]) for x in one]
    k, d, rsi = _stoch_rsi(vals)
    i = len(vals) - 1
    if any(x is None for x in (k[i], k[i - 1], d[i], d[i - 1], rsi[i])):
        return None
    memory = any((x is not None and (x <= 20 if above else x >= 80)) for x in k[:i])
    cross = k[i - 1] <= d[i - 1] and k[i] > d[i] if above else k[i - 1] >= d[i - 1] and k[i] < d[i]
    ema1 = vals[0]
    for close in vals:
        ema1 = close * 0.2 + ema1 * 0.8
    if not (memory and cross and (vals[-1] > ema1 if above else vals[-1] < ema1)):
        return None
    observed = one[-1]["timestamp"]
    if hasattr(observed, "to_pydatetime"):
        observed = observed.to_pydatetime()
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    risk = abs(entry - stop)
    target = entry + 2 * risk if above else entry - 2 * risk
    return {"schema_version": 1, "strategy_id": STRATEGY_ID, "asset": asset.upper(), "direction": direction,
            "setup_class": SETUP_CLASS, "phase": PHASE, "observed_at": observed.isoformat(),
            "valid_until": (observed + timedelta(minutes=5)).isoformat(), "horizon_minutes": 5,
            "confidence": 0.5, "confidence_status": "uncalibrated", "entry_condition": {"type": "market", "price": entry},
            "invalidation_price": stop, "targets": [target], "plugin_version": PLUGIN_VERSION,
            "metadata": {"source_symbol": symbol, "atr14_5m": atr, "risk": risk,
                         "strategy_exits": {"long": "bear_cross_and_rsi_above_70_after_overbought", "short": "bull_cross_and_rsi_below_30_after_oversold"},
                         "protective_take_profit_r": 2.0}, "feature_snapshot": {"ema9_5m": emas[-1], "stoch_k_1m": k[i], "stoch_d_1m": d[i], "rsi_1m": rsi[i]}}


def evaluate(conn, cutoff: datetime | None = None, *, snapshot: dict | None = None, alpha_db_path=None, outbox_dir=None, eval_interval="5m") -> list[dict]:
    snapshot = snapshot or {}; cutoff = cutoff or completed_cycle_for(snapshot.get("now"), "5m")
    events = []
    for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
        event = evaluate_symbol(load_bars_for_interval(conn, symbol, "5m", cutoff), load_bars_for_interval(conn, symbol, "1m", cutoff), asset=asset, symbol=symbol, cutoff=cutoff)
        if event and not has_active_event(STRATEGY_ID, asset.upper(), event["direction"], alpha_db_path=alpha_db_path, outbox_dir=outbox_dir, now=cutoff): events.append(event)
    return events


def run_plugin(cutoff_id: str, snapshot: dict) -> list[dict]:
    conn = config.get_db_connection(read_only=True, db_path=snapshot.get("market_db_path"))
    try:
        events = evaluate(conn, snapshot=snapshot)
        for event in events:
            event["input_snapshot_id"] = cutoff_id
        return events
    finally:
        conn.close()
