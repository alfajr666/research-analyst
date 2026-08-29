"""session_levels.py — UTC previous-day high/low (PDH/PDL) for LSR.

Pure, PIT, no I/O. Matches specs/strategy-liquidity-sweep-reversal-v1.md M1 contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import polars as pl


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _utc_day(ts: datetime) -> str:
    ts = _ensure_utc(ts)
    return ts.strftime("%Y-%m-%d")


def pdh_pdl(bars_15m: pl.DataFrame, asof_ts: datetime) -> dict[str, Any] | None:
    """Return PDH/PDL for the UTC day prior to asof_ts's calendar day.

    PDH(D) = max high of completed 15m bars whose UTC day == D-1
    PDL(D) = min low  of completed 15m bars whose UTC day == D-1
    """
    if bars_15m is None or bars_15m.height == 0:
        return None
    if "timestamp" not in bars_15m.columns or "high" not in bars_15m.columns or "low" not in bars_15m.columns:
        return None

    df = bars_15m.select(["timestamp", "high", "low"]).sort("timestamp")
    asof = _ensure_utc(asof_ts)
    target_day = _utc_day(asof)

    # Compute day for each bar
    df = df.with_columns(
        pl.col("timestamp").dt.strftime("%Y-%m-%d").alias("utc_day")
    )

    # Find prior day = day before target_day
    # Since days are strings, find unique days, pick the one immediately before target
    days = df["utc_day"].unique().to_list()
    days = sorted([d for d in days if d])
    if not days:
        return None

    # Find the largest day < target_day
    prior_days = [d for d in days if d < target_day]
    if not prior_days:
        return None
    prior_day = max(prior_days)

    prior = df.filter(pl.col("utc_day") == prior_day)
    if prior.height == 0:
        return None

    pdh = float(prior["high"].max())
    pdl = float(prior["low"].min())
    if pdh <= 0 or pdl <= 0 or pdh < pdl:
        return None

    return {
        "pdh": pdh,
        "pdl": pdl,
        "prior_utc_day": prior_day,
        "bar_count": int(prior.height),
    }


def pdh_pdl_series(bars_15m: pl.DataFrame) -> pl.DataFrame:
    """Optional vectorized helper: for each bar, attach the PDH/PDL of its prior UTC day."""
    if bars_15m is None or bars_15m.height == 0:
        return pl.DataFrame({"timestamp": [], "pdh": [], "pdl": [], "prior_utc_day": []})

    df = bars_15m.select(["timestamp", "high", "low"]).sort("timestamp")
    df = df.with_columns(
        pl.col("timestamp").dt.strftime("%Y-%m-%d").alias("bar_day")
    )

    # Group by day and compute daily extremes, then shift to get prior
    daily = df.group_by("bar_day").agg([
        pl.col("high").max().alias("day_high"),
        pl.col("low").min().alias("day_low"),
    ]).sort("bar_day")

    # For each bar_day, prior is the previous day's extremes
    daily = daily.with_columns([
        pl.col("day_high").shift(1).alias("pdh"),
        pl.col("day_low").shift(1).alias("pdl"),
        pl.col("bar_day").shift(1).alias("prior_utc_day"),
    ])

    # Join back
    out = df.join(daily.select(["bar_day", "pdh", "pdl", "prior_utc_day"]), on="bar_day", how="left")
    return out.select(["timestamp", "pdh", "pdl", "prior_utc_day"]).drop_nulls()
