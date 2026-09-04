"""Regime-owned direct Bybit 1h/4h history and per-asset readiness."""

from __future__ import annotations

import hashlib
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import httpx

import config


REGIME_1H_SOURCE = "bybit_rest"
REGIME_1H_BAR_VERSION = "bybit-rest-1h-v1"
REGIME_1H_INTERVAL_MS = 60 * 60 * 1000
REGIME_1H_FETCH_DAYS = int(getattr(config, "REGIME_1H_FETCH_DAYS", 4))
REGIME_1H_RETAIN_DAYS = int(getattr(config, "REGIME_1H_RETAIN_DAYS", 3))
REGIME_1H_READINESS_BARS = int(getattr(config, "REGIME_1H_READINESS_BARS", 57))
REGIME_1H_REQUEST_LIMIT = 200
REGIME_4H_SOURCE = "bybit_rest"
REGIME_4H_BAR_VERSION = "bybit-rest-4h-v1"
REGIME_4H_INTERVAL_MS = 4 * 60 * 60 * 1000
REGIME_4H_FETCH_DAYS = int(getattr(config, "REGIME_4H_FETCH_DAYS", 15))
REGIME_4H_RETAIN_DAYS = int(getattr(config, "REGIME_4H_RETAIN_DAYS", 14))
REGIME_4H_READINESS_BARS = int(getattr(config, "REGIME_4H_READINESS_BARS", 57))
REGIME_4H_REQUEST_LIMIT = 200


class RegimeHistoryError(RuntimeError):
    """A direct-history response cannot establish regime readiness."""


def _utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def completed_4h_end(value: Any) -> datetime:
    return _completed_end(value, 4)


def completed_1h_end(value: Any) -> datetime:
    return _completed_end(value, 1)


def _completed_end(value: Any, interval_hours: int) -> datetime:
    timestamp = _utc(value)
    return timestamp.replace(hour=timestamp.hour - timestamp.hour % interval_hours,
        minute=0,
        second=0,
        microsecond=0,
    )


def _bar_id(asset: str, bar_end: datetime, bar_version: str) -> str:
    identity = f"{asset.upper()}|{bar_end.isoformat()}|{REGIME_1H_SOURCE}|{bar_version}"
    return hashlib.sha256(identity.encode()).hexdigest()


