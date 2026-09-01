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


def _asset_from_symbol(symbol: str) -> str:
    s = str(symbol or "")
    if "_PERP" in s or "_PERP.A" in s or s.endswith(".A"):
        base = s.split("_")[0].split("USDT")[0].split("USD")[0]
        return base.upper() or "BTC"
    if "-USDT-PERP" in s:
        return s.split("-")[0].upper()
    # fallback guess
    for c in ("BTC", "ETH", "SOL", "PAXG", "XAUT"):
        if c in s.upper():
            return c
    return s[:3].upper() or "BTC"


def completed_cycle(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)


_INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def completed_cycle_for(now: datetime | None, interval: str) -> datetime:
    """Floor `now` to the most recent completed `interval` bar boundary."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    minutes = _INTERVAL_MINUTES.get(interval, 15)
    if minutes >= 60:
        hours = minutes // 60
        return now.replace(hour=now.hour - now.hour % hours, minute=0, second=0, microsecond=0)
    return now.replace(minute=now.minute - now.minute % minutes, second=0, microsecond=0)


def _ensure_utc(ts: datetime) -> datetime:
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _load_raw_observations_for_asset(conn, asset: str, cutoff: datetime, start: datetime, interval: str = "15m") -> List[Dict]:
    """Internal: raw rows with source for prefer logic."""
    cutoff = _ensure_utc(cutoff)
    rows = conn.execute(
        """
        SELECT source_end, source,
               CAST(json_extract(payload_json, '$.open') AS REAL),
               CAST(json_extract(payload_json, '$.high') AS REAL),
               CAST(json_extract(payload_json, '$.low') AS REAL),
               CAST(json_extract(payload_json, '$.close') AS REAL),
               COALESCE(CAST(json_extract(payload_json, '$.volume') AS REAL), 0.0),
               CAST(json_extract(payload_json, '$.open_interest') AS REAL),
               CAST(json_extract(payload_json, '$.funding_rate') AS REAL),
               payload_json
         FROM source_observations
         WHERE asset = ? AND interval=?
           AND source_end < ? AND source_end >= ?
            AND CAST(json_extract(payload_json, '$.close') AS REAL) > 0
         ORDER BY source_end ASC
        """,
        (asset, interval, cutoff, start),
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "timestamp": _ensure_utc(r[0]),
            "source": r[1],
            "open": float(r[2] or 0),
            "high": float(r[3] or 0),
            "low": float(r[4] or 0),
            "close": float(r[5] or 0),
            "volume": float(r[6] or 0),
            "open_interest": float(r[7]) if r[7] is not None else None,
            "funding_rate": float(r[8]) if r[8] is not None else None,
            "payload": r[9],
        })
    return out


def _prefer_rows(raw_rows: List[Dict]) -> List[Dict]:
    """For each timestamp prefer configured live WS data, then failover."""
    from collections import defaultdict
    by_ts: Dict[datetime, List[Dict]] = defaultdict(list)
    for r in raw_rows:
        by_ts[r["timestamp"]].append(r)
    preferred = []
    for ts, lst in sorted(by_ts.items()):
        if getattr(config, "COINANALYZE_EVAL_ENABLED", False):
            ca = [x for x in lst if x["source"] == "coinalyze"]
            if ca:
                preferred.append(ca[0])
                continue
        ws = [x for x in lst if str(x["source"]).endswith("_ws")]
        if ws:
            preferred.append(ws[0])
            continue
        vagg = [x for x in lst if x["source"] == getattr(config, "FAILOVER_SOURCE_NAME", "venue_agg_v1")]
        if vagg:
            preferred.append(vagg[0])
            continue
        # fallback any
        preferred.append(lst[0])
    return preferred


def load_preferred_15m_bars(conn, asset: Optional[str] = None, native_symbol: Optional[str] = None,
                            cutoff: Optional[datetime] = None, lookback_days: int = LOOKBACK_DAYS) -> pl.DataFrame:
    """Canonical preferred loader: usable CA wins over venue_agg_v1 for same bar end.
    If native_symbol given and looks CA, resolve to asset.
    """
    if cutoff is None:
        cutoff = _ensure_utc(datetime.now(timezone.utc))
    else:
        cutoff = _ensure_utc(cutoff)
    start = cutoff - timedelta(days=lookback_days)
    if asset is None and native_symbol:
        asset = _asset_from_symbol(native_symbol)
    if not asset:
        asset = "BTC"
    raw = _load_raw_observations_for_asset(conn, asset, cutoff, start, interval="15m")
    rows = _prefer_rows(raw)
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(
        {
            "timestamp": [r["timestamp"] for r in rows],
            "open": [r["open"] for r in rows],
            "high": [r["high"] for r in rows],
            "low": [r["low"] for r in rows],
            "close": [r["close"] for r in rows],
            "volume": [r["volume"] for r in rows],
            "open_interest": [r["open_interest"] for r in rows],
            "funding_rate": [r["funding_rate"] for r in rows],
            "source": [r["source"] for r in rows],
        },
        strict=False,
    )
    if "open_interest" in df.columns:
        df = df.with_columns(pl.col("open_interest").fill_null(0.0))
    if "funding_rate" in df.columns:
        df = df.with_columns(pl.col("funding_rate").fill_null(0.0))
    return df


def load_15m_bars(conn, symbol: str, cutoff: datetime, lookback_days: int = LOOKBACK_DAYS) -> pl.DataFrame:
    """Backward compat: delegate to preferred by asset."""
    asset = _asset_from_symbol(symbol)
    return load_preferred_15m_bars(conn, asset=asset, cutoff=cutoff, lookback_days=lookback_days)


def load_bars_for_interval(conn, symbol: str, interval: str, cutoff: datetime,
                           lookback_days: int = LOOKBACK_DAYS) -> pl.DataFrame:
    """Load preferred bars for an arbitrary eval interval (1m/5m/15m/...).
    HTF intervals (1h/4h) are resampled by ws_gateway and stored directly, so
    they load the same way; 15m/1h/4h are derived, 1m/5m streamed.
    """
    asset = _asset_from_symbol(symbol)
    cutoff = _ensure_utc(cutoff)
    start = cutoff - timedelta(days=lookback_days)
    raw = _load_raw_observations_for_asset(conn, asset, cutoff, start, interval=interval)
    rows = _prefer_rows(raw)
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(
        {
            "timestamp": [r["timestamp"] for r in rows],
            "open": [r["open"] for r in rows],
            "high": [r["high"] for r in rows],
            "low": [r["low"] for r in rows],
            "close": [r["close"] for r in rows],
            "volume": [r["volume"] for r in rows],
            "open_interest": [r["open_interest"] for r in rows],
            "funding_rate": [r["funding_rate"] for r in rows],
            "source": [r["source"] for r in rows],
        },
        strict=False,
    )
    if "open_interest" in df.columns:
        df = df.with_columns(pl.col("open_interest").fill_null(0.0))
    if "funding_rate" in df.columns:
        df = df.with_columns(pl.col("funding_rate").fill_null(0.0))
    return df


def load_btc_15m(conn, cutoff: datetime, lookback_days: int = LOOKBACK_DAYS) -> pl.DataFrame:
    """BTC preferred loader (delegates)."""
    cutoff = _ensure_utc(cutoff)
    df = load_preferred_15m_bars(conn, asset="BTC", cutoff=cutoff, lookback_days=lookback_days)
    if df.is_empty():
        return df
    return df.select(["timestamp", "close"])


def list_candidate_symbols(conn, cutoff: datetime, *, apply_rotation: bool = False,
                           assets: Iterable[str] | None = None) -> list[tuple[str, str]]:
    """Return every symbol in the upstream subscription universe.

    Strategies are intentionally unaware of rotation policy. The optional
    ``assets`` argument is retained for non-strategy callers and tests only.
    """
    cutoff = _ensure_utc(cutoff)
    if assets is not None:
        bases = sorted({str(asset).strip().upper() for asset in assets if str(asset).strip()})
        candidates = list(zip(config.expand_perp_symbols(bases, "bybit"), bases))
        if apply_rotation:
            from symbol_rotation import select_symbols
            return select_symbols(conn, candidates, cutoff)
        return candidates
    from symbol_rotation import subscription_assets
    bases, feed = subscription_assets(cutoff)
    candidates = list(zip(config.expand_perp_symbols(bases, "bybit"), bases))
    # The gateway and the evaluator consume the same durable rotation snapshot.
    # A missing/expired feed is fail-closed to permanent assets; strategies must
    # not re-rank that fallback from local bars.
    return candidates


def evaluation_symbols(conn, cutoff: datetime, snapshot: dict | None = None) -> list[tuple[str, str]]:
    """Use the evaluator-supplied scope; retain the loader for direct callers."""
    supplied = (snapshot or {}).get("subscription_symbols")
    if supplied is not None:
        return [(str(symbol), str(asset)) for symbol, asset in supplied]
    return list_candidate_symbols(conn, cutoff)


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
    alpha_path = Path(alpha_db_path or config.ANALYST_DB_PATH)
    # Single-shot open: re-arm must not block the 15m path on publisher lock contention.
    if alpha_path.exists():
        try:
            conn = config.get_db_connection(read_only=True, db_path=alpha_path)
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
