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
    ordered = bars.sort("timestamp")
    indexed = ordered.with_row_index("created_index").with_columns(
        (pl.col("low").cast(pl.Float64) - pl.col("high").cast(pl.Float64).shift(2)).alias("bullish_gap"),
        (pl.col("low").cast(pl.Float64).shift(2) - pl.col("high").cast(pl.Float64)).alias("bearish_gap"),
        pl.col("timestamp").shift(2).alias("two_back_timestamp"),
        pl.col("high").cast(pl.Float64).shift(2).alias("two_back_high"),
        pl.col("low").cast(pl.Float64).shift(2).alias("two_back_low"),
    )

    def evidence_for(items: List[Dict]) -> List[str]:
        return sorted({
            str(item_id)
            for item in items
            for item_id in item.get("source_observation_ids", [])
            if item_id
        })

    bullish = indexed.filter(pl.col("bullish_gap") > min_gap).select(
        "created_index",
        pl.lit(0).alias("direction_order"),
        pl.lit("bullish").alias("direction"),
        pl.col("two_back_timestamp").alias("start"),
        pl.col("timestamp").alias("end"),
        pl.col("two_back_high").alias("low"),
        pl.col("low").cast(pl.Float64).alias("high"),
        pl.col("bullish_gap").alias("gap"),
    )
    bearish = indexed.filter(pl.col("bearish_gap") > min_gap).select(
        "created_index",
        pl.lit(1).alias("direction_order"),
        pl.lit("bearish").alias("direction"),
        pl.col("two_back_timestamp").alias("start"),
        pl.col("timestamp").alias("end"),
        pl.col("high").cast(pl.Float64).alias("low"),
        pl.col("two_back_low").alias("high"),
        pl.col("bearish_gap").alias("gap"),
    )
    candidates = pl.concat([bullish, bearish], how="vertical_relaxed").sort(
        ["created_index", "direction_order"]
    )
    rows = ordered.to_dicts()
    fvgs: List[Dict] = []
    for candidate in candidates.to_dicts():
        index = int(candidate["created_index"])
        fvgs.append({
            "type": "fvg",
            "direction": candidate["direction"],
            "timeframe": tf,
            "created_index": index,
            "source_evidence_ids": evidence_for(rows[index - 2:index + 1]),
            "start": candidate["start"],
            "end": candidate["end"],
            "low": float(candidate["low"]),
            "high": float(candidate["high"]),
            "gap": float(candidate["gap"]),
            "state": "active",
            "created_at": candidate["end"],
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
    ordered = bars.sort("timestamp")
    min_disp = 1.5 * atr
    indexed = ordered.with_row_index("created_index").with_columns(
        pl.col("high").cast(pl.Float64).shift(1).rolling_max(
            swing_lookback, min_samples=swing_lookback,
        ).alias("previous_swing_high"),
        pl.col("low").cast(pl.Float64).shift(1).rolling_min(
            swing_lookback, min_samples=swing_lookback,
        ).alias("previous_swing_low"),
        pl.col("open").cast(pl.Float64).shift(1).alias("opposing_open"),
        pl.col("close").cast(pl.Float64).shift(1).alias("opposing_close"),
        pl.col("low").cast(pl.Float64).shift(1).alias("opposing_low"),
        pl.col("high").cast(pl.Float64).shift(1).alias("opposing_high"),
        pl.col("timestamp").shift(1).alias("opposing_timestamp"),
    )
    base = indexed.filter(
        (pl.col("high").cast(pl.Float64) - pl.col("low").cast(pl.Float64) >= min_disp)
        & (pl.col("created_index") >= swing_lookback)
    )
    bullish = base.filter(
        (pl.col("close").cast(pl.Float64) > pl.col("previous_swing_high"))
        & (pl.col("opposing_close") <= pl.col("opposing_open"))
    ).select(
        "created_index",
        pl.lit(0).alias("direction_order"),
        pl.lit("bullish").alias("direction"),
        pl.col("opposing_low").alias("low"),
        pl.col("opposing_high").alias("high"),
        pl.col("opposing_open").alias("opposing_open"),
        pl.col("opposing_timestamp").alias("start"),
        pl.col("timestamp").alias("end"),
    )
    bearish = base.filter(
        (pl.col("close").cast(pl.Float64) < pl.col("previous_swing_low"))
        & (pl.col("opposing_close") >= pl.col("opposing_open"))
    ).select(
        "created_index",
        pl.lit(1).alias("direction_order"),
        pl.lit("bearish").alias("direction"),
        pl.col("opposing_low").alias("low"),
        pl.col("opposing_high").alias("high"),
        pl.col("opposing_open").alias("opposing_open"),
        pl.col("opposing_timestamp").alias("start"),
        pl.col("timestamp").alias("end"),
    )
    candidates = pl.concat([bullish, bearish], how="vertical_relaxed").sort(
        ["created_index", "direction_order"]
    )
    rows = ordered.to_dicts()
    obs: List[Dict] = []
    for candidate in candidates.to_dicts():
        index = int(candidate["created_index"])
        obs.append({
            "type": "order_block",
            "direction": candidate["direction"],
            "timeframe": tf,
            "created_index": index,
            "source_evidence_ids": sorted({
                str(item_id)
                for row in rows[index - 1:index + 1]
                for item_id in row.get("source_observation_ids", [])
                if item_id
            }),
            "start": candidate["start"],
            "end": candidate["end"],
            "low": float(candidate["low"]),
            "high": float(candidate["high"]),
            "state": "active",
            "created_at": candidate["end"],
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
