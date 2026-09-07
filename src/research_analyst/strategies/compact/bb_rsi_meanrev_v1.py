"""BB(30, 2) + RSI(13) mean-reversion strategy migrated as a plugin."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import config
import polars as pl
from strategy_features import build_feature_frame
from strategy_v2_context import (
    cutoff_from_id, evaluation_symbols, has_active_event, last_completed_bar_fresh,
    get_shared_computation_context, load_bars_for_interval, strategy_market_connection, stoch_rsi, wilder_atr, wilder_rsi,
)

STRATEGY_ID = "bb-rsi-meanrev-v1"
PLUGIN_VERSION = "v1"

@dataclass(frozen=True)
class BBRsiMeanRevConfig:
    bb_length: int = 30
    bb_multiplier: float = 2.0
    rsi_length: int = 13
    skinny_ratio: float = 0.70
    atr_length: int = 14
    divergence_pivot: int = 5

def _rsi(closes: list[float], length: int) -> list[float | None]:
    return wilder_rsi(closes, length)

def _atr(bars, length: int) -> float:
    return float(wilder_atr(bars, length) or 0.0)

def _divergence(bars, rsi, pivot):
    window = pivot * 2 + 1
    pivots = bars.with_row_index("i").with_columns(
        pl.col("low").rolling_min(window, min_samples=window).shift(-pivot).alias("pivot_low"),
        pl.col("high").rolling_max(window, min_samples=window).shift(-pivot).alias("pivot_high"),
    )
    lp = [int(i) for i in pivots.filter(pl.col("low") == pl.col("pivot_low"))["i"].to_list()]
    hp = [int(i) for i in pivots.filter(pl.col("high") == pl.col("pivot_high"))["i"].to_list()]
    lp = [i for i in lp if rsi[i] is not None]
    hp = [i for i in hp if rsi[i] is not None]
    l, h = bars["low"].to_list(), bars["high"].to_list()
    bull = bear = False
    if len(lp) >= 2:
        a, b = lp[-2:]; bull = l[b] < l[a] and rsi[a] is not None and rsi[b] is not None and rsi[b] > rsi[a] and rsi[b] < 25
    if len(hp) >= 2:
        a, b = hp[-2:]; bear = h[b] > h[a] and rsi[a] is not None and rsi[b] is not None and rsi[b] < rsi[a] and rsi[b] > 75
    return bull, bear

def evaluate_symbol(bars, *, asset, symbol, cutoff, cfg=None, features=None):
    cfg = cfg or BBRsiMeanRevConfig()
    if bars.is_empty() or bars.height < cfg.bb_length + cfg.divergence_pivot * 2 or not last_completed_bar_fresh(bars, cutoff): return None
    if bars["timestamp"][-1] > cutoff: return None
    features = features if features is not None else build_feature_frame(
        bars,
        rsi={"rsi": cfg.rsi_length},
        atr={"atr": cfg.atr_length},
        bollinger={"bb": (cfg.bb_length, cfg.bb_multiplier)},
    )
    row = features.row(-1, named=True)
    mid, lower, upper = row.get("bb_middle"), row.get("bb_lower"), row.get("bb_upper")
    band_width = row.get("bb_width")
    widths = features["bb_width"].drop_nulls().tail(30)
    if band_width is None or widths.len() == 0 or band_width < float(widths.mean()) * cfg.skinny_ratio: return None
    closes = bars["close"].to_list()
    rsi = features["rsi"].to_list(); current = rsi[-1]
    if current is None: return None
    bull, bear = _divergence(bars, rsi, cfg.divergence_pivot); row = bars.row(-1, named=True); entry = float(row["close"]); atr = float(features["atr"][-1] or 0.0)
    long_signal, short_signal = (entry < lower and current < 25) or bull, (entry > upper and current > 75) or bear
    if not (long_signal or short_signal) or atr <= 0: return None
    direction = "long" if long_signal else "short"; stop = min(float(row["low"]), lower)-atr*.25 if direction == "long" else max(float(row["high"]), upper)+atr*.25; observed = row["timestamp"]
    if observed.tzinfo is None: observed = observed.replace(tzinfo=timezone.utc)
    return {"schema_version": 1, "strategy_id": STRATEGY_ID, "asset": asset.upper(), "direction": direction, "setup_class": "bb_rsi_mean_reversion", "phase": "band_extreme_or_divergence", "observed_at": observed.isoformat(), "valid_until": (observed+timedelta(minutes=5)).isoformat(), "horizon_minutes": 5, "confidence": .5, "confidence_status": "uncalibrated", "entry_condition": {"type": "market", "price": entry}, "invalidation_price": stop, "targets": [mid], "plugin_version": PLUGIN_VERSION, "feature_snapshot": {"source_symbol": symbol, "execution_timeframe": "5m", "bb_length": cfg.bb_length, "bb_multiplier": cfg.bb_multiplier, "bb_width": band_width, "rsi_length": cfg.rsi_length, "rsi": current, "lower_band": lower, "middle_band": mid, "upper_band": upper, "atr": atr, "bullish_divergence": bull, "bearish_divergence": bear, "cutoff": cutoff.isoformat()}}

def run_plugin(cutoff_id, snapshot):
    conn, owns_conn = strategy_market_connection(snapshot.get("market_db_path")); emitted = []
    try:
        cutoff = cutoff_from_id(str(snapshot.get("cutoff_at") or cutoff_id), snapshot.get("now"))
        for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
            bars = load_bars_for_interval(conn, symbol, "5m", cutoff)
            context = get_shared_computation_context()
            features = context.features(
                symbol, "5m",
                {
                    "rsi": {"rsi": BBRsiMeanRevConfig.rsi_length},
                    "atr": {"atr": BBRsiMeanRevConfig.atr_length},
                    "bollinger": {"bb": (BBRsiMeanRevConfig.bb_length, BBRsiMeanRevConfig.bb_multiplier)},
                },
            ) if context else None
            event = evaluate_symbol(bars, asset=asset, symbol=symbol, cutoff=cutoff, features=features)
            if event is not None and (not event.get("direction") or not has_active_event(STRATEGY_ID, asset, event["direction"], now=cutoff)):
                event["input_snapshot_id"] = cutoff_id; emitted.append(event)
        return emitted
    finally:
        if owns_conn:
            conn.close()
