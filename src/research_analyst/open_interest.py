"""Cutoff-bound Bybit open-interest enrichment for trade-quality scoring."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone
from statistics import median
from typing import Any, Mapping, Sequence

import httpx

import config


INTERVAL = "5m"
SOURCE_VERSION = "bybit-open-interest-v1"
LOOKBACK = 200
MIN_OBSERVATIONS = 32


def _utc(value: Any) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    parsed = _utc(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS oi_observations (
          venue TEXT NOT NULL, native_symbol TEXT NOT NULL, asset TEXT NOT NULL,
          interval TEXT NOT NULL, source_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
          open_interest REAL NOT NULL, source_version TEXT NOT NULL,
          PRIMARY KEY (venue, native_symbol, interval, source_at)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_oi_observations_asset_cutoff "
        "ON oi_observations (asset, interval, source_at)"
    )


def insert_observations(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> int:
    """Insert valid immutable observations and return the inserted count."""
    init_schema(conn)
    inserted = 0
    for row in rows:
        source_at = _iso(row.get("source_at"))
        retrieved_at = _iso(row.get("retrieved_at"))
        oi = row.get("open_interest")
        if not source_at or not retrieved_at:
            continue
        try:
            oi = float(oi)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(oi) or oi < 0:
            continue
        before = conn.total_changes
        conn.execute(
            """INSERT OR IGNORE INTO oi_observations
            (venue, native_symbol, asset, interval, source_at, retrieved_at,
             open_interest, source_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(row.get("venue") or "bybit"), str(row.get("native_symbol") or ""),
             str(row.get("asset") or ""), str(row.get("interval") or INTERVAL),
             source_at, retrieved_at, oi, str(row.get("source_version") or SOURCE_VERSION)),
        )
        inserted += int(conn.total_changes > before)
    return inserted


def load_observations(
    conn: sqlite3.Connection, venue: str, native_symbol: str, interval: str,
    cutoff: Any, *, limit: int = LOOKBACK,
) -> list[dict[str, Any]]:
    cutoff_iso = _iso(cutoff)
    if not cutoff_iso:
        return []
    rows = conn.execute(
        """SELECT source_at, retrieved_at, open_interest, source_version
           FROM oi_observations
          WHERE venue=? AND native_symbol=? AND interval=? AND source_at <= ?
          ORDER BY source_at DESC LIMIT ?""",
        (venue, native_symbol, interval, cutoff_iso, max(1, int(limit))),
    ).fetchall()
    return [
        {"source_at": row[0], "retrieved_at": row[1], "open_interest": float(row[2]),
         "source_version": row[3]}
        for row in reversed(rows)
    ]


def fetch_bybit_oi(
    asset: str, start_ms: int, end_ms: int, *, client: Any = None,
) -> list[dict[str, Any]]:
    """Fetch completed 5m Bybit OI points; caller owns persistence."""
    native = str(asset).upper()
    if not native.endswith("USDT"):
        native = f"{native}USDT"
    params = {"category": "linear", "symbol": native, "intervalTime": "5min",
              "startTime": int(start_ms), "endTime": int(end_ms), "limit": LOOKBACK}
    owns_client = client is None
    http = client or httpx.Client(timeout=20.0)
    try:
        response = http.get(f"{config.BYBIT_LINEAR_BASE_URL.rstrip('/')}/v5/market/open-interest", params=params)
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode", 0) != 0:
            raise RuntimeError(str(payload.get("retMsg") or "Bybit OI request failed"))
        retrieved = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        out = []
        for item in (payload.get("result") or {}).get("list") or []:
            source_ms = int(item.get("timestamp"))
            if source_ms > int(end_ms):
                continue
            out.append({"venue": "bybit", "native_symbol": native, "asset": str(asset),
                        "interval": INTERVAL,
                        "source_at": datetime.fromtimestamp(source_ms / 1000, timezone.utc),
                        "retrieved_at": retrieved,
                        "open_interest": float(item.get("openInterest")),
                        "source_version": SOURCE_VERSION})
        return out
    finally:
        if owns_client:
            http.close()


def collect_candidate_oi(
    conn: sqlite3.Connection, assets: Sequence[str], cutoff: datetime | str,
    *, client: Any = None,
) -> dict[str, int | str]:
    """Fetch and persist the bounded OI history for emitted candidate assets."""
    end = _utc(cutoff)
    if end is None:
        return {str(asset): "invalid cutoff" for asset in assets}
    end_ms = int(end.timestamp() * 1000)
    start_ms = end_ms - (LOOKBACK - 1) * 5 * 60_000
    results: dict[str, int | str] = {}
    for asset in dict.fromkeys(str(a) for a in assets):
        try:
            rows = fetch_bybit_oi(asset, start_ms, end_ms, client=client)
            results[asset] = insert_observations(conn, rows)
        except Exception as exc:  # optional enrichment never fails admission
            results[asset] = f"error: {exc}"[:200]
    conn.commit()
    return results


def oi_participation_score(
    candidate: Mapping[str, Any], observations: Sequence[Mapping[str, Any]],
    price_closes: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Return a neutral/support/contradict OI participation observation."""
    valid = []
    for row in observations:
        try:
            value = float(row.get("open_interest"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            valid.append(value)
    if len(valid) < MIN_OBSERVATIONS:
        return {"value": 0.5, "status": "unavailable", "reason": "OI history is unavailable",
                "observations": len(valid)}
    current = valid[-1]
    baseline = median(valid[:-1])
    change = current / baseline - 1.0 if baseline > 0 else 0.0
    closes = [float(v) for v in (price_closes or []) if isinstance(v, (int, float)) and math.isfinite(float(v))]
    if len(closes) >= 2:
        price_change = closes[-1] / closes[0] - 1.0 if closes[0] else 0.0
    else:
        price_change = float(candidate.get("price_return") or 0.0)
    direction = str(candidate.get("direction") or "").lower()
    aligned = (direction == "long" and price_change > 0) or (direction == "short" and price_change < 0)
    if abs(change) < 0.001:
        value, status = 0.5, "neutral"
    elif aligned and change > 0:
        value, status = 0.8, "support"
    elif aligned and change < 0:
        value, status = 0.55, "neutral"
    else:
        value, status = 0.35, "contradict"
    return {"value": value, "status": status, "reason": "OI participation evaluated",
            "observations": len(valid), "oi_change": change, "price_change": price_change}
