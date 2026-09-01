"""Long-only dual EMA proximity follower on completed 5m candles."""
from __future__ import annotations
from datetime import timedelta, timezone
import config
from strategy_v2_context import completed_cycle_for, ema_last, evaluation_symbols, load_bars_for_interval

STRATEGY_ID = "dual-zone-follower-v1"
PLUGIN_VERSION = "v1"

def evaluate_symbol(bars, *, asset, symbol, cutoff):
    lengths = (config.DUAL_ZONE_EXIT_EMA_LENGTH, config.DUAL_ZONE_ANCHOR_EMA_LENGTH, config.DUAL_ZONE_TREND_EMA_LENGTH)
    if bars.is_empty() or bars.height < max(lengths): return None
    row = bars.row(-1, named=True); close = float(row["close"]); closes = bars["close"].to_list()
    ema7, ema26, ema99 = (ema_last(closes, length) for length in lengths)
    if not all(value is not None and value > 0 for value in (close, ema7, ema26, ema99)): return None
    pct26 = (close - ema26) / ema26 * 100; pct99 = (close - ema99) / ema99 * 100
    if ema26 > ema99 and close > ema99 and close > ema26 and pct26 < config.DUAL_ZONE_A_ENTRY_DISTANCE_PCT:
        channel, stop, target = "A", ema26 * (1 - config.DUAL_ZONE_A_STOP_DISTANCE_PCT / 100), ema7 * (1 + config.DUAL_ZONE_A_TARGET_DISTANCE_PCT / 100)
    elif ema26 > ema99 and close > ema99 and pct99 < config.DUAL_ZONE_B_ENTRY_DISTANCE_PCT:
        channel, stop, target = "B", ema99 * (1 - config.DUAL_ZONE_B_STOP_DISTANCE_PCT / 100), ema7 * (1 + config.DUAL_ZONE_B_TARGET_DISTANCE_PCT / 100)
    else: return None
    observed = row["timestamp"]
    if observed.tzinfo is None: observed = observed.replace(tzinfo=timezone.utc)
    return {"schema_version": 1, "strategy_id": STRATEGY_ID, "asset": asset.upper(), "direction": "long", "setup_class": "dual_zone_follower", "phase": f"channel_{channel.lower()}", "observed_at": observed.isoformat(), "valid_until": (observed + timedelta(minutes=5)).isoformat(), "horizon_minutes": 5, "confidence": 0.5, "confidence_status": "uncalibrated", "entry_condition": {"type": "limit_at_ema_context", "price": close}, "entry_price": close, "invalidation_price": stop, "targets": [target], "plugin_version": PLUGIN_VERSION, "feature_snapshot": {"source_symbol": symbol, "execution_timeframe": "5m", "ema7": ema7, "ema26": ema26, "ema99": ema99, "channel": channel, "pct_above_ema26": pct26, "pct_above_ema99": pct99}}

def run_plugin(cutoff_id, snapshot):
    cutoff = completed_cycle_for(snapshot.get("now"), "5m")
    conn = config.get_db_connection(read_only=True, db_path=snapshot.get("market_db_path"))
    try:
        emitted = []
        for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
            event = evaluate_symbol(load_bars_for_interval(conn, symbol, "5m", cutoff), asset=asset, symbol=symbol, cutoff=cutoff)
            if event: event["input_snapshot_id"] = cutoff_id; emitted.append(event)
        return emitted
    finally: conn.close()
