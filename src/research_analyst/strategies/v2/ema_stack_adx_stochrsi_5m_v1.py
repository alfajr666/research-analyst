"""Non-repainting EMA stack and StochRSI trigger on completed 5m bars."""
from datetime import timedelta, timezone
import config
from strategy_v2_context import completed_cycle_for, ema_last, atr_last, load_bars_for_interval
from strategies.v2.dual_zone_follower_v2 import _dmi_adx

def _rsi(values, n=14):
    if len(values) <= n: return []
    out=[]
    for end in range(n, len(values)):
        changes=[values[i]-values[i-1] for i in range(end-n+1, end+1)]
        gain=sum(max(0, x) for x in changes)/n; loss=sum(max(0, -x) for x in changes)/n
        out.append(100 if not loss else 100-100/(1+gain/loss))
    return out

def _stoch(values, stoch_len, k_smooth, d_smooth):
    raw=[]
    for i in range(stoch_len-1, len(values)):
        window=values[i-stoch_len+1:i+1]; lo=min(window); hi=max(window)
        raw.append(0.0 if hi == lo else 100*(values[i]-lo)/(hi-lo))
    k=[sum(raw[i-k_smooth+1:i+1])/k_smooth for i in range(k_smooth-1, len(raw))]
    d=[sum(k[i-d_smooth+1:i+1])/d_smooth for i in range(d_smooth-1, len(k))]
    return raw, k, d
def evaluate_symbol(bars, trend, strength, *, asset, symbol, cutoff, direction):
    if bars.height < 220 or trend.height < 200 or strength.height < 30: return None
    c=bars["close"].to_list(); tc=trend["close"].to_list(); e=[ema_last(tc,n) for n in (20,50,100,200)]; atr=atr_last(bars,14); e200=ema_last(c,200)
    if not all(x and x>0 for x in e) or not atr or not e200: return None
    long=direction=="long"; stack=e[0]>e[1]>e[2]>e[3] if long else e[0]<e[1]<e[2]<e[3]
    if not stack or abs(e[0]-e[3])/e[3]>=.01: return None
    rsi=_rsi(c); raw_values, k_values, d_values=_stoch(rsi,14,3,3)
    if len(k_values) < 2 or len(d_values) < 2: return None
    k, d, pk, pd=k_values[-1], d_values[-1], k_values[-2], d_values[-2]
    if long and not (k <= 20 and d <= 20 and pk <= pd and k > d): return None
    if not long and not (k >= 80 and d >= 80 and pk >= pd and k < d): return None
    stop=e200-(1.5*atr if long else -1.5*atr); entry=float(c[-1]); risk=entry-stop
    if risk<=0: return None
    risk_abs=abs(risk); target=entry+2*risk_abs if long else entry-2*risk_abs; ts=bars["timestamp"][-1]; ts=ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
    return {"schema_version":1,"strategy_id":"ema-stack-15m-adx-stochrsi-5m-v1","asset":asset.upper(),"direction":direction,"setup_class":"ema_stack_adx_stochrsi","phase":"long_trigger" if long else "short_trigger","observed_at":ts.isoformat(),"valid_until":(ts+timedelta(minutes=5)).isoformat(),"entry_price":entry,"invalidation_price":stop,"targets":[target],"confidence_status":"uncalibrated","feature_snapshot":{"source_symbol":symbol,"ema20_15m":e[0],"ema50_15m":e[1],"ema100_15m":e[2],"ema200_15m":e[3],"spread_pct":abs(e[0]-e[3])/e[3]*100,"rsi":rsi[-1] if rsi else None,"stochrsi_k":k,"stochrsi_d":d,"ema200_5m":e200,"atr14_5m":atr}}
def run_plugin(cutoff_id,snapshot):
    cutoff=completed_cycle_for(snapshot.get("now"),"5m"); conn=config.get_db_connection(read_only=True,db_path=snapshot.get("market_db_path"))
    try:
        out=[]
        for a in config.load_static_symbols():
            b=load_bars_for_interval(conn,a,"5m",cutoff); t=load_bars_for_interval(conn,a,"15m",cutoff); h=load_bars_for_interval(conn,a,"1h",cutoff)
            dmi=_dmi_adx(h,14,14)
            e=(evaluate_symbol(b,t,h,asset=a,symbol=a,cutoff=cutoff,direction="long") or evaluate_symbol(b,t,h,asset=a,symbol=a,cutoff=cutoff,direction="short")) if dmi and dmi[0] >= 20 else None
            if e: e["input_snapshot_id"]=cutoff_id; out.append(e)
        return out
    finally: conn.close()
