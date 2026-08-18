"""Read-only 15-minute evaluator helpers shared by alpha strategy daemons."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

import config
from explosion_ignition import score_ignition
from trend_acceleration import MAX_BAR_GAP, score_trend_acceleration


EVALUATOR_INTERVAL_SECONDS = 15 * 60
MINIMUM_SCORE = 60.0


def completed_cycle(now: datetime | None = None) -> datetime:
    """Return the start of the in-progress 15-minute bar, in UTC."""
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    return now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)


def active_watchlist(conn, pool: str) -> list[tuple[str, str]]:
    """Return current active/warmed symbols, excluding stale historical entries."""
    rows = conn.execute("""
        SELECT symbol, asset
        FROM discovery_watchlist_history
        WHERE pool = ?
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY pool, symbol ORDER BY observed_at DESC, event_id DESC
        ) = 1
          AND state IN ('active', 'warmed')
        ORDER BY symbol
    """, (pool,)).fetchall()
    return [(symbol, asset) for symbol, asset in rows]


def _frames_for_watchlist(conn, watchlist: list[tuple[str, str]], cutoff: datetime) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    symbols = [symbol for symbol, _ in watchlist]
    if not symbols:
        return {}, pd.DataFrame()
    placeholders = ", ".join("?" for _ in symbols)
    data = conn.execute(f"""
        SELECT 
            source_end as timestamp,
            asset as underlying,
            native_symbol as symbol,
            json_extract(payload_json, '$.open_interest')::DOUBLE as open_interest,
            json_extract(payload_json, '$.funding_rate')::DOUBLE as funding_rate,
            json_extract(payload_json, '$.open')::DOUBLE as open,
            json_extract(payload_json, '$.high')::DOUBLE as high,
            json_extract(payload_json, '$.low')::DOUBLE as low,
            json_extract(payload_json, '$.close')::DOUBLE as close,
            json_extract(payload_json, '$.volume')::DOUBLE as volume
        FROM source_observations
        WHERE source_end < ? AND (native_symbol IN ({placeholders}) OR asset = 'BTC') 
          AND json_extract(payload_json, '$.close')::DOUBLE > 0
        ORDER BY asset, source_end
    """, (cutoff, *symbols)).fetchdf()
    if data.empty:
        return {}, data
    btc = data[data["underlying"] == "BTC"]
    frames = {symbol: data[data["symbol"] == symbol] for symbol in symbols}
    return frames, btc


def _is_fresh(frame: pd.DataFrame, cutoff: datetime) -> bool:
    if frame.empty:
        return False
    latest = frame["timestamp"].max()
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return cutoff - latest <= MAX_BAR_GAP


def evaluate_watchlist(conn, pool: str, scorer, cutoff: datetime | None = None, preset: str = "balanced") -> list[dict]:
    """Score only watchlisted symbols using closed, fresh local market bars."""
    cutoff = cutoff or completed_cycle()
    watchlist = active_watchlist(conn, pool)
    frames, btc = _frames_for_watchlist(conn, watchlist, cutoff)
    if btc.empty or not _is_fresh(btc, cutoff):
        return []

    results = []
    for symbol, asset in watchlist:
        frame = frames[symbol]
        if not _is_fresh(frame, cutoff):
            continue
        candidate = scorer(frame, btc) if scorer is score_ignition else scorer(frame, btc, preset=preset)
        if candidate is None:
            continue
        candidate["asset"] = asset
        candidate["source_symbol"] = symbol
        results.append(candidate)
    return sorted(results, key=lambda item: item["score"], reverse=True)


def ignition_candidates(conn, cutoff: datetime | None = None) -> list[dict]:
    return evaluate_watchlist(conn, "ignition", score_ignition, cutoff=cutoff)


def acceleration_candidates(conn, cutoff: datetime | None = None) -> list[dict]:
    return evaluate_watchlist(conn, "continuation", score_trend_acceleration, cutoff=cutoff)


def event_from_candidate(candidate: dict, family: str) -> dict:
    """Translate a ranking observation to the portable alpha-event schema."""
    close = float(candidate["close"])
    observed_at = candidate["observed_at"]
    if family == "ignition":
        entry_price = close * 1.005
        return {
            "schema_version": 1,
            "strategy_id": "impulse-ignition-v1",
            "asset": candidate["asset"],
            "direction": "long",
            "setup_class": "impulse_ignition",
            "phase": "armed_base",
            "observed_at": observed_at.isoformat(),
            "valid_until": (observed_at + timedelta(hours=4)).isoformat(),
            "horizon_minutes": 240,
            "confidence": round(candidate["score"] / 100, 4),
            "entry_condition": {"type": "breakout_above", "price": round(entry_price, 8)},
            "invalidation_price": round(close * 0.97, 8),
            "targets": [round(close * 1.04, 8), round(close * 1.08, 8)],
            "feature_snapshot": candidate,
        }
    entry_price = float(candidate["breakout_level"])
    risk = abs(entry_price - close) or close * 0.02
    return {
        "schema_version": 1,
        "strategy_id": "continuation-breakout-balanced-v1",
        "asset": candidate["asset"],
        "direction": "long",
        "setup_class": "continuation_breakout",
        "phase": "confirmed_expansion",
        "observed_at": observed_at.isoformat(),
        "valid_until": (observed_at + timedelta(hours=4)).isoformat(),
        "horizon_minutes": 240,
        "confidence": round(candidate["score"] / 100, 4),
        "entry_condition": {"type": "breakout_above", "price": round(entry_price, 8)},
        "invalidation_price": round(entry_price - risk, 8),
        "targets": [round(entry_price + risk * 1.5, 8), round(entry_price + risk * 3, 8)],
        "feature_snapshot": candidate,
    }
