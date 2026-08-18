"""Shared market context for confluence v2 strategy plugins."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import polars as pl

import config
from alpha_outbox import OUTBOX_DIR
from structure_zones import compute_atr, detect_fvg, detect_order_blocks


MAX_BAR_AGE = timedelta(minutes=20)
LOOKBACK_DAYS = 16


def completed_cycle(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def load_15m_bars(conn, symbol: str, cutoff: datetime, lookback_days: int = LOOKBACK_DAYS) -> pl.DataFrame:
    """Load completed 15m OHLCV (+OI/funding) bars strictly before cutoff."""
    cutoff = _ensure_utc(cutoff)
    start = cutoff - timedelta(days=lookback_days)
    rows = conn.execute(
        """
        SELECT
            source_end as timestamp,
            json_extract(payload_json, '$.open')::DOUBLE as open,
            json_extract(payload_json, '$.high')::DOUBLE as high,
            json_extract(payload_json, '$.low')::DOUBLE as low,
            json_extract(payload_json, '$.close')::DOUBLE as close,
            COALESCE(json_extract(payload_json, '$.volume')::DOUBLE, 0.0) as volume,
            COALESCE(json_extract(payload_json, '$.open_interest')::DOUBLE, 0.0) as open_interest,
            COALESCE(json_extract(payload_json, '$.funding_rate')::DOUBLE, 0.0) as funding_rate
        FROM source_observations
        WHERE native_symbol = ?
          AND source_end < ?
          AND source_end >= ?
          AND json_extract(payload_json, '$.close')::DOUBLE > 0
        ORDER BY source_end ASC
        """,
        (symbol, cutoff, start),
    ).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(
        {
            "timestamp": [_ensure_utc(r[0]) for r in rows],
            "open": [float(r[1] or 0) for r in rows],
            "high": [float(r[2] or 0) for r in rows],
            "low": [float(r[3] or 0) for r in rows],
            "close": [float(r[4] or 0) for r in rows],
            "volume": [float(r[5] or 0) for r in rows],
            "open_interest": [float(r[6] or 0) for r in rows],
            "funding_rate": [float(r[7] or 0) for r in rows],
        },
        strict=False,
    )


def load_btc_15m(conn, cutoff: datetime, lookback_days: int = LOOKBACK_DAYS) -> pl.DataFrame:
    cutoff = _ensure_utc(cutoff)
    start = cutoff - timedelta(days=lookback_days)
    rows = conn.execute(
        """
        SELECT
            source_end as timestamp,
            json_extract(payload_json, '$.close')::DOUBLE as close
        FROM source_observations
        WHERE asset = 'BTC'
          AND source_end < ?
          AND source_end >= ?
          AND json_extract(payload_json, '$.close')::DOUBLE > 0
        ORDER BY source_end ASC
        """,
        (cutoff, start),
    ).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(
        {
            "timestamp": [_ensure_utc(r[0]) for r in rows],
            "close": [float(r[1] or 0) for r in rows],
        },
        strict=False,
    )


def list_candidate_symbols(conn, cutoff: datetime) -> list[tuple[str, str]]:
    """Return (native_symbol, asset) with recent completed bars before cutoff."""
    cutoff = _ensure_utc(cutoff)
    watch = conn.execute(
        """
        SELECT symbol, asset
        FROM discovery_watchlist_history
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY pool, symbol ORDER BY observed_at DESC, event_id DESC
        ) = 1
          AND state IN ('active', 'warmed')
        """
    ).fetchall()
    seen: dict[str, str] = {}
    for symbol, asset in watch:
        if symbol and asset:
            seen[str(symbol)] = str(asset)
    if seen:
        return sorted(seen.items())
    rows = conn.execute(
        """
        SELECT DISTINCT native_symbol, asset
        FROM source_observations
        WHERE source_end < ? AND source_end >= ? - INTERVAL '28 hours'
          AND json_extract(payload_json, '$.close')::DOUBLE > 0
        ORDER BY native_symbol
        """,
        (cutoff, cutoff),
    ).fetchall()
    return [(str(s), str(a)) for s, a in rows if s and a]


def resample_ohlcv(bars: pl.DataFrame, every: str) -> pl.DataFrame:
    if bars.is_empty():
        return bars
    bars = bars.sort("timestamp")
    agg = [
        pl.col("open").first(),
        pl.col("high").max(),
        pl.col("low").min(),
        pl.col("close").last(),
        pl.col("volume").sum(),
    ]
    if "open_interest" in bars.columns:
        agg.append(pl.col("open_interest").last())
    if "funding_rate" in bars.columns:
        agg.append(pl.col("funding_rate").last())
    return bars.group_by_dynamic("timestamp", every=every).agg(agg)


def ema_last(closes: Sequence[float], span: int) -> float | None:
    if len(closes) < span:
        return None
    series = pl.Series("close", [float(c) for c in closes]).ewm_mean(span=span, adjust=False)
    val = float(series[-1])
    if val != val:  # NaN
        return None
    return val


def last_completed_bar_fresh(bars_15m: pl.DataFrame, cutoff: datetime) -> bool:
    if bars_15m.is_empty():
        return False
    latest = _ensure_utc(bars_15m["timestamp"][-1])
    return cutoff - latest <= MAX_BAR_AGE


def atr_last(bars: pl.DataFrame, period: int = 14) -> float | None:
    if bars.height < period + 1:
        return None
    return float(compute_atr(bars, period=period))


def structure_bias_4h(bars_4h: pl.DataFrame) -> str:
    """close vs EMA48_4h → long | short | missing."""
    if bars_4h.is_empty() or bars_4h.height < 48:
        return "missing"
    closes = bars_4h["close"].to_list()
    ema48 = ema_last(closes, 48)
    if ema48 is None or ema48 <= 0:
        return "missing"
    close = float(closes[-1])
    if close > ema48:
        return "long"
    if close < ema48:
        return "short"
    return "missing"


def _zone_mid(zone: dict) -> float | None:
    lo, hi = zone.get("low"), zone.get("high")
    if lo is None or hi is None:
        return None
    return (float(lo) + float(hi)) / 2.0


def _zone_direction(zone: dict) -> str | None:
    d = zone.get("direction")
    if d in ("bullish", "long"):
        return "long"
    if d in ("bearish", "short"):
        return "short"
    return None


def zone_bias_4h(zones: Sequence[dict], ref_close: float, atr_4h: float | None) -> tuple[str, dict | None]:
    """Nearest active|partial 4h FVG/OB by midpoint distance → bias + zone."""
    candidates = []
    for z in zones:
        tf = str(z.get("timeframe") or "")
        if tf not in ("4h", "4H"):
            kind = str(z.get("kind") or "")
            if "_4h" not in kind and not kind.endswith("4h"):
                continue
        state = z.get("state", "active")
        if state not in ("active", "partial"):
            continue
        mid = _zone_mid(z)
        direction = _zone_direction(z)
        if mid is None or direction is None:
            continue
        dist = abs(float(ref_close) - mid)
        dist_atr = dist / atr_4h if atr_4h and atr_4h > 0 else dist
        candidates.append((dist_atr, z, direction))
    if not candidates:
        return "missing", None
    candidates.sort(key=lambda item: item[0])
    _, zone, direction = candidates[0]
    return direction, zone


def resolve_bias(structure: str, zone: str) -> str | None:
    """Agree-or-abstain. Returns direction or None (fail)."""
    if structure in ("long", "short") and zone == "missing":
        return structure
    if zone in ("long", "short") and structure == "missing":
        return zone
    if structure in ("long", "short") and structure == zone:
        return structure
    return None


def compute_htf_zones(bars_1h: pl.DataFrame, bars_4h: pl.DataFrame) -> list[dict]:
    zones: list[dict] = []
    if not bars_1h.is_empty() and bars_1h.height >= 5:
        atr1 = compute_atr(bars_1h)
        for z in detect_fvg(bars_1h, atr=atr1, tf="1h"):
            zones.append(z)
        for z in detect_order_blocks(bars_1h, atr=atr1, tf="1h"):
            zones.append(z)
    if not bars_4h.is_empty() and bars_4h.height >= 5:
        atr4 = compute_atr(bars_4h)
        for z in detect_fvg(bars_4h, atr=atr4, tf="4h"):
            zones.append(z)
        for z in detect_order_blocks(bars_4h, atr=atr4, tf="4h"):
            zones.append(z)
    return zones


def compression_ok(bars_1h: pl.DataFrame, n: int, k: float, atr_1h: float) -> tuple[bool, float, float, float]:
    """Full-window range ≤ k·ATR. Returns ok, base_high, base_low, range."""
    if bars_1h.height < n or atr_1h <= 0:
        return False, 0.0, 0.0, 0.0
    window = bars_1h.tail(n)
    base_high = float(window["high"].max())
    base_low = float(window["low"].min())
    rng = base_high - base_low
    return rng <= k * atr_1h, base_high, base_low, rng


def prior_base_expansion_fail(
    bars_1h: pl.DataFrame,
    n: int,
    g: float,
    atr_1h: float,
    direction: str,
) -> bool:
    """True if last 1h close breaks prior (N-1) range by > g·ATR in trade direction."""
    if bars_1h.height < n or atr_1h <= 0:
        return True
    prior = bars_1h.tail(n).head(n - 1)
    if prior.height < 1:
        return True
    prior_high = float(prior["high"].max())
    prior_low = float(prior["low"].min())
    last_close = float(bars_1h["close"][-1])
    grace = g * atr_1h
    if direction == "long" and last_close > prior_high + grace:
        return True
    if direction == "short" and last_close < prior_low - grace:
        return True
    return False


def prior_range_ratio(bars_1h: pl.DataFrame, n: int, p: int) -> float | None:
    if bars_1h.height < n + p:
        return None
    base = bars_1h.tail(n)
    prior = bars_1h.tail(n + p).head(p)
    base_range = float(base["high"].max()) - float(base["low"].min())
    prior_range = float(prior["high"].max()) - float(prior["low"].min())
    if prior_range <= 0:
        return None
    return base_range / prior_range


def zone_stack_and_ltf_scores(
    zones: Sequence[dict],
    ref_price: float,
    atr_ref: float,
    direction: str,
) -> tuple[float, float]:
    """Return (ltf_inside_htf, zone_stack_tightness) in [0,1]."""
    from confluence_scoring import proximity_score

    if atr_ref <= 0:
        return 0.0, 0.0
    wanted = "bullish" if direction == "long" else "bearish"
    htf = [z for z in zones if str(z.get("timeframe") or "") in ("4h", "1h") and z.get("state") in ("active", "partial")]
    if not htf:
        return 0.0, 0.0
    dists = []
    dir_match = 0
    for z in htf:
        mid = _zone_mid(z)
        if mid is None:
            continue
        d = abs(ref_price - mid) / atr_ref
        dists.append(d)
        zd = z.get("direction")
        if zd == wanted or (wanted == "bullish" and zd == "long") or (wanted == "bearish" and zd == "short"):
            dir_match += 1
    if not dists:
        return 0.0, 0.0
    best = min(dists)
    ltf = proximity_score(best)
    stack = min(1.0, dir_match / max(2.0, len(dists) * 0.5)) * proximity_score(best)
    return ltf, stack


def has_active_event(
    strategy_id: str,
    asset: str,
    direction: str,
    *,
    alpha_db_path: str | Path | None = None,
    outbox_dir: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """True if a non-terminal live event exists for asset+direction under strategy_id."""
    now = _ensure_utc(now or datetime.now(timezone.utc))
    alpha_path = Path(alpha_db_path or config.ALPHA_DB_PATH)
    # Single-shot open: re-arm must not block the 15m path on publisher lock contention.
    if alpha_path.exists():
        try:
            import duckdb

            conn = duckdb.connect(str(alpha_path), read_only=True)
            try:
                row = conn.execute(
                    """
                    SELECT 1 FROM alpha_events
                    WHERE strategy_id = ? AND asset = ? AND direction = ?
                      AND status = 'active' AND valid_until > ?
                    LIMIT 1
                    """,
                    (strategy_id, asset, direction, now),
                ).fetchone()
                if row:
                    return True
            finally:
                conn.close()
        except Exception:
            pass

    directory = Path(outbox_dir or OUTBOX_DIR)
    if not directory.exists():
        return False
    for path in directory.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("strategy_id") != strategy_id:
            continue
        if payload.get("asset") != asset or payload.get("direction") != direction:
            continue
        vu = payload.get("valid_until")
        if not vu:
            continue
        try:
            until = _ensure_utc(datetime.fromisoformat(str(vu).replace("Z", "+00:00")))
        except ValueError:
            continue
        if until > now:
            return True
    return False


def snapshot_zones_for_asset(snapshot: dict, asset: str) -> list[dict]:
    zones = snapshot.get("zones") or []
    return [z for z in zones if not z.get("asset") or z.get("asset") == asset]
