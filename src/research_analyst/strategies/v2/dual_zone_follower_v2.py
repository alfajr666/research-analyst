"""Point-in-time enhanced dual-zone follower, emitting both directions."""
from datetime import timedelta, timezone
import config
from polars_indicators import dmi_adx
from strategy_features import build_feature_frame, cached_feature_frame
from strategy_v2_context import (
    cutoff_from_id,
    ema_last,
    evaluation_symbols,
    get_shared_computation_context,
    has_active_event,
    load_bars_for_interval,
    strategy_market_connection,
)

def _dmi_adx(bars, length, smoothing, *, symbol=None, interval=None):
    context = get_shared_computation_context()
    if context is not None and symbol is not None and interval is not None:
        adx_series, plus_di, minus_di = context.dmi_adx(symbol, interval, length, smoothing)
        if not adx_series or plus_di is None or minus_di is None:
            return None
        return adx_series[-1], plus_di, minus_di
    adx_series, plus_di, minus_di = dmi_adx(bars, length, smoothing)
    if not adx_series or plus_di is None or minus_di is None:
        return None
    return adx_series[-1], plus_di, minus_di

def evaluate_symbol(bars, *, asset, symbol, cutoff, direction="long", features=None):
    n = max(config.DUAL_ZONE_EXIT_EMA_LENGTH, config.DUAL_ZONE_ANCHOR_EMA_LENGTH, config.DUAL_ZONE_TREND_EMA_LENGTH)
    if bars.is_empty() or bars.height < n: return None
    feature_frame = features if features is not None else bars
    row = feature_frame.row(-1, named=True); close = float(row["close"]); c = bars["close"].to_list()
    if features is None:
        e7, e26, e99 = (ema_last(c, x) for x in (config.DUAL_ZONE_EXIT_EMA_LENGTH, config.DUAL_ZONE_ANCHOR_EMA_LENGTH, config.DUAL_ZONE_TREND_EMA_LENGTH))
    else:
        def feature_value(name):
            value = row.get(name)
            return float(value) if value is not None else None
        e7 = feature_value(f"ema_{config.DUAL_ZONE_EXIT_EMA_LENGTH}")
        e26 = feature_value(f"ema_{config.DUAL_ZONE_ANCHOR_EMA_LENGTH}")
        e99 = feature_value(f"ema_{config.DUAL_ZONE_TREND_EMA_LENGTH}")
    if not all(x and x > 0 for x in (close, e7, e26, e99)): return None
    long = direction == "long"
    regime = e26 > e99 and close > e26 and close > e99 if long else e26 < e99 and close < e26 and close < e99
    if not regime: return None
    d26 = abs(close-e26)/e26*100; d99 = abs(close-e99)/e99*100
    if d26 <= config.DUAL_ZONE_A_ENTRY_DISTANCE_PCT:
        ch, anchor, target_pct = "A", e26, config.DUAL_ZONE_A_TARGET_DISTANCE_PCT
    elif d99 <= config.DUAL_ZONE_B_ENTRY_DISTANCE_PCT:
        ch, anchor, target_pct = "B", e99, config.DUAL_ZONE_B_TARGET_DISTANCE_PCT
    else: return None
    stop_pct = (config.DUAL_ZONE_A_STOP_DISTANCE_PCT if ch == "A" else config.DUAL_ZONE_B_STOP_DISTANCE_PCT)/100
    stop = anchor*(1-stop_pct if long else 1+stop_pct); target = e7*(1+target_pct/100 if long else 1-target_pct/100)
    observed = row["timestamp"].replace(tzinfo=timezone.utc) if row["timestamp"].tzinfo is None else row["timestamp"]
    return {"schema_version": 1, "strategy_id": f"dual-zone{'-short' if not long else ''}-follower-v2", "asset": asset.upper(), "direction": direction, "setup_class": "dual_zone_follower" if long else "dual_zone_short_follower", "phase": f"channel_{ch.lower()}", "observed_at": observed.isoformat(), "valid_until": (observed+timedelta(minutes=5)).isoformat(), "horizon_minutes": 5, "confidence": 0.5, "confidence_status": "uncalibrated", "entry_condition": {"type": "limit_at_ema_context", "price": close}, "entry_price": close, "invalidation_price": stop, "targets": [target], "feature_snapshot": {"source_symbol": symbol, "execution_timeframe":"5m", "ema7":e7,"ema26":e26,"ema99":e99,"channel":ch,"entry_distance_pct":d26 if ch=="A" else d99,"cutoff": cutoff.isoformat() if cutoff else None}}

