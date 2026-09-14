"""bb_locked_v1 engine-handoff port: BB-anticipation TP race (executor bracket).

Authority: repo-final/vectorbt/strategies/bb_stoch_tp_race_v1.py
(sha256 34f864c1...), spec engine_handoff/bb_locked_v1_engine_spec.md.

Self-contained plugin: indicators come exclusively from the repository's
native engines (``strategy_features.build_feature_frame`` /
``strategy_v2_context``), per the in-house indicator policy in AGENTS.md.
The 15m frame is resampled from canonical completed 5m bars; the regime gate
is the completed 1h EMA200 from regime-owned direct history.

Signal layer (long; short mirrored):
    - BB(20, 2.0, population std) upper-band envelope: close in
      [upper - 0.50*ATR16, upper + 0.25*ATR16], close above mid, mid rising
      over 3 candles.
    - StochRSI(14,14,3,3) bullish cross from the lower extreme: K.prev <= D.prev,
      K > D, K.prev <= 20 (native 0-100 axis; 0.20 on the backtest's 0-1 axis).
    - Regime: close > completed-1h EMA200.

The intrabar TP-race lifecycle is NOT replayed here. Per the handoff spec
(section 9.1) the race is delivered as executor bracket semantics: the plugin
freezes and emits the full trade state (stop, tp1, tp2, entry ATR48, latch and
arming contract) so the executor can place bracket orders that preserve
intrabar level fills. Lifecycle detail lives in metadata.bracket_spec and
specs/strategy-bb-tp-race-locked-v1.md.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from strategy_features import build_feature_frame
from strategy_v2_context import (
    cutoff_from_id,
    ema_series,
    evaluation_symbols,
    has_active_event,
    load_bars_for_interval,
    strategy_market_connection,
)

STRATEGY_ID = config.BB_TP_RACE_STRATEGY_ID
PLUGIN_VERSION = "v1"

# StochRSI extremes on the native 0-100 axis (backtest: 0.20 / 0.80 on 0-1).
_STOCH_EXTREME_SCALED = config.BB_TP_RACE_STOCH_EXTREME * 100.0


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _completed_1h_ema200(bars1h, exec_timestamps: list[datetime]) -> list[float | None]:
    """Completed-1h EMA200 mapped causally: a 15m bar sees the last closed 1h bar."""
    closes1h = [float(v) for v in bars1h["close"].to_list()]
    ema200 = ema_series(closes1h, config.BB_TP_RACE_TREND_EMA_PERIOD)
    ends = [_utc(v) for v in bars1h["timestamp"].to_list()]
    out: list[float | None] = []
    for stamp in exec_timestamps:
        index = bisect_right(ends, _utc(stamp)) - 1
        out.append(ema200[index] if index >= 0 else None)
    return out


def evaluate_symbol(bars15, bars1h, *, asset: str, symbol: str,
                    cutoff: datetime) -> dict | None:
    """Evaluate one completed 15m cutoff; return a bracket-carried intent."""
    cutoff = _utc(cutoff)
    if bars15.is_empty() or bars1h.is_empty():
        return None
    bars15 = bars15.filter(bars15["timestamp"] <= cutoff).sort("timestamp")
    bars1h = bars1h.filter(bars1h["timestamp"] <= cutoff).sort("timestamp")
    min_bars = max(
        config.BB_TP_RACE_BB_PERIOD,
        config.BB_TP_RACE_ATR_REFERENCE_PERIOD,
        config.BB_TP_RACE_STOCH_PERIOD + config.BB_TP_RACE_RSI_PERIOD,
        config.BB_TP_RACE_EMA_PERIOD,
    ) + 4
    if (bars15.height < min_bars + 3
            or not _fresh(bars15, cutoff, 15 * 60 + config.DATA_FRESHNESS_MAX_SECONDS)):
        return None

    closes = [float(v) for v in bars15["close"].to_list()]
    highs = [float(v) for v in bars15["high"].to_list()]
    lows = [float(v) for v in bars15["low"].to_list()]

    features = build_feature_frame(
        bars15,
        bollinger={"bb": (config.BB_TP_RACE_BB_PERIOD, config.BB_TP_RACE_BB_STD)},
        stoch={"stoch": (
            config.BB_TP_RACE_RSI_PERIOD,
            config.BB_TP_RACE_STOCH_PERIOD,
            config.BB_TP_RACE_K_PERIOD,
            config.BB_TP_RACE_D_PERIOD,
        )},
        atr={
            "atr16": config.BB_TP_RACE_ATR_STOP_PERIOD,
            "atr48": config.BB_TP_RACE_ATR_REFERENCE_PERIOD,
        },
        ema={"ema7": config.BB_TP_RACE_EMA_PERIOD},
    )
    mid = features["bb_middle"].to_list()
    upper = features["bb_upper"].to_list()
    lower = features["bb_lower"].to_list()
    atr16_series = features["atr16"].to_list()
    atr48_series = features["atr48"].to_list()
    ema7_series = features["ema7"].to_list()
    k_series = features["stoch_k"].to_list()
    d_series = features["stoch_d"].to_list()

    exec_timestamps = [_utc(v) for v in bars15["timestamp"].to_list()]
    ema200_on_15m = _completed_1h_ema200(bars1h, exec_timestamps)

    last = len(closes) - 1
    if any(series[last] is None for series in (mid, upper, lower, atr16_series,
                                               atr48_series, ema7_series)):
        return None
    if last < 3 or mid[last - 3] is None:
        return None
    k_prev, d_prev = k_series[last - 1], d_series[last - 1]
    k_curr, d_curr = k_series[last], d_series[last]
    if None in (k_prev, d_prev, k_curr, d_curr):
        return None
    atr16 = atr16_series[last]
    if not atr16 or atr16 <= 0:
        return None
    close = closes[last]
    regime_value = ema200_on_15m[last]
    if regime_value is None or regime_value <= 0:
        return None

    bull_cross = (k_prev <= d_prev and k_curr > d_curr
                  and k_prev <= _STOCH_EXTREME_SCALED)
    bear_cross = (k_prev >= d_prev and k_curr < d_curr
                  and k_prev >= 100.0 - _STOCH_EXTREME_SCALED)
    mid_rising = mid[last] > mid[last - 3]
    mid_falling = mid[last] < mid[last - 3]
    near_upper = (
        close >= upper[last] - config.BB_TP_RACE_NEAR_BAND_ATR * atr16
        and close <= upper[last] + config.BB_TP_RACE_MAX_BAND_OVERSHOOT_ATR * atr16
    )
    near_lower = (
        close <= lower[last] + config.BB_TP_RACE_NEAR_BAND_ATR * atr16
        and close >= lower[last] - config.BB_TP_RACE_MAX_BAND_OVERSHOOT_ATR * atr16
    )
    trend_long_ok = close > regime_value
    trend_short_ok = close < regime_value

    raw_long = near_upper and mid_rising and close > mid[last] and bull_cross and trend_long_ok
    raw_short = near_lower and mid_falling and close < mid[last] and bear_cross and trend_short_ok
    if not (raw_long or raw_short):
        return None

    direction = "long" if raw_long else "short"
    entry_ref = close  # fill happens at the NEXT 15m open (executor market order)
    stop = (lows[last] - config.BB_TP_RACE_STOP_ATR_MULT * atr16 if direction == "long"
            else highs[last] + config.BB_TP_RACE_STOP_ATR_MULT * atr16)
    entry_atr48 = atr48_series[last]
    if not entry_atr48 or entry_atr48 <= 0 or entry_ref <= 0:
        return None
    if direction == "long" and stop >= entry_ref:
        return None
    if direction == "short" and stop <= entry_ref:
        return None
    tp1 = entry_ref + config.BB_TP_RACE_TP1_ATR48 * entry_atr48 if direction == "long" \
        else entry_ref - config.BB_TP_RACE_TP1_ATR48 * entry_atr48
    tp2 = entry_ref + config.BB_TP_RACE_TP2_ATR48 * entry_atr48 if direction == "long" \
        else entry_ref - config.BB_TP_RACE_TP2_ATR48 * entry_atr48

    observed = exec_timestamps[last]
    return {
        "schema_version": 1,
        "strategy_id": STRATEGY_ID,
        "plugin_version": PLUGIN_VERSION,
        "asset": asset.upper(),
        "direction": direction,
        "setup_class": "bb_tp_race_locked",
        "phase": "band_proximity_stoch_cross",
        "observed_at": observed.isoformat(),
        "valid_until": (observed + timedelta(minutes=config.BB_TP_RACE_ENTRY_VALIDITY_MINUTES)).isoformat(),
        "horizon_minutes": config.BB_TP_RACE_ENTRY_VALIDITY_MINUTES,
        "confidence": 0.5,
        "confidence_status": "uncalibrated",
        "entry_condition": {"type": "market_next_bar_open", "price": entry_ref},
        "entry_price": entry_ref,
        "invalidation_price": stop,
        "targets": [tp1, tp2],
        "metadata": {
            "execution_timeframe": "15m",
            "signal_timeframe": "15m",
            "regime_timeframe": "1h",
            "entry_timing": "next_completed_15m_open",
            "exit_policy": "executor_bracket_tp_race",
            "target_policy": "bracket_tp1_tp2_race",
            "stop_policy": "frozen_signal_candle_atr16",
            "stop_scope": "hard_stop_only_no_breakeven_no_trail",
            "bracket_spec": {
                "version": "bb_locked_v1",
                "stop": stop,
                "tp1": tp1,
                "tp2": tp2,
                "entry_atr48": entry_atr48,
                "atr16": atr16,
                "bep_arm_atr16": config.BB_TP_RACE_BEP_ARM_ATR16,
                "stoch_extreme_scaled": _STOCH_EXTREME_SCALED,
                "tp3_rule": (
                    "on each completed-candle StochRSI %K extreme close "
                    f"(K >= {100.0 - _STOCH_EXTREME_SCALED:.1f} or K <= "
                    f"{_STOCH_EXTREME_SCALED:.1f} on the 0-100 axis, either side), "
                    "re-freeze tp3 = EMA7[extreme] ± "
                    f"{config.BB_TP_RACE_TP3_EMA_ATR_MULT} * ATR16[extreme] anchored to position direction; "
                    "arm exactly ONE candle of intrabar race {BEP(after 1*ATR16 touch), tp3, tp2, tp1}; "
                    "nearest touched level wins, gap fills at open; shadow-BEP dip exit on armed candle "
                    "when BEP not yet raceable; race consumed if untouched"
                ),
                "stop_semantics": "live intrabar every bar, gap-through fills at open",
                "race_semantics": "intrabar level fills at the level, open when gapped past",
                "suppressed_on_entry_candle": True,
            },
        },
        "feature_snapshot": {
            "source_symbol": symbol,
            "execution_timeframe": "15m",
            "bb_period": config.BB_TP_RACE_BB_PERIOD,
            "bb_std": config.BB_TP_RACE_BB_STD,
            "bb_mid": mid[last],
            "bb_upper": upper[last],
            "bb_lower": lower[last],
            "stochrsi_k": k_curr,
            "stochrsi_d": d_curr,
            "stochrsi_k_prev": k_prev,
            "stochrsi_extreme_0_100": _STOCH_EXTREME_SCALED,
            "atr16_15m": atr16,
            "atr48_15m": entry_atr48,
            "ema7_15m": ema7_series[last],
            "ema200_1h": regime_value,
            "stop": stop,
            "tp1": tp1,
            "tp2": tp2,
            "cutoff": cutoff.isoformat(),
        },
    }


def _fresh(bars, cutoff: datetime, max_age_seconds: float) -> bool:
    latest = _utc(bars["timestamp"][-1])
    age = (cutoff - latest).total_seconds()
    return 0 <= age <= max_age_seconds


def run_plugin(cutoff_id: str, snapshot: dict) -> list[dict]:
    cutoff = cutoff_from_id(str(snapshot.get("cutoff_at") or cutoff_id), snapshot.get("now"))
    conn, owns_conn = strategy_market_connection(snapshot.get("market_db_path"))
    try:
        events = []
        for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
            bars15 = load_bars_for_interval(conn, symbol, "15m", cutoff)
            bars1h = load_bars_for_interval(conn, symbol, "1h", cutoff)
            event = evaluate_symbol(bars15, bars1h, asset=asset, symbol=symbol, cutoff=cutoff)
            if event is not None and not has_active_event(STRATEGY_ID, asset, event["direction"], now=cutoff):
                event["input_snapshot_id"] = cutoff_id
                events.append(event)
        return events
    finally:
        if owns_conn:
            conn.close()
