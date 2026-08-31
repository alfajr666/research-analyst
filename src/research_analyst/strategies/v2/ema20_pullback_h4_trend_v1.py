"""EMA20 pullback with completed 4h trend confirmation."""
from datetime import timedelta, timezone
import config
from strategy_v2_context import completed_cycle_for, ema_last, atr_last, load_bars_for_interval

def evaluate_symbol(bars, bars4, *, asset, symbol, cutoff, direction):
    if bars.height < 25 or bars4.height < 200: return None
    r=bars.row(-1,named=True); p=bars.row(-2,named=True); close=float(r["close"]); e20=ema_last(bars["close"].to_list(),20); atr=atr_last(bars,14)
    e50=ema_last(bars4["close"].to_list(),50); e200=ema_last(bars4["close"].to_list(),200)
    if not all(x and x>0 for x in (e20,atr,e50,e200)): return None
    long=direction=="long"; regime=float(bars4["close"][-1])>e200 and e50>e200 if long else float(bars4["close"][-1])<e200 and e50<e200
    pattern=float(r["low"])<=e20 and close>e20 and close>float(r["open"]) and float(p["close"])<float(p["open"]) and close>=float(p["open"]) if long else float(r["high"])>=e20 and close<e20 and close<float(r["open"]) and float(p["close"])>float(p["open"]) and close<=float(p["open"])
    if not regime or not pattern: return None
    swing=min(bars["low"].tail(10).to_list()) if long else max(bars["high"].tail(10).to_list()); stop=swing-atr if long else swing+atr; target=close+2*(close-stop) if long else close-2*(stop-close)
    ts=r["timestamp"].replace(tzinfo=timezone.utc) if r["timestamp"].tzinfo is None else r["timestamp"]
    return {"schema_version":1,"strategy_id":"ema20-pullback-h4-trend-v1","asset":asset.upper(),"direction":direction,"setup_class":"ema20_pullback_h4_trend","phase":"long_pullback" if long else "short_pullback","observed_at":ts.isoformat(),"valid_until":(ts+timedelta(minutes=5)).isoformat(),"entry_price":close,"invalidation_price":stop,"targets":[target],"confidence_status":"uncalibrated","feature_snapshot":{"source_symbol":symbol,"ema20_1h":e20,"atr14_1h":atr,"close_4h":float(bars4["close"][-1]),"ema50_4h":e50,"ema200_4h":e200,"swing_extreme":swing}}
def run_plugin(cutoff_id,snapshot):
    cutoff=completed_cycle_for(snapshot.get("now"),"5m"); conn=config.get_db_connection(read_only=True,db_path=snapshot.get("market_db_path"))
    try:
        out=[]
        for a in config.load_static_symbols():
            b=load_bars_for_interval(conn,a,"1h",cutoff); h=load_bars_for_interval(conn,a,"4h",cutoff); e= evaluate_symbol(b,h,asset=a,symbol=a,cutoff=cutoff,direction="long") or evaluate_symbol(b,h,asset=a,symbol=a,cutoff=cutoff,direction="short")
            if e: e["input_snapshot_id"]=cutoff_id; out.append(e)
        return out
    finally: conn.close()
