"""Point-in-time enhanced dual-zone follower, emitting both directions."""
from datetime import timedelta, timezone
import config
from strategy_v2_context import completed_cycle_for, ema_last, evaluation_symbols, load_bars_for_interval

def _dmi_adx(bars, length, smoothing):
    if bars.height < length + smoothing + 1: return None
    high, low, close = (bars[c].to_list() for c in ("high", "low", "close"))
    tr=[]; plus=[]; minus=[]
    for i in range(1, len(close)):
        tr.append(max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1])))
        up=high[i]-high[i-1]; down=low[i-1]-low[i]
        plus.append(up if up > down and up > 0 else 0.0); minus.append(down if down > up and down > 0 else 0.0)
    atr=sum(tr[:length]); p=sum(plus[:length]); m=sum(minus[:length]); dx=[]; pdi=midi=None
    for i in range(length, len(tr)):
        atr=atr-atr/length+tr[i]; p=p-p/length+plus[i]; m=m-m/length+minus[i]
        pdi=100*p/atr if atr else 0; midi=100*m/atr if atr else 0
        dx.append(100*abs(pdi-midi)/(pdi+midi) if pdi+midi else 0)
    if len(dx) < smoothing: return None
    adx=sum(dx[:smoothing])/smoothing
    for value in dx[smoothing:]: adx=(adx*(smoothing-1)+value)/smoothing
    return adx, pdi, midi

def evaluate_symbol(bars, *, asset, symbol, cutoff, direction="long"):
    n = max(config.DUAL_ZONE_EXIT_EMA_LENGTH, config.DUAL_ZONE_ANCHOR_EMA_LENGTH, config.DUAL_ZONE_TREND_EMA_LENGTH)
    if bars.is_empty() or bars.height < n: return None
    row = bars.row(-1, named=True); close = float(row["close"]); c = bars["close"].to_list()
    e7, e26, e99 = (ema_last(c, x) for x in (config.DUAL_ZONE_EXIT_EMA_LENGTH, config.DUAL_ZONE_ANCHOR_EMA_LENGTH, config.DUAL_ZONE_TREND_EMA_LENGTH))
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
    return {"schema_version": 1, "strategy_id": f"dual-zone{'-short' if not long else ''}-follower-v2", "asset": asset.upper(), "direction": direction, "setup_class": "dual_zone_follower" if long else "dual_zone_short_follower", "phase": f"channel_{ch.lower()}", "observed_at": observed.isoformat(), "valid_until": (observed+timedelta(minutes=5)).isoformat(), "horizon_minutes": 5, "confidence": 0.5, "confidence_status": "uncalibrated", "entry_condition": {"type": "limit_at_ema_context", "price": close}, "entry_price": close, "invalidation_price": stop, "targets": [target], "feature_snapshot": {"source_symbol": symbol, "execution_timeframe":"5m", "ema7":e7,"ema26":e26,"ema99":e99,"channel":ch,"entry_distance_pct":d26 if ch=="A" else d99}}

def _run(cutoff_id, snapshot, direction):
    cutoff = completed_cycle_for(snapshot.get("now"), "5m"); conn = config.get_db_connection(read_only=True, db_path=snapshot.get("market_db_path"))
    try:
        out=[]
        for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
            bars=load_bars_for_interval(conn, symbol, "5m", cutoff)
            adx_bars=load_bars_for_interval(conn, symbol, config.DUAL_ZONE_ADX_TIMEFRAME, cutoff)
            dmi=_dmi_adx(adx_bars, config.DUAL_ZONE_ADX_DI_LENGTH, config.DUAL_ZONE_ADX_SMOOTHING)
            if dmi and dmi[0] >= config.DUAL_ZONE_MIN_ADX and (not config.DUAL_ZONE_USE_DI_DIRECTION or (direction == "long" and dmi[1] > dmi[2]) or (direction == "short" and dmi[2] > dmi[1])):
                e=evaluate_symbol(bars, asset=asset, symbol=symbol, cutoff=cutoff, direction=direction)
            else: e=None
            if e: e["input_snapshot_id"] = cutoff_id; out.append(e)
        return out
    finally: conn.close()
def run_plugin(cutoff_id, snapshot): return _run(cutoff_id, snapshot, "long")
def run_short_plugin(cutoff_id, snapshot): return _run(cutoff_id, snapshot, "short")