def _run(cutoff_id, snapshot, direction):
    cutoff = cutoff_from_id(str(snapshot.get("cutoff_at") or cutoff_id), snapshot.get("now")); conn, owns_conn = strategy_market_connection(snapshot.get("market_db_path"))
    try:
        out=[]
        for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
            bars=load_bars_for_interval(conn, symbol, "5m", cutoff)
            adx_bars=load_bars_for_interval(conn, symbol, config.DUAL_ZONE_ADX_TIMEFRAME, cutoff)
            feature_key = f"dual-zone-5m-v1:{asset}:{cutoff.isoformat()}"
            features = cached_feature_frame(
                snapshot,
                feature_key,
                bars,
                lambda frame: build_feature_frame(frame, ema={
                    f"ema_{config.DUAL_ZONE_EXIT_EMA_LENGTH}": config.DUAL_ZONE_EXIT_EMA_LENGTH,
                    f"ema_{config.DUAL_ZONE_ANCHOR_EMA_LENGTH}": config.DUAL_ZONE_ANCHOR_EMA_LENGTH,
                    f"ema_{config.DUAL_ZONE_TREND_EMA_LENGTH}": config.DUAL_ZONE_TREND_EMA_LENGTH,
                }),
                asset=asset,
                interval="5m",
                cutoff=cutoff,
                feature_spec={"ema": {
                    f"ema_{config.DUAL_ZONE_EXIT_EMA_LENGTH}": config.DUAL_ZONE_EXIT_EMA_LENGTH,
                    f"ema_{config.DUAL_ZONE_ANCHOR_EMA_LENGTH}": config.DUAL_ZONE_ANCHOR_EMA_LENGTH,
                    f"ema_{config.DUAL_ZONE_TREND_EMA_LENGTH}": config.DUAL_ZONE_TREND_EMA_LENGTH,
                }},
            )
            dmi = _dmi_adx(
                adx_bars, config.DUAL_ZONE_ADX_DI_LENGTH, config.DUAL_ZONE_ADX_SMOOTHING,
                symbol=symbol, interval=config.DUAL_ZONE_ADX_TIMEFRAME,
            )
            if dmi and dmi[0] >= config.DUAL_ZONE_MIN_ADX and (not config.DUAL_ZONE_USE_DI_DIRECTION or (direction == "long" and dmi[1] > dmi[2]) or (direction == "short" and dmi[2] > dmi[1])):
                e=evaluate_symbol(bars, asset=asset, symbol=symbol, cutoff=cutoff, direction=direction, features=features)
            else: e=None
            if e and (not e.get("direction") or not has_active_event(e["strategy_id"], asset, direction, now=cutoff)):
                e["input_snapshot_id"] = cutoff_id
                e.setdefault("feature_snapshot", {}).update({"adx_1h": dmi[0], "+di_1h": dmi[1], "-di_1h": dmi[2], "cutoff": cutoff.isoformat()})
                out.append(e)
        return out
    finally:
        if owns_conn:
            conn.close()
def run_plugin(cutoff_id, snapshot): return _run(cutoff_id, snapshot, "long")
def run_short_plugin(cutoff_id, snapshot): return _run(cutoff_id, snapshot, "short")
