"""FVG and Order Block zone detection per data-platform-strategy-plugins spec.

Computes on resampled 1h/4h bars from CoinAnalyze data.
Zones are advisory (support/neutral/contradict/unavailable).
Each snapshot keeps at most the 3 most recent active zones per asset/tf/dir/type.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

import polars as pl


def compute_atr(df: pl.DataFrame, period: int = 14) -> float:
    if df.height < 2:
        return 1.0
    df = df.with_columns([
        (pl.col("high") - pl.col("low")).alias("tr1"),
        (pl.col("high") - pl.col("close").shift(1)).abs().alias("tr2"),
        (pl.col("low") - pl.col("close").shift(1)).abs().alias("tr3"),
    ])
    df = df.with_columns(pl.max_horizontal("tr1", "tr2", "tr3").alias("tr"))
    atr = df["tr"].tail(period).mean()
    return float(atr) if atr and atr > 0 else 1.0


def _resample_to_higher(df: pl.DataFrame, every: str = "1h") -> pl.DataFrame:
    if df.is_empty():
        return df
    from strategy_v2_context import resample_ohlcv
    return resample_ohlcv(df, every)


def detect_fvg(bars: pl.DataFrame, atr: float | None = None, min_gap_mult: float = 0.25, tf: str = "1h") -> List[Dict[str, Any]]:
    """Detect FVGs on the provided bars (assumed closed bars, sorted)."""
    if bars.height < 3:
        return []
    if atr is None:
        atr = compute_atr(bars)
    if atr is None or atr <= 0:
        return []
    min_gap = min_gap_mult * atr
    bars = bars.sort("timestamp")
    fvgs: List[Dict] = []
    rows = bars.to_dicts()

    def evidence_for(items: List[Dict]) -> List[str]:
        return sorted({
            str(item_id)
            for item in items
            for item_id in item.get("source_observation_ids", [])
            if item_id
        })

    for i in range(2, len(rows)):
        prev_high = float(rows[i-2]["high"])
        curr_low = float(rows[i]["low"])
        gap = curr_low - prev_high
        if gap > min_gap:
            fvgs.append({
                "type": "fvg",
                "direction": "bullish",
                "timeframe": tf,
                "created_index": i,
                "source_evidence_ids": evidence_for(rows[i-2:i+1]),
                "start": rows[i-2]["timestamp"],
                "end": rows[i]["timestamp"],
                "low": prev_high,
                "high": curr_low,
                "gap": gap,
                "state": "active",
                "created_at": rows[i]["timestamp"],
            })
        prev_low = float(rows[i-2]["low"])
        curr_high = float(rows[i]["high"])
        gap = prev_low - curr_high
        if gap > min_gap:
            fvgs.append({
                "type": "fvg",
                "direction": "bearish",
                "timeframe": tf,
                "created_index": i,
                "source_evidence_ids": evidence_for(rows[i-2:i+1]),
                "start": rows[i-2]["timestamp"],
                "end": rows[i]["timestamp"],
                "low": curr_high,
                "high": prev_low,
                "gap": gap,
                "state": "active",
                "created_at": rows[i]["timestamp"],
            })
    # Lifecycle is evaluated only by bars after the FVG exists. A future bar
    # cannot retroactively change the state of a zone before its creation.
    for f in fvgs:
        for j in range(f["created_index"] + 1, len(rows)):
            b = rows[j]
            blo, bhi, bcl = float(b["low"]), float(b["high"]), float(b["close"])
            if f["direction"] == "bullish":
                touched = blo <= f["high"] and bhi >= f["low"]
                if bcl < f["low"]:
                    f["state"] = "invalidated"
                    f["invalidated_at"] = b["timestamp"]
                    break
                if blo <= f["low"]:
                    f["state"] = "filled"
                    f["filled_at"] = b["timestamp"]
                    break
                if touched and f["state"] == "active":
                    f["state"] = "partial"
                    f["first_mitigated_at"] = b["timestamp"]
            else:
                touched = bhi >= f["low"] and blo <= f["high"]
                if bcl > f["high"]:
                    f["state"] = "invalidated"
                    f["invalidated_at"] = b["timestamp"]
                    break
                if bhi >= f["high"]:
                    f["state"] = "filled"
                    f["filled_at"] = b["timestamp"]
                    break
                if touched and f["state"] == "active":
                    f["state"] = "partial"
                    f["first_mitigated_at"] = b["timestamp"]
    return fvgs


def detect_order_blocks(bars: pl.DataFrame, atr: float | None = None, swing_lookback: int = 20, tf: str = "1h") -> List[Dict[str, Any]]:
    if bars.height < swing_lookback + 2:
        return []
    if atr is None:
        atr = compute_atr(bars)
    if atr is None or atr <= 0:
        return []
    bars = bars.sort("timestamp")
    min_disp = 1.5 * atr
    rows = bars.to_dicts()
    obs: List[Dict] = []
    for i in range(swing_lookback , len(rows)):
        disp_high = float(rows[i]["high"])
        disp_low = float(rows[i]["low"])
        disp_close = float(rows[i]["close"])
        swing_highs = [float(r["high"]) for r in rows[i-swing_lookback:i]]
        swing_lows = [float(r["low"]) for r in rows[i-swing_lookback:i]]
        prev_swing_high = max(swing_highs) if swing_highs else 0
        prev_swing_low = min(swing_lows) if swing_lows else 0
        opposing = rows[i-1]
        if disp_close > prev_swing_high and (disp_high - disp_low) >= min_disp and float(opposing.get("close", 0)) <= float(opposing.get("open", 0)):
            zone_low = float(opposing["low"])
            zone_high = float(opposing["high"])
            obs.append({
                "type": "order_block",
                "direction": "bullish",
                "timeframe": tf,
                "created_index": i,
                "source_evidence_ids": sorted({
                    str(item_id)
                    for row in rows[i-1:i+1]
                    for item_id in row.get("source_observation_ids", [])
                    if item_id
                }),
                "start": opposing["timestamp"],
                "end": rows[i]["timestamp"],
                "low": zone_low,
                "high": zone_high,
                "state": "active",
                "created_at": rows[i]["timestamp"],
            })
        if disp_close < prev_swing_low and (disp_high - disp_low) >= min_disp and float(opposing.get("close", 0)) >= float(opposing.get("open", 0)):
            zone_low = float(opposing["low"])
            zone_high = float(opposing["high"])
            obs.append({
                "type": "order_block",
                "direction": "bearish",
                "timeframe": tf,
                "created_index": i,
                "source_evidence_ids": sorted({
                    str(item_id)
                    for row in rows[i-1:i+1]
                    for item_id in row.get("source_observation_ids", [])
                    if item_id
                }),
                "start": opposing["timestamp"],
                "end": rows[i]["timestamp"],
                "low": zone_low,
                "high": zone_high,
                "state": "active",
                "created_at": rows[i]["timestamp"],
            })
    for o in obs:
        for j in range(o["created_index"] + 1, len(rows)):
            b = rows[j]
            blo, bhi, bcl = float(b["low"]), float(b["high"]), float(b["close"])
            touched = blo <= o["high"] and bhi >= o["low"]
            if o["direction"] == "bullish":
                if bcl < o["low"]:
                    o["state"] = "invalidated"
                    o["invalidated_at"] = b["timestamp"]
                    break
                if blo <= o["low"]:
                    o["state"] = "filled"
                    o["filled_at"] = b["timestamp"]
                    break
            else:
                if bcl > o["high"]:
                    o["state"] = "invalidated"
                    o["invalidated_at"] = b["timestamp"]
                    break
                if bhi >= o["high"]:
                    o["state"] = "filled"
                    o["filled_at"] = b["timestamp"]
                    break
            if touched and o["state"] == "active":
                o["state"] = "partial"
                o["first_mitigated_at"] = b["timestamp"]
    return obs


def get_active_zones_for_snapshot(zones: List[Dict], max_per: int = 3) -> List[Dict]:
    """Return at most the 3 most recent active per asset/tf/dir/type."""
    grouped: Dict[tuple, List[Dict]] = {}
    for zone in zones:
        if zone.get("state") not in ("active", "partial"):
            continue
        key = (
            str(zone.get("asset", "")).upper(),
            str(zone.get("timeframe", "")),
            str(zone.get("direction", "")),
            str(zone.get("type") or zone.get("kind", "")),
        )
        grouped.setdefault(key, []).append(zone)
    selected: List[Dict] = []
    for group in grouped.values():
        group.sort(key=lambda z: (z.get("created_at", z.get("end", "")), str(z.get("reference_id", ""))), reverse=True)
        selected.extend(group[:max_per])
    return selected


def attach_zone_evidence(event: dict, zones: List[Dict]) -> dict:
    """Attach advisory zone evidence to event feature_snapshot (support/neutral etc)."""
    snap = event.setdefault("feature_snapshot", {})
    # simplistic: if any active zone overlaps recent price -> support
    price = snap.get("close") or 0
    for z in zones[:3]:
        if z.get("state") != "active":
            continue
        typ = z.get("type") or z.get("kind", "zone")
        tf = z.get("timeframe", "")
        key = f"{typ}_{tf}" if tf else typ
        if z.get("low", 0) <= price <= z.get("high", 0):
            snap[key] = "support"
        else:
            if key not in snap:
                snap[key] = "neutral"
    return event
