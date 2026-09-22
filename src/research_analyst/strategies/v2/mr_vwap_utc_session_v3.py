"""MR VWAP UTC-session v3 port (ACTIVE): shared identity with RAHL v3.

Authority: research-analyst-hl/specs/mr-vwap-utc-session-v3.md + the RAHL v3
module (same directory name there). The ONLY delta vs v2 semantics is the
stop: setup extreme ∓ 1.5*ATR14(15m).

RA adaptation notes (deliberate, documented):
- Anchor is UTC-midnight session VWAP (HLC3/volume, population sigma),
  NOT the locked-v1 persistent volume-peak anchor.
- Confirmation follows the RAHL 3-bar session-bounded convention (RSI
  recovers + close back inside 2σ within setup..setup+2, same session),
  not locked-v1's rsi_then_price state machine. One shared ID ⇒ one
  behavior across producers.
- Entry reference is the confirmation-bar close (RA locked-v1 convention);
  RAHL uses next-15m-open. Geometry/target/RR gates are otherwise identical.
- ACTIVE since 2026-09-22 alongside mr-vwap-locked-v1 (distinct UTC-session
  anchor; same-direction clashes resolve deterministically).

Frame: 15m signals resampled from canonical completed 5m bars; 1h bars for
the |ΔVWAP(3h)/ATR14(1h)| < 0.25 same-session range gate.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

import polars as pl

import config
from strategy_v2_context import (
    cutoff_from_id,
    evaluation_symbols,
    has_active_event,
    load_bars_for_interval,
    resample_ohlcv,
    strategy_market_connection,
    wilder_rsi,
)

STRATEGY_ID = "mr-vwap-utc-session-v3"
PLUGIN_VERSION = "v3"

DAY_MS = 86_400_000
M15_MS = 900_000

STOP_ATR_MULTIPLE = float(getattr(config, "MR_VWAP_UTC_V3_ATR_BUFFER", 1.5))
RANGE_SLOPE = float(getattr(config, "MR_VWAP_UTC_V3_RANGE_SLOPE", 0.25))
MINIMUM_RR = float(getattr(config, "MR_VWAP_UTC_V3_MINIMUM_RR", 1.2))
RSI_LOWER = float(getattr(config, "MR_VWAP_UTC_V3_RSI_LOWER", 40.0))
RSI_UPPER = float(getattr(config, "MR_VWAP_UTC_V3_RSI_UPPER", 60.0))
TARGET_R_CAP = float(getattr(config, "MR_VWAP_UTC_V3_TARGET_R_CAP", 2.0))
ENTRY_VALIDITY_MINUTES = int(getattr(config, "MR_VWAP_UTC_V3_ENTRY_VALIDITY_MINUTES", 5))


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _session_start_ms(ms: int) -> int:
    return int(ms) - int(ms) % DAY_MS


def evaluate_symbol(bars5m, bars1h, *, asset: str, symbol: str, cutoff: datetime) -> dict | None:
    cutoff = _utc(cutoff)
    if bars5m.is_empty() or bars1h.is_empty():
        return None
    bars5m = bars5m.filter(bars5m["timestamp"] <= cutoff).sort("timestamp")
    bars15 = resample_ohlcv(bars5m, "15m")
    if bars15.is_empty() or bars15.height < 110:
        return None
    bars1h = bars1h.filter(bars1h["timestamp"] <= cutoff).sort("timestamp")

    rows = bars15.to_dicts()
    stamps = [_utc(r["timestamp"]) for r in rows]
    highs = [float(r["high"]) for r in rows]
    lows = [float(r["low"]) for r in rows]
    closes = [float(r["close"]) for r in rows]
    volumes = [float(r.get("volume") or 0.0) for r in rows]
    ms_starts = [int(s.timestamp() * 1000) for s in stamps]
    sessions = [_session_start_ms(m) for m in ms_starts]

    # Causal UTC-session VWAP + population sigma on HLC3.
    vwap: list[float | None] = [None] * len(rows)
    sigma: list[float | None] = [None] * len(rows)
    cum_v = cum_pv = cum_p2v = 0.0
    cur_session: int | None = None
    for i in range(len(rows)):
        if sessions[i] != cur_session:
            cur_session = sessions[i]
            cum_v = cum_pv = cum_p2v = 0.0
        price = (highs[i] + lows[i] + closes[i]) / 3.0
        cum_v += volumes[i]
        cum_pv += price * volumes[i]
        cum_p2v += price * price * volumes[i]
        if cum_v <= 0:
            continue
        mean = cum_pv / cum_v
        var = max(0.0, cum_p2v / cum_v - mean * mean)
        if not math.isfinite(mean) or not math.isfinite(var):
            continue
        vwap[i] = mean
        sigma[i] = math.sqrt(var)

    last = len(rows) - 1
    setup_idx = last - 1
    if sessions[last] != sessions[setup_idx]:
        return None
    vw, sg = vwap[last], sigma[last]
    if vw is None or sg is None or sg <= 0:
        return None

    rsi = wilder_rsi(closes, 14)
    from polars_indicators import wilder_atr_series

    atr15 = wilder_atr_series(bars15, 14).to_list()
    atr1h = wilder_atr_series(bars1h, 14).to_list()
    r, a = rsi[setup_idx], atr15[setup_idx]
    if r is None or a is None or a <= 0:
        return None

    # Same-session 3h range gate on completed-hour endpoints.
    ends1 = [_utc(v) for v in bars1h["timestamp"].to_list()]
    hour_end = cutoff.replace(minute=0, second=0, microsecond=0)
    now_idx = past_idx = None
    for i in range(len(stamps) - 1, -1, -1):
        if stamps[i] <= hour_end - timedelta(milliseconds=1) and now_idx is None:
            now_idx = i
        if stamps[i] <= hour_end - timedelta(hours=3, milliseconds=1) and past_idx is None:
            past_idx = i
        if now_idx is not None and past_idx is not None:
            break
    if now_idx is None or past_idx is None:
        return None
    if sessions[now_idx] != sessions[last] or sessions[past_idx] != sessions[last]:
        return None
    cur_vw, past_vw = vwap[now_idx], vwap[past_idx]
    if cur_vw is None or past_vw is None:
        return None
    import bisect

    ai = bisect.bisect_right(ends1, hour_end) - 1
    atr_h = atr1h[ai] if 0 <= ai < len(atr1h) else None
    if atr_h is None or atr_h <= 0:
        return None
    if abs((cur_vw - past_vw) / atr_h) >= RANGE_SLOPE:
        return None

    b_low, b_high = lows[setup_idx], highs[setup_idx]
    entry_ref = closes[last]
    for direction, touched in (
        ("long", b_low < vw - 2 * sg and r < RSI_LOWER),
        ("short", b_high > vw + 2 * sg and r > RSI_UPPER),
    ):
        if not touched:
            continue
        confirmed = False
        for j in range(max(0, setup_idx - 2), setup_idx + 1):
            if sessions[j] != sessions[last]:
                continue
            rj = rsi[j]
            if rj is None:
                continue
            inside = (direction == "long" and closes[j] > vw - 2 * sg) or (
                direction == "short" and closes[j] < vw + 2 * sg
            )
            recovered = (direction == "long" and rj > r) or (direction == "short" and rj < r)
            if inside and recovered:
                confirmed = True
        if not confirmed:
            continue
        extreme = b_low if direction == "long" else b_high
        stop = (extreme - STOP_ATR_MULTIPLE * a if direction == "long"
                else extreme + STOP_ATR_MULTIPLE * a)
        risk = abs(entry_ref - stop)
        if risk <= 0:
            continue
        target_vwap = vw
        target_2r = (entry_ref + TARGET_R_CAP * risk if direction == "long"
                     else entry_ref - TARGET_R_CAP * risk)
        target = (target_vwap if abs(target_vwap - entry_ref) < abs(target_2r - entry_ref)
                  else target_2r)
        if abs(target - entry_ref) / risk < MINIMUM_RR:
            continue
        if direction == "long" and not (stop < entry_ref < target):
            continue
        if direction == "short" and not (target < entry_ref < stop):
            continue
        observed = stamps[last]
        return {
            "schema_version": 1,
            "strategy_id": STRATEGY_ID,
            "plugin_version": PLUGIN_VERSION,
            "asset": asset.upper(),
            "direction": direction,
            "setup_class": "mr_vwap_band_reversion",
            "phase": "rsi_then_price_confirmation",
            "observed_at": observed.isoformat(),
            "valid_until": (observed + timedelta(minutes=ENTRY_VALIDITY_MINUTES)).isoformat(),
            "horizon_minutes": ENTRY_VALIDITY_MINUTES,
            "confidence": 0.5,
            "confidence_status": "uncalibrated",
            "entry_condition": {"type": "market_next_bar_open", "price": entry_ref},
            "entry_price": entry_ref,
            "invalidation_price": stop,
            "targets": [target],
            "metadata": {
                "execution_timeframe": "5m",
                "signal_timeframe": "15m",
                "entry_timing": "confirmation_close",
                "stop_policy": "setup_extreme_plus_1.5atr15_fixed",
                "target_policy": "vwap_or_2r_whichever_nearer",
                "vwap_mode": "utc_session",
                "vwap_contract_version": STRATEGY_ID,
            },
            "feature_snapshot": {
                "source_symbol": symbol,
                "signal_timeframe": "15m",
                "vwap": vw,
                "sigma": sg,
                "rsi14_15m": r,
                "atr14_15m": a,
                "stop": stop,
                "target": target,
                "minimum_rr": MINIMUM_RR,
                "cutoff": cutoff.isoformat(),
            },
        }
    return None


def run_plugin(cutoff_id: str, snapshot: dict) -> list[dict]:
    cutoff = cutoff_from_id(str(snapshot.get("cutoff_at") or cutoff_id), snapshot.get("now"))
    if cutoff.minute % 15:
        return []
    conn, owns_conn = strategy_market_connection(snapshot.get("market_db_path"))
    try:
        events = []
        for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
            bars5m = load_bars_for_interval(conn, symbol, "5m", cutoff)
            bars1h = load_bars_for_interval(conn, symbol, "1h", cutoff)
            event = evaluate_symbol(bars5m, bars1h, asset=asset, symbol=symbol, cutoff=cutoff)
            if event is not None and not has_active_event(STRATEGY_ID, asset, event["direction"], now=cutoff):
                event["input_snapshot_id"] = cutoff_id
                events.append(event)
        return events
    finally:
        if owns_conn:
            conn.close()