def init_regime_history_schema(conn: Any) -> None:
    for interval in ("1h", "4h"):
        conn.execute(
            f"""
        CREATE TABLE IF NOT EXISTS regime_{interval}_bars (
            bar_id TEXT PRIMARY KEY,
            asset TEXT NOT NULL,
            bar_end TEXT NOT NULL,
            source TEXT NOT NULL,
            venue TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL,
            source_start TEXT,
            source_end TEXT NOT NULL,
            request_id TEXT,
            retrieved_at TEXT NOT NULL,
            bar_version TEXT NOT NULL,
            UNIQUE(asset, bar_end, source, bar_version)
        )
            """
        )
        conn.execute(
            f"""
        CREATE TABLE IF NOT EXISTS regime_{interval}_backfill_jobs (
            asset TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            required_from TEXT NOT NULL,
            required_through TEXT NOT NULL,
            covered_bars INTEGER NOT NULL,
            missing_bars INTEGER NOT NULL,
            attempts INTEGER NOT NULL,
            lease_until TEXT,
            next_retry_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        )
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_regime_{interval}_bars_asset_end "
            f"ON regime_{interval}_bars (asset, bar_end)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_regime_{interval}_jobs_status "
            f"ON regime_{interval}_backfill_jobs (status, next_retry_at)"
        )


def _finite_positive(value: Any) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _normalize_row(
    asset: str,
    row: Any,
    retrieved_at: datetime,
    *,
    interval_ms: int,
    bar_version: str,
) -> dict[str, Any]:
    if isinstance(row, dict):
        start_ms = row.get("start_ms", row.get("timestamp"))
        values = [start_ms, row.get("open"), row.get("high"), row.get("low"), row.get("close"), row.get("volume")]
    elif isinstance(row, (list, tuple)) and len(row) >= 6:
        values = list(row[:6])
    else:
        raise RegimeHistoryError("malformed direct candle")
    try:
        start_ms = int(float(values[0]))
        start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
        end = start + timedelta(milliseconds=interval_ms)
        prices = [float(value) for value in values[1:5]]
        volume = float(values[5]) if values[5] is not None else None
    except (TypeError, ValueError, OverflowError) as exc:
        raise RegimeHistoryError("malformed direct candle values") from exc
    if not all(_finite_positive(value) for value in prices):
        raise RegimeHistoryError("non-positive direct candle price")
    if prices[1] < max(prices[0], prices[3]) or prices[2] > min(prices[0], prices[3]) or prices[2] > prices[1]:
        raise RegimeHistoryError("invalid direct candle range")
    if volume is not None and (not math.isfinite(volume) or volume < 0):
        raise RegimeHistoryError("invalid direct candle volume")
    canonical_asset = str(asset).upper()
    return {
        "bar_id": _bar_id(canonical_asset, end, bar_version),
        "asset": canonical_asset,
        "bar_end": end.isoformat(),
        "source": REGIME_4H_SOURCE,
        "venue": "bybit",
        "open": prices[0],
        "high": prices[1],
        "low": prices[2],
        "close": prices[3],
        "volume": volume,
        "source_start": start.isoformat(),
        "source_end": end.isoformat(),
        "request_id": None,
        "retrieved_at": retrieved_at.isoformat(),
        "bar_version": bar_version,
    }


def _fetch_bybit(asset: str, start_ms: int, end_ms: int, interval: str) -> list[Any]:
    """Fetch public completed-window candles without writing market.sqlite3."""
    symbol = f"{str(asset).upper()}USDT"
    for attempt in range(3):
        try:
            response = httpx.get(
                f"{config.BYBIT_LINEAR_BASE_URL.rstrip('/')}/v5/market/kline",
                params={
                    "category": "linear",
                    "symbol": symbol,
                    "interval": interval,
                    "start": start_ms,
                    "end": end_ms - 1,
                    "limit": REGIME_1H_REQUEST_LIMIT if interval == "60" else REGIME_4H_REQUEST_LIMIT,
                },
                timeout=float(getattr(
                    config, f"REGIME_{'1H' if interval == '60' else '4H'}_REQUEST_TIMEOUT_SECONDS", 20
                )),
            )
            if response.status_code == 429:
                retry_after = float(response.headers.get("retry-after", "5"))
                if attempt < 2:
                    time.sleep(max(0.0, retry_after))
                    continue
                raise RegimeHistoryError("rate_limited")
            response.raise_for_status()
            payload = response.json()
            if payload.get("retCode") != 0:
                raise RegimeHistoryError("bybit_response_error")
            result = payload.get("result") or {}
            rows = result.get("list") if isinstance(result, dict) else None
            if not isinstance(rows, list):
                raise RegimeHistoryError("malformed_bybit_response")
            return rows
        except (httpx.HTTPError, ValueError, RegimeHistoryError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    raise RegimeHistoryError("request_failed")


def fetch_bybit_1h(asset: str, start_ms: int, end_ms: int) -> list[Any]:
    return _fetch_bybit(asset, start_ms, end_ms, "60")


def fetch_bybit_4h(asset: str, start_ms: int, end_ms: int) -> list[Any]:
    return _fetch_bybit(asset, start_ms, end_ms, "240")


def _history_config(interval: str) -> tuple[str, str, int, int, int, int]:
    if interval == "1h":
        return (
            "regime_1h_bars", "regime_1h_backfill_jobs", REGIME_1H_INTERVAL_MS,
            REGIME_1H_FETCH_DAYS, REGIME_1H_RETAIN_DAYS, REGIME_1H_READINESS_BARS,
        )
    return (
        "regime_4h_bars", "regime_4h_backfill_jobs", REGIME_4H_INTERVAL_MS,
        REGIME_4H_FETCH_DAYS, REGIME_4H_RETAIN_DAYS, REGIME_4H_READINESS_BARS,
    )


def _required_window(cutoff: Any, interval: str = "4h") -> tuple[datetime, datetime, datetime]:
    through = completed_1h_end(cutoff) if interval == "1h" else completed_4h_end(cutoff)
    _, _, _, fetch_days, retain_days, _ = _history_config(interval)
    required_from = through - timedelta(days=retain_days)
    fetch_from = through - timedelta(days=fetch_days)
    return fetch_from, required_from, through


def _coverage(
    conn: Any,
    asset: str,
    required_from: datetime,
    through: datetime,
    interval: str,
) -> dict[str, Any]:
    table, _, interval_ms, _, retain_days, readiness_bars = _history_config(interval)
    version = REGIME_1H_BAR_VERSION if interval == "1h" else REGIME_4H_BAR_VERSION
    rows = conn.execute(
        f"""
        SELECT bar_end FROM {table}
         WHERE asset = ? AND source = ? AND bar_version = ?
           AND source_end > ? AND source_end <= ?
         ORDER BY source_end
        """,
        (str(asset).upper(), REGIME_1H_SOURCE, version,
         required_from.isoformat(), through.isoformat()),
    ).fetchall()
    ends = {_utc(row[0]) for row in rows}
    expected = {
        required_from + timedelta(milliseconds=interval_ms * index)
        for index in range(1, retain_days * (24 if interval == "1h" else 6) + 1)
    }
    missing = sorted(expected - ends)
    return {
        "covered_bars": len(ends),
        "missing_bars": len(missing),
        "ready": len(ends) >= readiness_bars and not missing,
    }


def _prune_regime_history(conn: Any, asset: str, through: Any, interval: str) -> int:
    """Retain the complete interval window needed by the regime scorer."""
    table, _, _, _, retain_days, _ = _history_config(interval)
    version = REGIME_1H_BAR_VERSION if interval == "1h" else REGIME_4H_BAR_VERSION
    cutoff = _utc(through) - timedelta(days=retain_days)
    cursor = conn.execute(
        f"""
        DELETE FROM {table}
         WHERE asset = ? AND source = ? AND bar_version = ? AND source_end <= ?
        """,
        (str(asset).upper(), REGIME_1H_SOURCE, version, cutoff.isoformat()),
    )
    return int(cursor.rowcount or 0)


def _ensure_asset_ready(
    conn: Any,
    asset: str,
    cutoff: Any,
    *,
    interval: str,
    fetcher: Callable[[str, int, int], Iterable[Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Backfill and assess one asset and interval without affecting others."""
    init_regime_history_schema(conn)
    asset = str(asset).upper()
    table, jobs_table, _, _, _, _ = _history_config(interval)
    version = REGIME_1H_BAR_VERSION if interval == "1h" else REGIME_4H_BAR_VERSION
    interval_ms = REGIME_1H_INTERVAL_MS if interval == "1h" else REGIME_4H_INTERVAL_MS
    fetch_from, required_from, through = _required_window(cutoff, interval)
    current = _utc(now or datetime.now(timezone.utc))
    coverage = _coverage(conn, asset, required_from, through, interval)
    if coverage["ready"]:
        _prune_regime_history(conn, asset, through, interval)
        coverage = _coverage(conn, asset, required_from, through, interval)
        conn.execute(
            f"""
            INSERT INTO {jobs_table}
                (asset, status, required_from, required_through, covered_bars, missing_bars,
                 attempts, lease_until, next_retry_at, last_error, updated_at)
            VALUES (?, 'ready', ?, ?, ?, ?, 0, NULL, NULL, NULL, ?)
            ON CONFLICT(asset) DO UPDATE SET
                status='ready', required_from=excluded.required_from,
                required_through=excluded.required_through, covered_bars=excluded.covered_bars,
                missing_bars=excluded.missing_bars, lease_until=NULL, next_retry_at=NULL,
                last_error=NULL, updated_at=excluded.updated_at
            """,
            (asset, required_from.isoformat(), through.isoformat(), coverage["covered_bars"],
             coverage["missing_bars"], current.isoformat()),
        )
        conn.commit()
        return {"asset": asset, "status": "ready", **coverage}

    row = conn.execute(
        f"SELECT status, lease_until, attempts FROM {jobs_table} WHERE asset = ?",
        (asset,),
    ).fetchone()
    if row and row[0] == "running" and row[1]:
        try:
            if _utc(row[1]) > current:
                return {
                    "asset": asset,
                    "status": "retryable",
                    **coverage,
                    "reason": "lease_active",
                    "next_retry_at": row[1],
                }
        except (TypeError, ValueError, OverflowError):
            pass
    attempts = int(row[2]) + 1 if row else 1
    conn.execute(
        f"""
        INSERT INTO {jobs_table}
            (asset, status, required_from, required_through, covered_bars, missing_bars,
             attempts, lease_until, next_retry_at, last_error, updated_at)
        VALUES (?, 'running', ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
        ON CONFLICT(asset) DO UPDATE SET
            status='running', required_from=excluded.required_from,
            required_through=excluded.required_through, covered_bars=excluded.covered_bars,
            missing_bars=excluded.missing_bars, attempts=excluded.attempts,
            lease_until=excluded.lease_until, updated_at=excluded.updated_at
        """,
        (asset, required_from.isoformat(), through.isoformat(), coverage["covered_bars"],
         coverage["missing_bars"], attempts, (current + timedelta(minutes=5)).isoformat(), current.isoformat()),
    )
    conn.commit()
    try:
        default_fetcher = fetch_bybit_1h if interval == "1h" else fetch_bybit_4h
        raw_rows = list((fetcher or default_fetcher)(
            asset, int(fetch_from.timestamp() * 1000), int(through.timestamp() * 1000)
        ))
        retrieved_at = _utc(now or datetime.now(timezone.utc))
        normalized = {}
        for row in raw_rows:
            item = _normalize_row(
                asset, row, retrieved_at, interval_ms=interval_ms, bar_version=version
            )
            end = _utc(item["bar_end"])
            if fetch_from < end <= through:
                if end in normalized:
                    raise RegimeHistoryError(f"duplicate_{interval}_bar")
                normalized[end] = item
        for item in normalized.values():
            conn.execute(
                f"""
                INSERT OR IGNORE INTO {table}
                    (bar_id, asset, bar_end, source, venue, open, high, low, close, volume,
                     source_start, source_end, request_id, retrieved_at, bar_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(item[key] for key in (
                    "bar_id", "asset", "bar_end", "source", "venue", "open", "high", "low",
                    "close", "volume", "source_start", "source_end", "request_id", "retrieved_at",
                    "bar_version",
                )),
            )
        coverage = _coverage(conn, asset, required_from, through, interval)
        status = "ready" if coverage["ready"] else "retryable"
        last_error = None if coverage["ready"] else f"incomplete_or_gapped_{interval}_history"
        if status == "ready":
            _prune_regime_history(conn, asset, through, interval)
            coverage = _coverage(conn, asset, required_from, through, interval)
        conn.execute(
            f"""
            UPDATE {jobs_table}
               SET status=?, covered_bars=?, missing_bars=?, lease_until=NULL,
                   next_retry_at=?, last_error=?, updated_at=?
             WHERE asset=?
            """,
            (status, coverage["covered_bars"], coverage["missing_bars"],
             None if status == "ready" else (current + timedelta(minutes=5)).isoformat(),
             last_error, current.isoformat(), asset),
        )
        conn.commit()
        return {
            "asset": asset,
            "status": status,
            **coverage,
            "last_error": last_error,
            "next_retry_at": None if status == "ready" else (current + timedelta(minutes=5)).isoformat(),
        }
    except Exception as exc:
        conn.execute(
            f"""
            UPDATE {jobs_table}
               SET status='retryable', lease_until=NULL, next_retry_at=?,
                   last_error=?, updated_at=?
             WHERE asset=?
            """,
            ((current + timedelta(minutes=5)).isoformat(), type(exc).__name__, current.isoformat(), asset),
        )
        conn.commit()
        return {
            "asset": asset,
            "status": "retryable",
            **coverage,
            "last_error": type(exc).__name__,
            "next_retry_at": (current + timedelta(minutes=5)).isoformat(),
            "error": type(exc).__name__,
        }


def ensure_asset_ready(
    conn: Any,
    asset: str,
    cutoff: Any,
    *,
    fetcher: Callable[[str, int, int], Iterable[Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    return _ensure_asset_ready(conn, asset, cutoff, interval="4h", fetcher=fetcher, now=now)


def ensure_asset_1h_ready(
    conn: Any,
    asset: str,
    cutoff: Any,
    *,
    fetcher: Callable[[str, int, int], Iterable[Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    return _ensure_asset_ready(conn, asset, cutoff, interval="1h", fetcher=fetcher, now=now)


def _load_regime_bars(conn: Any, asset: str, cutoff: Any, interval: str, limit: int):
    """Load completed direct bars with stable provenance for regime scoring."""
    import polars as pl

    table, _, _, _, _, _ = _history_config(interval)
    version = REGIME_1H_BAR_VERSION if interval == "1h" else REGIME_4H_BAR_VERSION
    cutoff = _utc(cutoff)
    rows = conn.execute(
        f"""
        SELECT bar_id, bar_end, open, high, low, close, volume, source, bar_version
          FROM {table}
         WHERE asset = ? AND source_end <= ? AND source = ? AND bar_version = ?
         ORDER BY source_end DESC LIMIT ?
        """,
        (str(asset).upper(), cutoff.isoformat(), REGIME_1H_SOURCE, version, limit),
    ).fetchall()
    rows.reverse()
    return pl.DataFrame({
        "timestamp": [_utc(row[1]) for row in rows],
        "open": [row[2] for row in rows],
        "high": [row[3] for row in rows],
        "low": [row[4] for row in rows],
        "close": [row[5] for row in rows],
        "volume": [row[6] or 0.0 for row in rows],
        "source": [row[7] for row in rows],
        "bar_id": [row[0] for row in rows],
        "bar_version": [row[8] for row in rows],
        "source_observation_ids": [[row[0]] for row in rows],
    })


def load_regime_1h_bars(conn: Any, asset: str, cutoff: Any, limit: int = 72):
    return _load_regime_bars(conn, asset, cutoff, "1h", limit)


def load_regime_4h_bars(conn: Any, asset: str, cutoff: Any, limit: int = 84):
    return _load_regime_bars(conn, asset, cutoff, "4h", limit)
