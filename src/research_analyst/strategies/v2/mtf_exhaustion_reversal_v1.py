"""Multi-timeframe exhaustion reversal using confirmed 4h RSI divergence."""
from __future__ import annotations

from datetime import timedelta, timezone

import config
from strategy_v2_context import (
    cutoff_from_id, evaluation_symbols, has_active_event, last_completed_bar_fresh,
    load_bars_for_interval, stoch_rsi, wilder_atr, wilder_rsi,
)
from strategies.v2.dual_zone_follower_v2 import _dmi_adx

STRATEGY_ID = "mtf-exhaustion-reversal-v1"
PLUGIN_VERSION = "v1"


def _confirmed_divergence(bars, rsi, lookback: int, direction: str) -> bool:
    if bars.height < lookback + 10:
        return False
    start = max(2, bars.height - lookback)
    lows, highs = [], []
    for index in range(start, bars.height - 2):
        low_window = [float(value) for value in bars["low"][index - 2:index + 3]]
        high_window = [float(value) for value in bars["high"][index - 2:index + 3]]
        if float(bars["low"][index]) == min(low_window) and rsi[index] is not None:
            lows.append(index)
        if float(bars["high"][index]) == max(high_window) and rsi[index] is not None:
            highs.append(index)
    if direction == "long" and len(lows) >= 2:
        first, second = lows[-2:]
        return float(bars["low"][second]) < float(bars["low"][first]) and rsi[second] > rsi[first]
    if direction == "short" and len(highs) >= 2:
        first, second = highs[-2:]
        return float(bars["high"][second]) > float(bars["high"][first]) and rsi[second] < rsi[first]
    return False


def _vwma(bars, length: int) -> float | None:
    if bars.height < length:
        return None
    tail = bars.tail(length)
    volume = sum(float(value) for value in tail["volume"].to_list())
    return sum(float(price) * float(vol) for price, vol in zip(tail["close"].to_list(), tail["volume"].to_list())) / volume if volume > 0 else None


def evaluate_symbol(bars5, bars1h, bars4h, bars15m, *, asset: str, symbol: str, cutoff) -> dict | None:
    if any(frame.is_empty() for frame in (bars5, bars1h, bars4h, bars15m)):
        return None
    if not last_completed_bar_fresh(bars5, cutoff) or bars5["timestamp"][-1] > cutoff:
        return None
    closes4 = [float(value) for value in bars4h["close"].to_list()]
    rsi4 = wilder_rsi(closes4, config.MTF_EXHAUSTION_RSI_LENGTH)
    dmi = _dmi_adx(bars1h, 14, 14)
    rsi1 = wilder_rsi([float(value) for value in bars1h["close"].to_list()], config.MTF_EXHAUSTION_RSI_LENGTH)
    raw, k, d = stoch_rsi([float(value) for value in bars5["close"].to_list()], 14, 14, 3, 3)
    if dmi is None or rsi1[-1] is None or any(value is None for value in (raw[-1], k[-1], k[-2], d[-1], d[-2])):
        return None
    row = bars5.row(-1, named=True)
    entry = float(row["close"])
    atr = wilder_atr(bars5, config.MTF_EXHAUSTION_ATR_LENGTH)
    if atr is None or atr <= 0:
        return None
    long_signal = _confirmed_divergence(bars4h, rsi4, config.MTF_EXHAUSTION_DIVERGENCE_LOOKBACK, "long") and rsi1[-1] < 30 and dmi[0] < config.MTF_EXHAUSTION_MAX_ADX and k[-2] <= d[-2] and k[-1] > d[-1] and k[-1] < 20
    short_signal = _confirmed_divergence(bars4h, rsi4, config.MTF_EXHAUSTION_DIVERGENCE_LOOKBACK, "short") and rsi1[-1] > 70 and dmi[0] < config.MTF_EXHAUSTION_MAX_ADX and k[-2] >= d[-2] and k[-1] < d[-1] and k[-1] > 80
    if not (long_signal or short_signal):
        return None
    direction = "long" if long_signal else "short"
    stop = entry - config.MTF_EXHAUSTION_ATR_STOP_MULTIPLIER * atr if direction == "long" else entry + config.MTF_EXHAUSTION_ATR_STOP_MULTIPLIER * atr
    vwap = _vwma(bars15m, 96)
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
    conn = config.get_db_connection(read_only=True, db_path=snapshot.get("market_db_path"))
    try:
        events = []
        for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
            event = evaluate_symbol(
                load_bars_for_interval(conn, symbol, "5m", cutoff),
                load_bars_for_interval(conn, symbol, "1h", cutoff),
                load_bars_for_interval(conn, symbol, "4h", cutoff),
                load_bars_for_interval(conn, symbol, "15m", cutoff),
                asset=asset, symbol=symbol, cutoff=cutoff,
            )
            if event and not has_active_event(STRATEGY_ID, asset, event["direction"], now=cutoff):
                event["input_snapshot_id"] = cutoff_id
                events.append(event)
        return events
    finally:
        conn.close()
