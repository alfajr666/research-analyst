"""Read-only accumulation detection from completed local futures bars."""

from __future__ import annotations

import os
import statistics
from collections import OrderedDict
from datetime import datetime, timezone

import polars as pl


VOL_THRESHOLD = float(os.getenv("VOLUME_SPIKE_THRESHOLD", "1.5"))
PRICE_THRESHOLD = float(os.getenv("PRICE_SILENT_THRESHOLD", "3.0"))
MAX_BAR_AGE_SECONDS = 20 * 60


def completed_cycle(now: datetime | None = None) -> datetime:
    """Return the start of the in-progress UTC 15-minute bar."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)


def get_hourly_buckets(conn, symbol: str, cutoff: datetime) -> list[dict]:
    """Aggregate only completed 15-minute bars into completed hourly buckets."""
    rows = conn.execute("""
        SELECT 
            source_end as timestamp,
            json_extract(payload_json, '$.volume')::DOUBLE as volume,
            json_extract(payload_json, '$.close')::DOUBLE as close,
            json_extract(payload_json, '$.open_interest')::DOUBLE as open_interest
        FROM source_observations
        WHERE native_symbol = ? AND source_end < ? AND source_end >= ? - INTERVAL '30 hours'
        ORDER BY source_end ASC
    """, (symbol, cutoff, cutoff)).fetchall()
    buckets: OrderedDict[datetime, dict] = OrderedDict()
    for timestamp, volume, close, open_interest in rows:
        hour = timestamp.replace(minute=0, second=0, microsecond=0)
        bucket = buckets.setdefault(hour, {
            "hour": hour, "volume": 0.0, "close": 0.0, "oi_raw": 0.0, "bar_count": 0,
        })
        bucket["volume"] += float(volume or 0.0)
        bucket["close"] = float(close or 0.0)
        bucket["oi_raw"] = float(open_interest or 0.0)
        bucket["bar_count"] += 1
    return [bucket for bucket in buckets.values() if bucket["bar_count"] == 4]


def check_accumulation(hourly: list[dict]) -> dict | None:
    """Identify a volume spike with quiet one-hour price action."""
    if len(hourly) < 25:
        return None
    average_volume = statistics.median(item["volume"] for item in hourly[-25:-1])
    if average_volume <= 0:
        return None
    current, previous = hourly[-1], hourly[-2]
    price_change = ((current["close"] - previous["close"]) / previous["close"] * 100
                    if previous["close"] > 0 else 0.0)
    volume_spike = current["volume"] / average_volume
    if volume_spike < VOL_THRESHOLD or abs(price_change) > PRICE_THRESHOLD:
        return None
    return {
        "vol_spike": round(volume_spike, 2),
        "price_change_1h": round(price_change, 2),
        "hour_volume": round(current["volume"], 2),
        "close_price": current["close"],
        "oi_raw": current["oi_raw"],
    }


def confluence(conn, symbol: str, cutoff: datetime) -> dict | None:
    """Return EMA pullback and candle confirmation from fresh completed bars."""
    rows = conn.execute("""
        SELECT 
            source_end as timestamp,
            json_extract(payload_json, '$.open')::DOUBLE as open,
            json_extract(payload_json, '$.close')::DOUBLE as close
        FROM source_observations
        WHERE native_symbol = ? AND source_end < ? AND source_end >= ? - INTERVAL '7 days'
        ORDER BY source_end ASC
    """, (symbol, cutoff, cutoff)).fetchall()
    if len(rows) < 100:
        return None
    latest_timestamp, latest_open, latest_close = rows[-1]
    if (cutoff - latest_timestamp).total_seconds() > MAX_BAR_AGE_SECONDS:
        return None
    closes = [float(row[2] or 0.0) for row in rows]
    ema = pl.DataFrame({"close": closes}).with_columns(
        pl.col("close").ewm_mean(span=99, adjust=False).alias("ema_99")
    ).tail(1).item(0, "ema_99")
    latest_close = float(latest_close or 0.0)
    latest_open = float(latest_open or 0.0)
    if ema <= 0 or latest_close <= 0:
        return None
    direction = "long" if latest_close > ema else "short"
    ema_distance = ((latest_close - ema) / ema if direction == "long"
                    else (ema - latest_close) / ema)
    if not 0 <= ema_distance <= 0.01:
        return None
    triggered = ((direction == "long" and latest_close > latest_open) or
                 (direction == "short" and latest_close < latest_open))
    if not triggered:
        return None
    return {
        "direction": direction,
        "ema_99": float(ema),
        "ema_distance": float(ema_distance),
        "close": latest_close,
        "open": latest_open,
        "bar_timestamp": latest_timestamp,
    }
