"""Multi-timeframe exhaustion reversal using confirmed 4h RSI divergence."""
from __future__ import annotations

from datetime import timedelta, timezone

import config
from polars_indicators import strict_pivot_indices, vwma_last
from strategy_features import build_feature_frame
from strategy_v2_context import (
    cutoff_from_id, evaluation_symbols, has_active_event, last_completed_bar_fresh,
    get_shared_computation_context, load_bars_for_interval, strategy_market_connection,
)
from strategies.v2.dual_zone_follower_v2 import _dmi_adx

STRATEGY_ID = "mtf-exhaustion-reversal-v1"
PLUGIN_VERSION = "v1"


def _confirmed_divergence(bars, rsi, lookback: int, direction: str) -> bool:
    if bars.height < lookback + 10:
        return False
    start = max(2, bars.height - lookback)
    pivot_highs, pivot_lows = strict_pivot_indices(bars, 2, 2, strict=False)
    lows = [index for index in pivot_lows
            if start <= index < bars.height - 2 and rsi[index] is not None]
    highs = [index for index in pivot_highs
             if start <= index < bars.height - 2 and rsi[index] is not None]
    if direction == "long" and len(lows) >= 2:
        first, second = lows[-2:]
        return float(bars["low"][second]) < float(bars["low"][first]) and rsi[second] > rsi[first]
    if direction == "short" and len(highs) >= 2:
        first, second = highs[-2:]
        return float(bars["high"][second]) > float(bars["high"][first]) and rsi[second] < rsi[first]
    return False


def _vwma(bars, length: int) -> float | None:
    return vwma_last(bars, length)


def evaluate_symbol(bars5, bars1h, bars4h, bars15m, *, asset: str, symbol: str, cutoff,
                    features4=None, features1=None, features5=None, features15=None) -> dict | None:
    if any(frame.is_empty() for frame in (bars5, bars1h, bars4h, bars15m)):
        return None
    if not last_completed_bar_fresh(bars5, cutoff) or bars5["timestamp"][-1] > cutoff:
        return None
    features4 = features4 if features4 is not None else build_feature_frame(
        bars4h, rsi={"rsi4": config.MTF_EXHAUSTION_RSI_LENGTH},
    )
    features1 = features1 if features1 is not None else build_feature_frame(
        bars1h, rsi={"rsi1": config.MTF_EXHAUSTION_RSI_LENGTH},
    )
    features5 = features5 if features5 is not None else build_feature_frame(
        bars5, stoch={"stoch": (14, 14, 3, 3)}, atr={"atr": config.MTF_EXHAUSTION_ATR_LENGTH},
    )
    features15 = features15 if features15 is not None else build_feature_frame(
        bars15m, vwma={"vwma": 96},
    )
    rsi4 = features4["rsi4"].to_list()
    dmi = _dmi_adx(bars1h, 14, 14, symbol=symbol, interval="1h")
    rsi1 = features1["rsi1"].to_list()
    raw, k, d = (features5[name].to_list() for name in ("stoch_raw", "stoch_k", "stoch_d"))
    if dmi is None or rsi1[-1] is None or any(value is None for value in (raw[-1], k[-1], k[-2], d[-1], d[-2])):
        return None
    row = bars5.row(-1, named=True)
    entry = float(row["close"])
    atr = features5["atr"][-1]
    if atr is None or atr <= 0:
        return None
    long_signal = _confirmed_divergence(bars4h, rsi4, config.MTF_EXHAUSTION_DIVERGENCE_LOOKBACK, "long") and rsi1[-1] < 30 and dmi[0] < config.MTF_EXHAUSTION_MAX_ADX and k[-2] <= d[-2] and k[-1] > d[-1] and k[-1] < 20
    short_signal = _confirmed_divergence(bars4h, rsi4, config.MTF_EXHAUSTION_DIVERGENCE_LOOKBACK, "short") and rsi1[-1] > 70 and dmi[0] < config.MTF_EXHAUSTION_MAX_ADX and k[-2] >= d[-2] and k[-1] < d[-1] and k[-1] > 80
    if not (long_signal or short_signal):
        return None
    direction = "long" if long_signal else "short"
    stop = entry - config.MTF_EXHAUSTION_ATR_STOP_MULTIPLIER * atr if direction == "long" else entry + config.MTF_EXHAUSTION_ATR_STOP_MULTIPLIER * atr
    vwap = features15["vwma"][-1]
    timestamp = row["timestamp"]
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return {
        "schema_version": 1, "strategy_id": STRATEGY_ID, "plugin_version": PLUGIN_VERSION,
        "asset": asset.upper(), "direction": direction, "setup_class": "mtf_exhaustion_reversal",
        "phase": "bullish_exhaustion" if direction == "long" else "bearish_exhaustion",
        "observed_at": timestamp.isoformat(), "valid_until": (timestamp + timedelta(minutes=5)).isoformat(),
        "horizon_minutes": 5, "confidence": 0.5, "confidence_status": "uncalibrated",
        "entry_condition": {"type": "market", "price": entry}, "entry_price": entry,
        "invalidation_price": stop,
        "feature_snapshot": {
            "source_symbol": symbol, "timeframe_provenance": "5m->15m/1h/4h",
            "rsi_4h": rsi4[-1], "rsi_1h": rsi1[-1], "adx_1h": dmi[0], "+di_1h": dmi[1], "-di_1h": dmi[2],
            "stochrsi_raw_5m": raw[-1], "stochrsi_k_5m": k[-1], "stochrsi_d_5m": d[-1],
            "atr16_5m": atr, "atr_stop_multiplier": config.MTF_EXHAUSTION_ATR_STOP_MULTIPLIER, "vwma_length": 96,
            "vwap_timeframe": "15m", "vwap_24h": vwap, "cutoff": cutoff.isoformat(),
        },
    }


def run_plugin(cutoff_id: str, snapshot: dict) -> list[dict]:
    cutoff = cutoff_from_id(str(snapshot.get("cutoff_at") or cutoff_id), snapshot.get("now"))
    conn, owns_conn = strategy_market_connection(snapshot.get("market_db_path"))
    try:
        events = []
        for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
            context = get_shared_computation_context()
            features4 = context.features(symbol, "4h", {"rsi": {"rsi4": config.MTF_EXHAUSTION_RSI_LENGTH}}) if context else None
            features1 = context.features(symbol, "1h", {"rsi": {"rsi1": config.MTF_EXHAUSTION_RSI_LENGTH}}) if context else None
            features5 = context.features(symbol, "5m", {"stoch": {"stoch": (14, 14, 3, 3)}, "atr": {"atr": config.MTF_EXHAUSTION_ATR_LENGTH}}) if context else None
            features15 = context.features(symbol, "15m", {"vwma": {"vwma": 96}}) if context else None
            event = evaluate_symbol(
                load_bars_for_interval(conn, symbol, "5m", cutoff),
                load_bars_for_interval(conn, symbol, "1h", cutoff),
                load_bars_for_interval(conn, symbol, "4h", cutoff),
                load_bars_for_interval(conn, symbol, "15m", cutoff),
                asset=asset, symbol=symbol, cutoff=cutoff,
                features4=features4, features1=features1,
                features5=features5, features15=features15,
            )
            if event and not has_active_event(STRATEGY_ID, asset, event["direction"], now=cutoff):
                event["input_snapshot_id"] = cutoff_id
                events.append(event)
        return events
    finally:
        if owns_conn:
            conn.close()
