"""Non-repainting EMA stack and StochRSI trigger on completed 5m bars."""
from datetime import timedelta, timezone
import config
from strategy_v2_context import cutoff_from_id, ema_last, atr_last, evaluation_symbols, has_active_event, load_bars_for_interval, stoch_rsi, wilder_rsi
from strategies.v2.dual_zone_follower_v2 import _dmi_adx

def _rsi(values, n=14):
    return wilder_rsi(values, n)

def _stoch(values, stoch_len, k_smooth, d_smooth):
    return stoch_rsi(values, 14, stoch_len, k_smooth, d_smooth)
def evaluate_symbol(bars, trend, strength, *, asset, symbol, cutoff, direction):
    if bars.height < 220 or trend.height < 200 or strength.height < 30: return None
    c=bars["close"].to_list(); tc=trend["close"].to_list(); e=[ema_last(tc,n) for n in (20,50,100,200)]; atr=atr_last(bars,14); e200=ema_last(c,200)
    dmi = _dmi_adx(strength, 14, 14)
    if not all(x and x>0 for x in e) or not atr or not e200 or (getattr(config, "EMA_STACK_USE_ADX", True) and (dmi is None or dmi[0] < getattr(config, "EMA_STACK_MIN_ADX", 20.0))): return None
    long=direction=="long"; stack=e[0]>e[1]>e[2]>e[3] if long else e[0]<e[1]<e[2]<e[3]
    if not stack or abs(e[0]-e[3])/e[3]>=.01: return None
    raw_values, k_values, d_values=_stoch(c,14,3,3)
    if any(value is None for value in (k_values[-1], d_values[-1], k_values[-2], d_values[-2])): return None
    k, d, pk, pd=k_values[-1], d_values[-1], k_values[-2], d_values[-2]
    if long and not (k <= 20 and d <= 20 and pk <= pd and k > d): return None
    if not long and not (k >= 80 and d >= 80 and pk >= pd and k < d): return None
    stop=e200-(1.5*atr if long else -1.5*atr); entry=float(c[-1]); risk=entry-stop
    if risk<=0: return None
    risk_abs=abs(risk); target=entry+2*risk_abs if long else entry-2*risk_abs; ts=bars["timestamp"][-1]; ts=ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
    return {"schema_version":1,"strategy_id":"ema-stack-15m-adx-stochrsi-5m-v1","asset":asset.upper(),"direction":direction,"setup_class":"ema_stack_adx_stochrsi","phase":"long_trigger" if long else "short_trigger","observed_at":ts.isoformat(),"valid_until":(ts+timedelta(minutes=5)).isoformat(),"horizon_minutes":5,"confidence":0.5,"confidence_status":"uncalibrated","entry_condition":{"type":"market","price":entry},"entry_price":entry,"invalidation_price":stop,"targets":[target],"feature_snapshot":{"source_symbol":symbol,"ema20_15m":e[0],"ema50_15m":e[1],"ema100_15m":e[2],"ema200_15m":e[3],"spread_pct":abs(e[0]-e[3])/e[3]*100,"rsi":_rsi(c)[-1],"stochrsi_raw":raw_values[-1],"stochrsi_k":k,"stochrsi_d":d,"ema200_5m":e200,"atr14_5m":atr,"cutoff":cutoff.isoformat()}}
def run_plugin(cutoff_id,snapshot):
    cutoff=cutoff_from_id(str(snapshot.get("cutoff_at") or cutoff_id), snapshot.get("now")); conn=config.get_db_connection(read_only=True,db_path=snapshot.get("market_db_path"))
    try:
        out=[]
        for symbol, a in evaluation_symbols(conn, cutoff, snapshot):
            b=load_bars_for_interval(conn,symbol,"5m",cutoff); t=load_bars_for_interval(conn,symbol,"15m",cutoff); h=load_bars_for_interval(conn,symbol,"1h",cutoff)
            dmi=_dmi_adx(h,14,14)
            e=(evaluate_symbol(b,t,h,asset=a,symbol=symbol,cutoff=cutoff,direction="long") or evaluate_symbol(b,t,h,asset=a,symbol=symbol,cutoff=cutoff,direction="short")) if dmi and dmi[0] >= 20 else None
            if e and not has_active_event(e["strategy_id"], a, e["direction"], now=cutoff):
                e["input_snapshot_id"]=cutoff_id
                e.setdefault("feature_snapshot", {}).update({"adx_1h": dmi[0], "+di_1h": dmi[1], "-di_1h": dmi[2], "cutoff": cutoff.isoformat()})
                out.append(e)
        return out
    finally: conn.close()
