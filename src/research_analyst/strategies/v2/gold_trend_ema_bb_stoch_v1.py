"""GoldTrendEMA_BB_Stoch, evaluated against every subscribed asset."""
from __future__ import annotations

from datetime import timedelta, timezone

import config
from strategy_v2_context import (
    cutoff_from_id, ema_last, evaluation_symbols, has_active_event,
    last_completed_bar_fresh, load_bars_for_interval, stoch_rsi, wilder_atr,
)

STRATEGY_ID = "gold-trend-ema-bb-stoch-v1"
PLUGIN_VERSION = "v1"


def evaluate_symbol(bars, *, asset: str, symbol: str, cutoff, cfg=None) -> dict | None:
    cfg = cfg or {
        "fast_ema": config.GOLD_FAST_EMA, "slow_ema": config.GOLD_SLOW_EMA, "bb_length": config.GOLD_BB_LENGTH, "bb_std": config.GOLD_BB_STD,
        "rsi_length": config.GOLD_RSI_LENGTH, "stoch_length": config.GOLD_STOCH_LENGTH, "k_smoothing": config.GOLD_K_SMOOTHING,
        "d_smoothing": config.GOLD_D_SMOOTHING, "oversold": 20.0, "overbought": 80.0,
        "atr_length": config.GOLD_ATR_LENGTH, "atr_stop_multiplier": config.GOLD_ATR_STOP_MULTIPLIER,
        "touch_tolerance": config.GOLD_TOUCH_TOLERANCE,
    }
    if bars.is_empty() or bars.height < max(cfg["slow_ema"], cfg["bb_length"] + cfg["stoch_length"] + 20):
        return None
    if not last_completed_bar_fresh(bars, cutoff) or bars["timestamp"][-1] > cutoff:
        return None
    closes = [float(value) for value in bars["close"].to_list()]
    ema50, ema200 = ema_last(closes, cfg["fast_ema"]), ema_last(closes, cfg["slow_ema"])
    atr = wilder_atr(bars, cfg["atr_length"])
    raw, k, d = stoch_rsi(closes, cfg["rsi_length"], cfg["stoch_length"], cfg["k_smoothing"], cfg["d_smoothing"])
    if None in (ema50, ema200, atr, raw[-1], raw[-2], k[-1], k[-2], d[-1], d[-2]):
        return None
    window = closes[-cfg["bb_length"]:]
    middle = sum(window) / cfg["bb_length"]
    deviation = (sum((value - middle) ** 2 for value in window) / cfg["bb_length"]) ** 0.5
    lower, upper = middle - cfg["bb_std"] * deviation, middle + cfg["bb_std"] * deviation
    row = bars.row(-1, named=True)
    close, low, high = float(row["close"]), float(row["low"]), float(row["high"])
    long_touch = low <= lower * (1.0 + cfg["touch_tolerance"])
    short_touch = high >= upper * (1.0 - cfg["touch_tolerance"])
    long_signal = close > ema50 and close > ema200 and long_touch and k[-2] <= d[-2] and k[-1] > d[-1] and k[-1] < cfg["oversold"]
    short_signal = close < ema50 and close < ema200 and short_touch and k[-2] >= d[-2] and k[-1] < d[-1] and k[-1] > cfg["overbought"]
    if not (long_signal or short_signal) or close <= 0 or atr <= 0:
        return None
    direction = "long" if long_signal else "short"
    stop = close - cfg["atr_stop_multiplier"] * atr if direction == "long" else close + cfg["atr_stop_multiplier"] * atr
    timestamp = row["timestamp"]
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return {
        "schema_version": 1, "strategy_id": STRATEGY_ID, "plugin_version": PLUGIN_VERSION,
        "asset": asset.upper(), "direction": direction, "setup_class": "gold_trend_ema_bb_stoch",
        "phase": "lower_band_pullback" if direction == "long" else "upper_band_pullback",
        "observed_at": timestamp.isoformat(), "valid_until": (timestamp + timedelta(minutes=5)).isoformat(),
        "horizon_minutes": 5, "confidence": 0.5, "confidence_status": "uncalibrated",
        "entry_condition": {"type": "market", "price": close}, "entry_price": close,
        "invalidation_price": stop,
        "feature_snapshot": {
            "source_symbol": symbol, "execution_timeframe": "5m", "ema50": ema50, "ema200": ema200,
            "bb_length": cfg["bb_length"], "bb_std": cfg["bb_std"], "lower_band": lower,
            "middle_band": middle, "upper_band": upper, "rsi_length": cfg["rsi_length"],
            "stochrsi_raw": raw[-1], "stochrsi_k": k[-1], "stochrsi_d": d[-1],
            "atr14": atr, "atr_stop_multiplier": cfg["atr_stop_multiplier"],
            "cutoff": cutoff.isoformat(),
        },
    }


def run_plugin(cutoff_id: str, snapshot: dict) -> list[dict]:
    cutoff = cutoff_from_id(str(snapshot.get("cutoff_at") or cutoff_id), snapshot.get("now"))
    conn = config.get_db_connection(read_only=True, db_path=snapshot.get("market_db_path"))
    try:
        events = []
        for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
            event = evaluate_symbol(load_bars_for_interval(conn, symbol, "5m", cutoff), asset=asset, symbol=symbol, cutoff=cutoff)
            if event and not has_active_event(STRATEGY_ID, asset, event["direction"], now=cutoff):
                event["input_snapshot_id"] = cutoff_id
                events.append(event)
        return events
    finally:
        conn.close()
