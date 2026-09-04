"""Regime-owned direct Bybit 4h history and per-asset readiness."""

from __future__ import annotations

import hashlib
import math
import time
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import httpx

import config


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
    timestamp = _utc(value)
    return timestamp.replace(
        hour=timestamp.hour - timestamp.hour % 4,
        minute=0,
        second=0,
        microsecond=0,
    )


def _bar_id(asset: str, bar_end: datetime) -> str:
    identity = f"{asset.upper()}|{bar_end.isoformat()}|{REGIME_4H_SOURCE}|{REGIME_4H_BAR_VERSION}"
    return hashlib.sha256(identity.encode()).hexdigest()


def init_regime_history_schema(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS regime_4h_bars (
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
        """
        CREATE TABLE IF NOT EXISTS regime_4h_backfill_jobs (
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_regime_4h_bars_asset_end ON regime_4h_bars (asset, bar_end)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_regime_4h_jobs_status ON regime_4h_backfill_jobs (status, next_retry_at)")


def _finite_positive(value: Any) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _normalize_row(asset: str, row: Any, retrieved_at: datetime) -> dict[str, Any]:
    if isinstance(row, dict):
        start_ms = row.get("start_ms", row.get("timestamp"))
        values = [start_ms, row.get("open"), row.get("high"), row.get("low"), row.get("close"), row.get("volume")]
    elif isinstance(row, (list, tuple)) and len(row) >= 6:
        values = list(row[:6])
    else:
        raise RegimeHistoryError("malformed 4h candle")
    try:
        start_ms = int(float(values[0]))
        start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
        end = start + timedelta(milliseconds=REGIME_4H_INTERVAL_MS)
        prices = [float(value) for value in values[1:5]]
        volume = float(values[5]) if values[5] is not None else None
    except (TypeError, ValueError, OverflowError) as exc:
        raise RegimeHistoryError("malformed 4h candle values") from exc
    if not all(_finite_positive(value) for value in prices):
        raise RegimeHistoryError("non-positive 4h candle price")
    if prices[1] < max(prices[0], prices[3]) or prices[2] > min(prices[0], prices[3]) or prices[2] > prices[1]:
        raise RegimeHistoryError("invalid 4h candle range")
    if volume is not None and (not math.isfinite(volume) or volume < 0):
        raise RegimeHistoryError("invalid 4h candle volume")
    canonical_asset = str(asset).upper()
    return {
        "bar_id": _bar_id(canonical_asset, end),
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
        "bar_version": REGIME_4H_BAR_VERSION,
    }


def fetch_bybit_4h(asset: str, start_ms: int, end_ms: int) -> list[Any]:
    """Fetch public completed-window candles without writing market.sqlite3."""
    symbol = f"{str(asset).upper()}USDT"
    for attempt in range(3):
        try:
            response = httpx.get(
                f"{config.BYBIT_LINEAR_BASE_URL.rstrip('/')}/v5/market/kline",
                params={
                    "category": "linear",
                    "symbol": symbol,
                    "interval": "240",
                    "start": start_ms,
                    "end": end_ms - 1,
                    "limit": REGIME_4H_REQUEST_LIMIT,
                },
                timeout=float(getattr(config, "REGIME_4H_REQUEST_TIMEOUT_SECONDS", 20)),
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


def _required_window(cutoff: Any) -> tuple[datetime, datetime, datetime]:
    through = completed_4h_end(cutoff)
    required_from = through - timedelta(days=REGIME_4H_RETAIN_DAYS)
    fetch_from = through - timedelta(days=REGIME_4H_FETCH_DAYS)
    return fetch_from, required_from, through


def _coverage(conn: Any, asset: str, required_from: datetime, through: datetime) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT bar_end FROM regime_4h_bars
         WHERE asset = ? AND source = ? AND bar_version = ?
           AND source_end > ? AND source_end <= ?
         ORDER BY source_end
        """,
        (str(asset).upper(), REGIME_4H_SOURCE, REGIME_4H_BAR_VERSION,
         required_from.isoformat(), through.isoformat()),
    ).fetchall()
    ends = {_utc(row[0]) for row in rows}
    expected = {
        required_from + timedelta(milliseconds=REGIME_4H_INTERVAL_MS * index)
        for index in range(1, REGIME_4H_RETAIN_DAYS * 6 + 1)
    }
    missing = sorted(expected - ends)
    return {
        "covered_bars": len(ends),
        "missing_bars": len(missing),
        "ready": len(ends) >= REGIME_4H_READINESS_BARS and not missing,
    }


def prune_regime_history(conn: Any, asset: str, through: Any) -> int:
    """Retain the complete 4h window needed by the active regime scorer."""
    cutoff = _utc(through) - timedelta(days=REGIME_4H_RETAIN_DAYS)
    cursor = conn.execute(
        """
        DELETE FROM regime_4h_bars
         WHERE asset = ? AND source = ? AND bar_version = ? AND source_end <= ?
        """,
        (str(asset).upper(), REGIME_4H_SOURCE, REGIME_4H_BAR_VERSION, cutoff.isoformat()),
    )
    return int(cursor.rowcount or 0)


def ensure_asset_ready(
    conn: Any,
    asset: str,
    cutoff: Any,
    *,
    fetcher: Callable[[str, int, int], Iterable[Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Backfill and assess one asset without affecting other assets."""
    init_regime_history_schema(conn)
    asset = str(asset).upper()
    fetch_from, required_from, through = _required_window(cutoff)
    current = _utc(now or datetime.now(timezone.utc))
    coverage = _coverage(conn, asset, required_from, through)
    if coverage["ready"]:
        prune_regime_history(conn, asset, through)
        coverage = _coverage(conn, asset, required_from, through)
        conn.execute(
            """
            INSERT INTO regime_4h_backfill_jobs
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
        "SELECT status, lease_until, attempts FROM regime_4h_backfill_jobs WHERE asset = ?",
        (asset,),
    ).fetchone()
    if row and row[0] == "running" and row[1]:
        try:
            if _utc(row[1]) > current:
                return {"asset": asset, "status": "retryable", **coverage, "reason": "lease_active"}
        except (TypeError, ValueError, OverflowError):
            pass
    attempts = int(row[2]) + 1 if row else 1
    conn.execute(
        """
        INSERT INTO regime_4h_backfill_jobs
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
        raw_rows = list((fetcher or fetch_bybit_4h)(
            asset, int(fetch_from.timestamp() * 1000), int(through.timestamp() * 1000)
        ))
        retrieved_at = _utc(now or datetime.now(timezone.utc))
        normalized = {}
        for row in raw_rows:
            item = _normalize_row(asset, row, retrieved_at)
            end = _utc(item["bar_end"])
            if fetch_from < end <= through:
                if end in normalized:
                    raise RegimeHistoryError("duplicate_4h_bar")
                normalized[end] = item
        for item in normalized.values():
            conn.execute(
                """
                INSERT OR IGNORE INTO regime_4h_bars
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
        coverage = _coverage(conn, asset, required_from, through)
        status = "ready" if coverage["ready"] else "retryable"
        last_error = None if coverage["ready"] else "incomplete_or_gapped_4h_history"
        if status == "ready":
            prune_regime_history(conn, asset, through)
            coverage = _coverage(conn, asset, required_from, through)
        conn.execute(
            """
            UPDATE regime_4h_backfill_jobs
               SET status=?, covered_bars=?, missing_bars=?, lease_until=NULL,
                   next_retry_at=?, last_error=?, updated_at=?
             WHERE asset=?
            """,
            (status, coverage["covered_bars"], coverage["missing_bars"],
             None if status == "ready" else (current + timedelta(minutes=5)).isoformat(),
             last_error, current.isoformat(), asset),
        )
        conn.commit()
        return {"asset": asset, "status": status, **coverage}
    except Exception as exc:
        conn.execute(
            """
            UPDATE regime_4h_backfill_jobs
               SET status='retryable', lease_until=NULL, next_retry_at=?,
                   last_error=?, updated_at=?
             WHERE asset=?
            """,
            ((current + timedelta(minutes=5)).isoformat(), type(exc).__name__, current.isoformat(), asset),
        )
        conn.commit()
        return {"asset": asset, "status": "retryable", **coverage, "error": type(exc).__name__}


def load_regime_4h_bars(conn: Any, asset: str, cutoff: Any, limit: int = 84):
    """Load completed direct bars with stable provenance for regime scoring."""
    import polars as pl

    cutoff = _utc(cutoff)
    rows = conn.execute(
        """
        SELECT bar_id, bar_end, open, high, low, close, volume, source, bar_version
          FROM regime_4h_bars
         WHERE asset = ? AND source_end <= ? AND source = ? AND bar_version = ?
         ORDER BY source_end DESC LIMIT ?
        """,
        (str(asset).upper(), cutoff.isoformat(), REGIME_4H_SOURCE, REGIME_4H_BAR_VERSION, limit),
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
