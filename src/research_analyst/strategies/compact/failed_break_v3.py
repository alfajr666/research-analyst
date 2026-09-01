"""FailedBreak v3: 4h swing failure/reclaim armed by a 5m StochRSI turn."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl

import config
from strategy_v2_context import (
    completed_cycle_for,
    has_active_event,
    last_completed_bar_fresh,
    evaluation_symbols,
    load_bars_for_interval,
    load_preferred_15m_bars,
    resample_ohlcv,
)

STRATEGY_ID = "failed-break-v3"
PLUGIN_VERSION = "failed_break_v3_pinescript_port"


def _stoch_rsi(close: pl.Series) -> tuple[pl.Series, pl.Series]:
    # Match the compact port: Wilder-like EMA RSI, then 14/3/3 StochRSI.
    d = close.diff()
    gain = d.clip(lower_bound=0).ewm_mean(alpha=1 / 14, adjust=False)
    loss = (-d.clip(upper_bound=0)).ewm_mean(alpha=1 / 14, adjust=False)
    rsi = 100 - 100 / (1 + gain / loss.replace(0, None))
    low = rsi.rolling_min(14)
    high = rsi.rolling_max(14)
    raw = 100 * (rsi - low) / (high - low).replace(0, None)
    return raw.rolling_mean(3), raw.rolling_mean(3).rolling_mean(3)


def _latest_setup(bars_15m: pl.DataFrame) -> dict | None:
    h4 = resample_ohlcv(bars_15m, "4h")
    if h4.height < 7:
        return None
    high = h4["high"].to_list(); low = h4["low"].to_list()
    close = h4["close"].to_list()
    swing_high = swing_low = None
    setup = None
    # Pivots become known three bars later, exactly as in the compact state.
    for i in range(h4.height):
        if i >= 6:
            c = i - 3
            if high[c] >= max(high[c - 3:c] + high[c + 1:c + 4]): swing_high = high[c]
            if low[c] <= min(low[c - 3:c] + low[c + 1:c + 4]): swing_low = low[c]
        if swing_high is not None:
            if high[i] > swing_high and close[i] < swing_high:
                setup = {"direction": "short", "stop": high[i], "swing": swing_high, "armed_at": h4["timestamp"][i]}
            elif high[i] > swing_high and close[i] > swing_high:
                if setup and setup["direction"] == "short": setup = None
        if swing_low is not None:
            if low[i] < swing_low and close[i] > swing_low:
                setup = {"direction": "long", "stop": low[i], "swing": swing_low, "armed_at": h4["timestamp"][i]}
            elif low[i] < swing_low and close[i] < swing_low:
                if setup and setup["direction"] == "long": setup = None
        if setup and ((setup["direction"] == "short" and close[i] > setup["stop"]) or
                      (setup["direction"] == "long" and close[i] < setup["stop"])):
            setup = None
    return setup


def evaluate_symbol(bars_5m: pl.DataFrame, bars_15m: pl.DataFrame, *, asset: str,
                    symbol: str, cutoff: datetime, cooldown_bars: int = 4) -> dict | None:
    if bars_5m.height < 40 or bars_15m.is_empty(): return None
    if not last_completed_bar_fresh(bars_5m, cutoff) or not last_completed_bar_fresh(bars_15m, cutoff): return None
    setup = _latest_setup(bars_15m)
    if setup is None: return None
    k, d = _stoch_rsi(bars_5m["close"])
    if any(v is None for v in (k[-1], d[-1], k[-2], d[-2])): return None
    direction = setup["direction"]
    trigger = (k[-1] < 20 and k[-2] <= d[-2] and k[-1] > d[-1]) if direction == "long" else (k[-1] > 80 and k[-2] >= d[-2] and k[-1] < d[-1])
    entry = float(bars_5m["close"][-1]); stop = float(setup["stop"])
    if not trigger or (direction == "long" and not entry > setup["swing"]) or (direction == "short" and not entry < setup["swing"]): return None
    risk = entry - stop if direction == "long" else stop - entry
    if risk <= 0: return None
    target = entry + 2 * risk if direction == "long" else entry - 2 * risk
    observed = bars_5m["timestamp"][-1]
    if observed.tzinfo is None: observed = observed.replace(tzinfo=timezone.utc)
    return {"schema_version": 1, "strategy_id": STRATEGY_ID, "asset": asset.upper(), "direction": direction,
            "setup_class": "failed_break_reclaim", "phase": "stoch_rsi_trigger", "observed_at": observed.isoformat(),
            "valid_until": (observed + timedelta(minutes=5)).isoformat(), "horizon_minutes": 5,
            "confidence": 0.5, "confidence_status": "uncalibrated", "entry_condition": {"type": "market", "price": entry},
            "entry_price": entry, "stop_loss": stop, "take_profit": target, "invalidation_price": stop, "targets": [target],
            "plugin_version": PLUGIN_VERSION, "feature_snapshot": {"source_symbol": symbol, "execution_timeframe": "5m",
            "context_timeframe": "15m->4h", "swing": setup["swing"], "strategy_stop": stop, "minimum_target_r": 2.0,
            "stoch_k": float(k[-1]), "stoch_d": float(d[-1]), "cooldown_bars": cooldown_bars}}


def run_plugin(cutoff_id: str, snapshot: dict) -> list[dict]:
    conn = config.get_db_connection(read_only=True, db_path=snapshot.get("market_db_path")); emitted = []
    try:
        cutoff = completed_cycle_for(snapshot.get("now"), "5m")
        for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
            bars5 = load_bars_for_interval(conn, symbol, "5m", cutoff)
            bars15 = load_preferred_15m_bars(conn, asset=asset, cutoff=cutoff)
            ev = evaluate_symbol(bars5, bars15, asset=asset, symbol=symbol, cutoff=cutoff)
            if ev and not has_active_event(STRATEGY_ID, asset.upper(), ev["direction"], now=cutoff):
                ev["input_snapshot_id"] = cutoff_id
                emitted.append(ev)
        return emitted
    finally: conn.close()
