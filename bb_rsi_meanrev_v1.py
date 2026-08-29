"""BB(30, 2) + RSI(13) mean-reversion strategy migrated as a plugin."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import config
from alpha_outbox import write_event
from strategy_v2_context import completed_cycle_for, last_completed_bar_fresh, load_bars_for_interval, list_candidate_symbols

STRATEGY_ID = "bb-rsi-meanrev-v1"
PLUGIN_VERSION = "v1"
SUPPORTED_ASSETS = frozenset(("BTC", "ETH", "PAXG", "QQQ"))

@dataclass(frozen=True)
class BBRsiMeanRevConfig:
    bb_length: int = 30
    bb_multiplier: float = 2.0
    rsi_length: int = 13
    skinny_ratio: float = 0.70
    atr_length: int = 14
    divergence_pivot: int = 5

def _rsi(closes: list[float], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= length: return out
    gain = loss = 0.0
    for i in range(1, length + 1):
        d = closes[i] - closes[i - 1]; gain += max(d, 0.0); loss += max(-d, 0.0)
    gain /= length; loss /= length; out[length] = 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)
    for i in range(length + 1, len(closes)):
        d = closes[i] - closes[i - 1]; gain = (gain * (length - 1) + max(d, 0.0)) / length; loss = (loss * (length - 1) + max(-d, 0.0)) / length
        out[i] = 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)
    return out

def _atr(bars, length: int) -> float:
    h, l, c = (bars[x].to_list() for x in ("high", "low", "close")); trs = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, len(c))]
    value = trs[0] if trs else 0.0; alpha = 1 / length
    for tr in trs[1:]: value = alpha * tr + (1 - alpha) * value
    return value

def _divergence(bars, rsi, pivot):
    l, h = bars["low"].to_list(), bars["high"].to_list(); lp, hp = [], []
    for i in range(pivot, len(l)-pivot):
        if l[i] == min(l[i-pivot:i+pivot+1]): lp.append(i)
        if h[i] == max(h[i-pivot:i+pivot+1]): hp.append(i)
    bull = bear = False
    if len(lp) >= 2:
        a, b = lp[-2:]; bull = l[b] < l[a] and rsi[a] is not None and rsi[b] is not None and rsi[b] > rsi[a] and rsi[b] < 25
    if len(hp) >= 2:
        a, b = hp[-2:]; bear = h[b] > h[a] and rsi[a] is not None and rsi[b] is not None and rsi[b] < rsi[a] and rsi[b] > 75
    return bull, bear

def evaluate_symbol(bars, *, asset, symbol, cutoff, cfg=None):
    cfg = cfg or BBRsiMeanRevConfig()
    if asset.upper() not in SUPPORTED_ASSETS or bars.is_empty() or bars.height < cfg.bb_length + cfg.divergence_pivot * 2 or not last_completed_bar_fresh(bars, cutoff): return None
    closes = [float(x) for x in bars["close"].to_list()]; w = closes[-cfg.bb_length:]; mid = sum(w) / cfg.bb_length; sd = (sum((x-mid)**2 for x in w) / cfg.bb_length) ** .5; lower, upper = mid - 2*sd, mid + 2*sd
    widths = []
    for i in range(cfg.bb_length-1, len(closes)):
        x = closes[i-cfg.bb_length+1:i+1]; m = sum(x)/cfg.bb_length; widths.append(4*(sum((v-m)**2 for v in x)/cfg.bb_length)**.5)
    if widths[-1] < sum(widths[-30:]) / min(30, len(widths)) * cfg.skinny_ratio: return None
    rsi = _rsi(closes, cfg.rsi_length); current = rsi[-1]
    if current is None: return None
    bull, bear = _divergence(bars, rsi, cfg.divergence_pivot); row = bars[-1]; entry = float(row["close"]); atr = _atr(bars, cfg.atr_length)
    long_signal, short_signal = (entry < lower and current < 25) or bull, (entry > upper and current > 75) or bear
    if not (long_signal or short_signal) or atr <= 0: return None
    direction = "long" if long_signal else "short"; stop = min(float(row["low"]), lower)-atr*.25 if direction == "long" else max(float(row["high"]), upper)+atr*.25; observed = row["timestamp"]
    if observed.tzinfo is None: observed = observed.replace(tzinfo=timezone.utc)
    return {"schema_version": 1, "strategy_id": STRATEGY_ID, "asset": asset.upper(), "direction": direction, "setup_class": "bb_rsi_mean_reversion", "phase": "band_extreme_or_divergence", "observed_at": observed.isoformat(), "valid_until": (observed+timedelta(minutes=5)).isoformat(), "horizon_minutes": 5, "confidence": .5, "confidence_status": "uncalibrated", "entry_condition": {"type": "market", "price": entry}, "invalidation_price": stop, "targets": [mid], "plugin_version": PLUGIN_VERSION, "feature_snapshot": {"source_symbol": symbol, "execution_timeframe": "5m", "bb_length": 30, "bb_multiplier": 2, "rsi_length": 13, "rsi": current, "lower_band": lower, "middle_band": mid, "upper_band": upper, "atr": atr, "bullish_divergence": bull, "bearish_divergence": bear}}

def run_plugin(cutoff_id, snapshot):
    conn = config.get_db_connection(read_only=True, db_path=snapshot.get("db_path")); emitted = []
    try:
        cutoff = completed_cycle_for(snapshot.get("now"), "5m")
        for symbol, asset in list_candidate_symbols(conn, cutoff):
            if asset.upper() not in SUPPORTED_ASSETS: continue
            event = evaluate_symbol(load_bars_for_interval(conn, symbol, "5m", cutoff), asset=asset, symbol=symbol, cutoff=cutoff)
            if event is not None:
                event["input_snapshot_id"] = cutoff_id; created, _ = write_event(event)
                if created: emitted.append(event)
        return emitted
    finally: conn.close()
