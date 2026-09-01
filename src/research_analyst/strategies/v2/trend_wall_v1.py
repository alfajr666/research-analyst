"""15m TrendWall pullback/reclaim coordinated by completed 5m cutoffs."""
from __future__ import annotations

from datetime import timedelta, timezone

import config
from strategy_v2_context import (
    cutoff_from_id, evaluation_symbols, has_active_event, last_completed_bar_fresh,
    load_bars_for_interval, wilder_atr, wilder_rsi,
)
from strategies.v2.dual_zone_follower_v2 import _dmi_adx

STRATEGY_ID = "trend-wall-v1"
PLUGIN_VERSION = "v1"


def evaluate_symbol(bars15, bars1h, *, asset: str, symbol: str, cutoff, execution_bars=None) -> dict | None:
    if bars15.is_empty() or bars1h.is_empty() or not last_completed_bar_fresh(bars15, cutoff):
        return None
    signal = bars15.row(-1, named=True)
    if signal["timestamp"] > cutoff:
        return None
    dmi = _dmi_adx(bars1h, 14, 14)
    if dmi is None:
        return None
    closes1 = [float(value) for value in bars1h["close"].to_list()]
    from strategy_v2_context import ema_last
    wall, ema7, ema26 = ema_last(closes1, config.TREND_WALL_EMA_LENGTH), ema_last(closes1, 7), ema_last(closes1, 26)
    if None in (wall, ema7, ema26) or wall <= 0 or ema7 <= 0 or ema26 <= 0 or bars15.height < 2:
        return None
    closes15 = [float(value) for value in bars15["close"].to_list()]
    rsi = wilder_rsi(closes15, 14)
    execution_bars = execution_bars if execution_bars is not None else bars15
    atr = wilder_atr(execution_bars, config.TREND_WALL_ATR_LENGTH)
    if rsi[-1] is None or rsi[-2] is None or atr is None or atr <= 0:
        return None
    volumes = [float(value) for value in bars15["volume"].to_list()]
    if len(volumes) < 20 or sum(volumes[-20:]) <= 0:
        return None
    volume_ratio = volumes[-1] / (sum(volumes[-20:]) / 20)
    close, low, high = float(signal["close"]), float(signal["low"]), float(signal["high"])
    near_wall = abs(close - wall) / wall <= config.TREND_WALL_WALL_PROXIMITY
    long_signal = close > wall and near_wall and low <= wall and ema7 > ema26 and dmi[0] > config.TREND_WALL_ADX_MIN and rsi[-1] > rsi[-2] and rsi[-1] < 40 and volume_ratio > 0.5
    short_signal = close < wall and near_wall and high >= wall and ema7 < ema26 and dmi[0] > config.TREND_WALL_ADX_MIN and rsi[-1] < rsi[-2] and rsi[-1] > 60 and volume_ratio > 0.5
    if not (long_signal or short_signal):
        return None
    direction = "long" if long_signal else "short"
    stop = close - config.TREND_WALL_ATR_STOP_MULTIPLIER * atr if direction == "long" else close + config.TREND_WALL_ATR_STOP_MULTIPLIER * atr
    timestamp = signal["timestamp"]
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return {
        "schema_version": 1, "strategy_id": STRATEGY_ID, "plugin_version": PLUGIN_VERSION,
        "asset": asset.upper(), "direction": direction, "setup_class": "trend_wall",
        "phase": "wall_reclaim_long" if direction == "long" else "wall_reclaim_short",
        "observed_at": timestamp.isoformat(), "valid_until": (timestamp + timedelta(minutes=5)).isoformat(),
        "horizon_minutes": 5, "confidence": 0.5, "confidence_status": "uncalibrated",
        "entry_condition": {"type": "market", "price": close}, "entry_price": close,
        "invalidation_price": stop,
        "feature_snapshot": {
            "source_symbol": symbol, "signal_timeframe": "15m", "context_timeframe": "1h",
            "ema99_wall_1h": wall, "ema7_1h": ema7, "ema26_1h": ema26,
            "adx_1h": dmi[0], "+di_1h": dmi[1], "-di_1h": dmi[2], "rsi_15m": rsi[-1],
            "volume_ratio_15m": volume_ratio, "atr16_5m": atr, "wall_proximity": abs(close - wall) / wall,
            "wall_break_buffer_atr": 0.5, "structural_exit": "15m close beyond wall by 0.5 ATR",
            "cutoff": cutoff.isoformat(),
        },
    }


def run_plugin(cutoff_id: str, snapshot: dict) -> list[dict]:
    cutoff = cutoff_from_id(str(snapshot.get("cutoff_at") or cutoff_id), snapshot.get("now"))
    if cutoff.minute % 15:
        return []
    conn = config.get_db_connection(read_only=True, db_path=snapshot.get("market_db_path"))
    try:
        events = []
        for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
            event = evaluate_symbol(
                load_bars_for_interval(conn, symbol, "15m", cutoff),
                load_bars_for_interval(conn, symbol, "1h", cutoff),
                asset=asset, symbol=symbol, cutoff=cutoff,
                execution_bars=load_bars_for_interval(conn, symbol, "5m", cutoff),
            )
            if event and not has_active_event(STRATEGY_ID, asset, event["direction"], now=cutoff):
                event["input_snapshot_id"] = cutoff_id
                events.append(event)
        return events
    finally:
        conn.close()
