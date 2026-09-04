"""Public WebSocket market-data ingestion (replaces REST polling for base timeframes).

Design (see specs/ws-ingestion.md):
- Bybit is the default public source (WS_BYBIT_ENABLED=true); Binance is opt-in
  (WS_BINANCE_ENABLED=false).
- We stream **1m + 5m kline** (plus markPrice). 15m/1h/4h are resampled locally
  from the 5m base by the writer task and persisted as derived observations, so
  existing evaluators (which read source_observations for 15m/HTF) work unchanged.
- Native bars are stamped source=bybit_ws/binance_ws, data_purity=pure_ws (starts
  with "pure_" so the alpha_outbox emit gate accepts them; the CA-truth failover
  gate still blocks non-pure venue_agg rows).
- Single writer: all DB writes (live bars + resampled) happen in one asyncio task
   over one SQLite connection, preserving single-writer discipline for market.sqlite3.

Run:  python src/research_analyst/ws_gateway.py  # foreground daemon from repo root
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import polars as pl

import config
from strategy_v2_context import load_bars_for_interval, resample_ohlcv
from db_maintenance import prune_market_db, vacuum_sqlite
from evaluation_trigger import publish as publish_evaluation_trigger


MARKET_KIND = "usdt_perp"
BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"
BINANCE_WS_URL = "wss://fstream.binance.com/stream"

# How far back the resample window looks when building derived TFs from 5m.
RESAMPLE_LOOKBACK_MIN = int(os.getenv("WS_RESAMPLE_LOOKBACK_MIN", "1440"))  # 24h of 5m

# Streamed base timeframes -> exchange-specific tokens.
STREAMED_TFS = [t.strip() for t in os.getenv("WS_STREAM_TIMEFRAMES", "1m,5m").split(",") if t.strip()]
WS_MESSAGE_TIMEOUT_SECONDS = 90
_LAST_MARKET_MAINTENANCE = 0.0
_LAST_MARKET_VACUUM = 0.0
WS_STALE_SECONDS = int(os.getenv("WS_STALE_SECONDS", "180"))
_STARTED_MONOTONIC = time.monotonic()
_HEALTH = {
    "last_message_at": None, "last_bar_at": None, "active_connections": 0,
    "reconnect_count": 0, "last_error": None, "subscribed_count": 0,
    "feed_id": None, "fallback_state": None,
}


def _write_health(status: str) -> None:
    path = config.DEFAULT_DB_DIR / "ws_health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"service": "ws_gateway", "status": status, "ts": datetime.now(timezone.utc).isoformat(), **_HEALTH}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def _record_message(is_bar: bool = False) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _HEALTH["last_message_at"] = now
    if is_bar:
        _HEALTH["last_bar_at"] = now


async def health_monitor() -> None:
    while True:
        last_bar = _HEALTH["last_bar_at"]
        stale = (
            (datetime.now(timezone.utc) - datetime.fromisoformat(last_bar)).total_seconds() > WS_STALE_SECONDS
            if last_bar
            else time.monotonic() - _STARTED_MONOTONIC > WS_STALE_SECONDS
        )
        _write_health("stale" if stale else "healthy")
        if stale and _HEALTH["active_connections"] > 0:
            raise RuntimeError(f"WebSocket feed stale for more than {WS_STALE_SECONDS}s")
        await asyncio.sleep(10)
BYBIT_TF_TOKEN = {"1m": "1", "5m": "5", "15m": "15"}
BINANCE_TF_STREAM = {"1m": "1m", "5m": "5m", "15m": "15m"}


# --------------------------------------------------------------------------- #
# Universe selection
# --------------------------------------------------------------------------- #
def load_rotated_bases() -> List[str]:
    """Bases fed by the rotation feed (analog of binance_oi_rotation) when enabled.

    Tolerant: returns [] if the feed file is missing or unparsable.
    """
    path = getattr(config, "BINANCE_OI_ROTATION_FEED_PATH", "")
    p = Path(path) if path else None
    if not p or not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: List[str] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("candidates") or data.get("symbols") or []
    else:
        return []
    for it in items:
        if isinstance(it, str):
            sym = it
        elif isinstance(it, dict):
            sym = it.get("symbol") or it.get("asset") or it.get("native_symbol") or ""
        else:
            continue
        sym = str(sym).upper().replace("USDT", "").replace("_PERP", "").replace(".A", "")
        if sym:
            out.append(sym)
    return out


def _open_position_assets(now: datetime | None = None) -> set[str]:
    """Keep fresh open positions subscribed after rotation removes their assets."""
    snapshot_dir = getattr(config, "EXECUTOR_SNAPSHOT_DIR", "") or ""
    if not snapshot_dir:
        return set()
    base = Path(snapshot_dir)
    if not base.exists():
        return set()
    now = now or datetime.now(timezone.utc)
    assets: set[str] = set()
    account_dirs = sorted(path for path in base.glob("*/*") if path.is_dir())
    for account_dir in account_dirs:
        latest = account_dir / "latest.json"
        snapshot = None
        for attempt in range(3):
            try:
                snapshot = json.loads(latest.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError, json.JSONDecodeError):
                if attempt < 2:
                    time.sleep(0.02)
        if snapshot is None:
            continue
        try:
            snapshot_at = datetime.fromisoformat(str(snapshot["timestamp"]).replace("Z", "+00:00"))
            if snapshot_at.tzinfo is None:
                snapshot_at = snapshot_at.replace(tzinfo=timezone.utc)
            age = (now - snapshot_at.astimezone(timezone.utc)).total_seconds()
            if age < 0 or age > float(getattr(config, "DATA_FRESHNESS_MAX_SECONDS", 600)):
                continue
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        for position in snapshot.get("positions", []):
            if str(position.get("status", "")).upper() != "OPEN":
                continue
            original = {}
            try:
                original = json.loads(position.get("original_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            asset = original.get("asset")
            if not asset and position.get("symbol"):
                asset = str(position["symbol"]).split("/")[0]
            if asset:
                assets.add(str(asset).upper().removesuffix("USDT"))
    return assets


def select_universe() -> List[str]:
    """Return rotated symbols plus permanent and open-position assets."""
    from symbol_rotation import subscription_assets

    now = datetime.now(timezone.utc)
    bases, metadata = subscription_assets(now)
    carryover = _open_position_assets(now)
    if carryover:
        bases = sorted(set(bases) | carryover)
        metadata = dict(metadata)
        metadata["position_carryover_symbols"] = sorted(carryover)
    _HEALTH["subscribed_count"] = len(bases)
    _HEALTH["feed_id"] = metadata.get("feed_id")
    _HEALTH["fallback_state"] = metadata.get("fallback_reason")
    return sorted(set(b.strip().upper() for b in bases if b and b.strip()))


def subscription_state(at: datetime | None = None) -> tuple[List[str], dict]:
    """Expose symbols plus feed identity for the reconciliation supervisor."""
    return select_universe_at(at or datetime.now(timezone.utc))


def select_universe_at(at: datetime) -> tuple[List[str], dict]:
    """Return a deterministic subscription snapshot for a supplied timestamp."""
    from symbol_rotation import subscription_assets

    bases, metadata = subscription_assets(at)
    carryover = _open_position_assets(at)
    if carryover:
        bases = sorted(set(bases) | carryover)
        metadata = dict(metadata)
        metadata["position_carryover_symbols"] = sorted(carryover)
    return sorted(set(b.strip().upper() for b in bases if b and b.strip())), metadata


class SubscriptionSupervisor:
    """Reconcile feed versions without blocking provider reads.

    The provider task is cancelled and replaced only after the desired feed
    version changes. Reconciliation is therefore idempotent and a repeated
    feed cannot duplicate streams.
    """

    def __init__(self, initial: List[str] | None = None, feed: dict | None = None):
        self.bases = initial if initial is not None else select_universe()
        self.feed = feed or {}

    def reconcile(self, bases: List[str], feed: dict | None = None) -> dict:
        desired = sorted(set(str(base).strip().upper() for base in bases if str(base).strip()))
        if not desired:
            desired = list(self.bases)
        next_feed = feed or {}
        changed = desired != self.bases or next_feed.get("feed_id") != self.feed.get("feed_id")
        result = {
            "changed": changed,
            "added": sorted(set(desired) - set(self.bases)),
            "removed": sorted(set(self.bases) - set(desired)),
            "feed_id": next_feed.get("feed_id"),
        }
        if changed:
            self.bases[:] = desired
            self.feed = next_feed
        return result


# --------------------------------------------------------------------------- #
# Stream planning
# --------------------------------------------------------------------------- #
def plan_bybit_streams(symbols: List[str], shard: int) -> List[List[str]]:
    """Shard Bybit topics (kline.<tf>.<SYM> + optional markPrice.<SYM>) per connection."""
    shard = max(1, shard)
    perp = config.expand_perp_symbols(symbols, "bybit")  # BTCUSDT
    shards: List[List[str]] = []
    for sym in perp:
        if not shards or len(shards[-1]) >= shard:
            shards.append([])
        for tf in STREAMED_TFS:
            token = BYBIT_TF_TOKEN.get(tf, tf)
            shards[-1].append(f"kline.{token}.{sym}")
        # Bybit does not expose mark price as a public markPrice.<symbol> topic.
        # Sending it with the kline topics makes the shard subscription fail.
    return shards


def plan_binance_streams(symbols: List[str]) -> List[str]:
    """Single combined-connection stream list (kline_<tf> + optional markPrice@1s)."""
    perp = config.expand_perp_symbols(symbols, "binance")  # BTCUSDT
    streams: List[str] = []
    for sym in perp:
        for tf in STREAMED_TFS:
            streams.append(f"{sym.lower()}@kline_{BINANCE_TF_STREAM.get(tf, tf)}")
        if getattr(config, "WS_MARKPRICE_ENABLED", True):
            streams.append(f"{sym.lower()}@markPrice@1s")
    return streams


# --------------------------------------------------------------------------- #
# Message normalization  (WS raw -> unified bar/mark record)
# --------------------------------------------------------------------------- #
def _base_from_perp(symbol: str) -> str:
    return symbol.upper().replace("USDT", "").replace("_PERP", "").replace(".A", "")


def _bybit_interval_from_topic(topic: str) -> str:
    token = topic.split(".")[1] if topic.startswith("kline.") else ""
    rev = {v: k for k, v in BYBIT_TF_TOKEN.items()}
    return rev.get(token, token)


def normalize_bybit_kline(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = msg.get("data")
    if not isinstance(data, list):
        data = [data] if isinstance(data, dict) else None
    if not data:
        return None
    rec = data[0]
    sym = str(rec.get("symbol") or msg.get("topic", "").split(".")[-1])
    interval = _bybit_interval_from_topic(msg.get("topic", ""))
    try:
        return {
            "native_symbol": sym,
            "asset": _base_from_perp(sym),
            "interval": interval,
            "open": float(rec["open"]),
            "high": float(rec["high"]),
            "low": float(rec["low"]),
            "close": float(rec["close"]),
            "volume": float(rec.get("volume", 0) or 0),
            "source_start_ms": int(rec["start"]),
            "source_end_ms": int(rec["end"]),
            "confirm": int(rec.get("confirm", 1) or 1),
        }
    except (KeyError, TypeError, ValueError):
        return None


def normalize_bybit_mark(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    d = msg.get("data")
    if not isinstance(d, dict):
        return None
    sym = str(d.get("symbol") or msg.get("topic", "").split(".")[-1])
    try:
        return {
            "native_symbol": sym,
            "asset": _base_from_perp(sym),
            "interval": "mark",
            "mark_price": float(d["markPrice"]),
            "funding_rate": float(d.get("fundingRate", 0) or 0),
            "source_end_ms": int(d.get("time", 0) or 0),
        }
    except (KeyError, TypeError, ValueError):
        return None


def normalize_binance_kline(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    k = msg.get("k")
    if not isinstance(k, dict):
        return None
    sym = str(k.get("s") or msg.get("s") or "")
    try:
        return {
            "native_symbol": sym,
            "asset": _base_from_perp(sym),
            "interval": str(k.get("i", "1m")),
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k.get("v", 0) or 0),
            "source_start_ms": int(k["t"]),
            "source_end_ms": int(k["T"]),
            "confirm": 1 if k.get("x") else 0,
        }
    except (KeyError, TypeError, ValueError):
        return None


def normalize_binance_mark(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if msg.get("e") != "markPriceUpdate":
        return None
    sym = str(msg.get("s", ""))
    try:
        return {
            "native_symbol": sym,
            "asset": _base_from_perp(sym),
            "interval": "mark",
            "mark_price": float(msg["p"]),
            "funding_rate": float(msg.get("r", 0) or 0),
            "source_end_ms": int(msg.get("E", 0) or 0),
        }
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Observation id + payload shaping
# --------------------------------------------------------------------------- #
def make_observation_id(source: str, venue: str, native_symbol: str, interval: str, source_end: datetime) -> str:
    mat = "|".join((source, venue, native_symbol, interval, source_end.isoformat()))
    return hashlib.sha256(mat.encode("utf-8")).hexdigest()


def _ts(ms: Any) -> datetime:
    if ms is None or ms <= 0:
        return datetime.now(timezone.utc)
    val = float(ms)
    if val > 1e12:
        val /= 1000.0
    return datetime.fromtimestamp(val, tz=timezone.utc)


def _row_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    return _ts(value)


def bar_record_to_row(rec: Dict[str, Any], source: str, venue: str, retrieval_kind: str) -> Dict[str, Any]:
    """Shape a normalized kline record into a source_observations row dict."""
    end = _ts(rec["source_end_ms"])
    start = _ts(rec["source_start_ms"]) if rec.get("source_start_ms") else end
    payload = {
        "open": rec["open"], "high": rec["high"], "low": rec["low"], "close": rec["close"],
        "volume": rec["volume"], "open_interest": None, "funding_rate": None,
    }
    return {
        "observation_id": make_observation_id(source, venue, rec["native_symbol"], rec["interval"], end),
        "source": source, "venue": venue, "native_symbol": rec["native_symbol"],
        "asset": rec["asset"], "market_kind": MARKET_KIND, "interval": rec["interval"],
        "source_start": start, "source_end": end,
        "retrieved_at": datetime.now(timezone.utc), "retrieval_kind": retrieval_kind,
        "payload_json": json.dumps(payload),
    }


def mark_record_to_row(rec: Dict[str, Any], source: str, venue: str, retrieval_kind: str) -> Dict[str, Any]:
    end = _ts(rec["source_end_ms"])
    payload = {"mark_price": rec["mark_price"], "funding_rate": rec["funding_rate"]}
    return {
        "observation_id": make_observation_id(source, venue, rec["native_symbol"], "mark", end),
        "source": source, "venue": venue, "native_symbol": rec["native_symbol"],
        "asset": rec["asset"], "market_kind": MARKET_KIND, "interval": "mark",
        "source_start": end, "source_end": end,
        "retrieved_at": datetime.now(timezone.utc), "retrieval_kind": retrieval_kind,
        "payload_json": json.dumps(payload),
    }


# --------------------------------------------------------------------------- #
# Backfill (REST seed) — best effort, runs before the live stream
# --------------------------------------------------------------------------- #
_PENDING: List[Dict[str, Any]] = []


def _queue_row(row: Dict[str, Any]) -> None:
    _PENDING.append(row)


def flush_pending() -> int:
    if not _PENDING:
        return 0
    rows = _PENDING.copy()
    _PENDING.clear()
    conn = config.get_db_connection(db_path=config.MARKET_DB_PATH)
    try:
        _executemany_rows(conn, rows)
    finally:
        conn.close()
    return len(rows)


def backfill_via_rest(provider: str, symbols: List[str], hours: int) -> int:
    """Seed recent 1m + 5m history via REST so evaluators have a warm window immediately."""
    source = config.BYBIT_WS_SOURCE if provider == "bybit" else config.BINANCE_WS_SOURCE
    venue = provider
    written = 0
    with httpx.Client(timeout=20) as client:
        for sym in config.expand_perp_symbols(symbols, provider):
            for tf in STREAMED_TFS:
                try:
                    if provider == "bybit":
                        token = BYBIT_TF_TOKEN.get(tf, tf)
                        r = client.get("https://api.bybit.com/v5/market/kline", params={
                            "category": "linear", "symbol": sym, "interval": token, "limit": max(2, hours * 60 // (5 if tf == "5m" else 1)),
                        })
                        js = r.json()
                        rows = js.get("result", {}).get("list", []) if isinstance(js.get("result"), dict) else []
                        for row in reversed(rows):
                            rec = {
                                "native_symbol": sym, "asset": _base_from_perp(sym), "interval": tf,
                                "open": float(row[1]), "high": float(row[2]), "low": float(row[3]),
                                "close": float(row[4]), "volume": float(row[5]),
                                "source_start_ms": int(row[0]), "source_end_ms": int(row[0]) + (300000 if tf == "5m" else 60000),
                                "confirm": 1,
                            }
                            _queue_row(bar_record_to_row(rec, source, venue, "backfill"))
                            written += 1
                    else:
                        r = client.get("https://fapi.binance.com/fapi/v1/klines", params={
                            "symbol": sym, "interval": BINANCE_TF_STREAM.get(tf, tf), "limit": max(2, hours * 60 // (5 if tf == "5m" else 1)),
                        })
                        for row in r.json():
                            rec = {
                                "native_symbol": sym, "asset": _base_from_perp(sym), "interval": tf,
                                "open": float(row[1]), "high": float(row[2]), "low": float(row[3]),
                                "close": float(row[4]), "volume": float(row[5]),
                                "source_start_ms": int(row[0]), "source_end_ms": int(row[6]),
                                "confirm": 1,
                            }
                            _queue_row(bar_record_to_row(rec, source, venue, "backfill"))
                            written += 1
                except Exception as e:
                    print(f"[backfill] {provider} {sym} {tf} failed: {e}")
    flush_pending()
    return written


# --------------------------------------------------------------------------- #
# Writer task: single DB writer (live bars + resample from 5m)
# --------------------------------------------------------------------------- #
def _executemany_rows(conn, rows: List[Dict[str, Any]]) -> None:
    conn.executemany(
        """
        INSERT OR IGNORE INTO source_observations
        (observation_id, source, venue, native_symbol, asset, market_kind,
         interval, source_start, source_end, retrieved_at, retrieval_kind, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(observation_id) DO UPDATE SET
            source_start=excluded.source_start,
            source_end=excluded.source_end,
            retrieved_at=excluded.retrieved_at,
            retrieval_kind=excluded.retrieval_kind,
            payload_json=excluded.payload_json
        """,
        [
            (
                r["observation_id"], r["source"], r["venue"], r["native_symbol"], r["asset"],
                r["market_kind"], r["interval"], r["source_start"], r["source_end"],
                r["retrieved_at"], r["retrieval_kind"], r["payload_json"],
            )
            for r in rows
        ],
    )
    conn.commit()


def _publish_base_triggers(rows: List[Dict[str, Any]]) -> None:
    """Publish each newly committed base-bar cutoff exactly once per interval."""
    cutoffs = {
        (row.get("interval"), row.get("source_end"))
        for row in rows
        if row.get("interval") in ("1m", "5m") and row.get("source_end") is not None
    }
    for interval, cutoff in sorted(cutoffs, key=lambda item: (item[1], item[0])):
        publish_evaluation_trigger(cutoff, interval=interval)


def resample_and_persist(conn, bases: List[str], now: datetime, ws_source: str) -> int:
    """Build 15m/1h/4h derived bars from recent 5m and upsert them.

    5m is streamed directly; 1m is used as-is. Derived bars keep the originating
    ws source (e.g. bybit_ws) so the emit-gate purity check (pure_*) and
    _get_bar_purity treat them as pure.
    """
    written = 0
    start = now - timedelta(minutes=RESAMPLE_LOOKBACK_MIN)
    for asset in bases:
        bars = load_bars_for_interval(
            conn, asset, "5m", now,
            lookback_days=max(1, RESAMPLE_LOOKBACK_MIN // 1440),
        )
        if bars.height < 3:
            continue
        df = bars
        # Very old test/backfill rows used source_start == source_end and
        # represented bar starts. Live rows use the canonical exclusive end.
        # Normalize that legacy shape before applying the shared resampler.
        stamped = conn.execute(
            """SELECT COUNT(*), SUM(source_start = source_end)
               FROM source_observations
              WHERE asset=? AND interval='5m' AND source_end <= ? AND source_end >= ?""",
            (asset, now, start),
        ).fetchone()
        if stamped and stamped[0] and stamped[0] == stamped[1]:
            df = df.with_columns((pl.col("timestamp") + pl.duration(minutes=5)).alias("timestamp"))
        for every in ("15m", "1h", "4h"):
            res = resample_ohlcv(df, every)
            if res.is_empty():
                continue
            for t, o, h, l, c, v, provenance, purity in zip(
                res["timestamp"].to_list(), res["open"].to_list(), res["high"].to_list(),
                res["low"].to_list(), res["close"].to_list(), res["volume"].to_list(),
                res["source_provenance"].to_list(), res["data_purity"].to_list(),
            ):
                end = (_ts(int(t.timestamp() * 1000)) if hasattr(t, "timestamp") else _ts(int(t)))
                row = {
                    "observation_id": make_observation_id(ws_source, "derived", asset + "USDT", every, end),
                    "source": ws_source, "venue": "derived", "native_symbol": asset + "USDT",
                    "asset": asset, "market_kind": MARKET_KIND, "interval": every,
                    "source_start": end - timedelta(minutes={"15m": 15, "1h": 60, "4h": 240}[every]),
                    "source_end": end,
                    "retrieved_at": now, "retrieval_kind": "resampled",
                    "payload_json": json.dumps({
                        "open": o, "high": h, "low": l, "close": c, "volume": v,
                        "open_interest": None, "funding_rate": None,
                        "provenance": {"sources": provenance, "base_interval": "5m",
                                       "source_end": end.isoformat(), "data_purity": purity},
                    }),
                }
                _PENDING.append(row)
                written += 1
    if _PENDING:
        _executemany_rows(conn, _PENDING.copy())
        _PENDING.clear()
    return written


def _maybe_prune_market(conn, now: datetime) -> None:
    """Run market retention on the gateway's single writer connection."""
    global _LAST_MARKET_MAINTENANCE, _LAST_MARKET_VACUUM
    if not getattr(config, "DB_MAINTENANCE_ENABLED", True):
        return
    current = time.monotonic()
    interval = max(60, int(getattr(config, "DB_MAINTENANCE_INTERVAL_SECONDS", 3600)))
    if current - _LAST_MARKET_MAINTENANCE < interval:
        return
    _LAST_MARKET_MAINTENANCE = current
    try:
        result = prune_market_db(conn, now)
        deleted = sum(result.values())
        vacuum_interval = max(
            interval, int(getattr(config, "DB_MAINTENANCE_VACUUM_INTERVAL_SECONDS", 86400))
        )
        if deleted and current - _LAST_MARKET_VACUUM >= vacuum_interval:
            vacuum_sqlite(conn)
            _LAST_MARKET_VACUUM = current
        print(f"Market database maintenance: {result}", flush=True)
    except Exception as exc:
        print(f"Market database maintenance failed: {exc}", file=sys.stderr, flush=True)


async def writer_task(queue: asyncio.Queue, bases: List[str], ws_source: str) -> None:
    conn = config.get_db_connection(db_path=config.MARKET_DB_PATH)
    batch: List[Dict[str, Any]] = []
    last_resample = time.monotonic()
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=1.0)
                batch.append(item)
                if len(batch) >= 500:
                    _executemany_rows(conn, batch)
                    _publish_base_triggers(batch)
                    batch.clear()
            except asyncio.TimeoutError:
                if batch:
                    _executemany_rows(conn, batch)
                    _publish_base_triggers(batch)
                    batch.clear()
                if time.monotonic() - last_resample >= 60:
                    resample_and_persist(conn, bases, datetime.now(timezone.utc), ws_source)
                    last_resample = time.monotonic()
                _maybe_prune_market(conn, datetime.now(timezone.utc))
    finally:
        if batch:
            _executemany_rows(conn, batch)
        resample_and_persist(conn, bases, datetime.now(timezone.utc), ws_source)
        conn.close()


# --------------------------------------------------------------------------- #
# Provider connections (async, lazy import websockets)
# --------------------------------------------------------------------------- #
async def _bybit_conn(topics: List[str], queue: asyncio.Queue, source: str) -> None:
    import websockets

    venue = "bybit"
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(BYBIT_WS_URL, ping_interval=15, ping_timeout=10) as ws:
                backoff = 1.0
                _HEALTH["active_connections"] += 1
                await ws.send(json.dumps({"op": "subscribe", "args": topics}))

                async def heartbeat() -> None:
                    while True:
                        await asyncio.sleep(20)
                        await ws.send(json.dumps({"op": "ping"}))

                heartbeat_task = asyncio.create_task(heartbeat())
                try:
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=WS_MESSAGE_TIMEOUT_SECONDS)
                        _record_message()
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if msg.get("op") == "subscribe" and not msg.get("success", True):
                            print(f"[bybit] subscription rejected: {msg}")
                        topic = msg.get("topic", "")
                        if topic.startswith("kline."):
                            rec = normalize_bybit_kline(msg)
                            if rec and rec.get("confirm"):
                                _record_message(is_bar=True)
                                queue.put_nowait(bar_record_to_row(rec, source, venue, "stream"))
                        elif topic.startswith("markPrice."):
                            rec = normalize_bybit_mark(msg)
                            if rec:
                                queue.put_nowait(mark_record_to_row(rec, source, venue, "stream"))
                finally:
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
        except Exception as e:
            _HEALTH["active_connections"] = max(0, _HEALTH["active_connections"] - 1)
            _HEALTH["reconnect_count"] += 1
            _HEALTH["last_error"] = str(e)[:500]
            _write_health("reconnecting")
            print(f"[bybit] connection error: {e}; retrying in {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def _binance_conn(streams: List[str], queue: asyncio.Queue, source: str) -> None:
    import websockets

    venue = "binance"
    backoff = 1.0
    while True:
        try:
            url = BINANCE_WS_URL + "/".join(streams)
            async with websockets.connect(url, ping_interval=15, ping_timeout=10) as ws:
                backoff = 1.0
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=WS_MESSAGE_TIMEOUT_SECONDS)
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    data = msg.get("data", msg)
                    etype = data.get("e")
                    if etype == "kline":
                        rec = normalize_binance_kline(data)
                        if rec and rec.get("confirm"):
                            queue.put_nowait(bar_record_to_row(rec, source, venue, "stream"))
                    elif etype == "markPriceUpdate":
                        rec = normalize_binance_mark(data)
                        if rec:
                            queue.put_nowait(mark_record_to_row(rec, source, venue, "stream"))
        except Exception as e:
            print(f"[binance] connection error: {e}; retrying in {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def _run(provider: str, bases: List[str], queue: asyncio.Queue, source: str) -> None:
    if provider == "bybit":
        shards = plan_bybit_streams(bases, config.WS_BYBIT_SHARD)
        await asyncio.gather(*(_bybit_conn(s, queue, source) for s in shards))
    else:
        streams = plan_binance_streams(bases)
        await _binance_conn(streams, queue, source)


async def _supervise_provider(provider: str, supervisor: SubscriptionSupervisor,
                              queue: asyncio.Queue, source: str) -> None:
    """Keep live streams aligned with the current durable feed version."""
    stream_task = asyncio.create_task(_run(provider, supervisor.bases, queue, source))
    # Each provider owns its observed feed version even when the symbol list is
    # shared, so Bybit reconciliation cannot hide a Binance update.
    observed_feed_id = supervisor.feed.get("feed_id")
    try:
        while True:
            await asyncio.sleep(5)
            desired, feed = subscription_state()
            if desired == supervisor.bases and feed.get("feed_id") == observed_feed_id:
                continue
            result = supervisor.reconcile(desired, feed)
            observed_feed_id = feed.get("feed_id")
            print(
                f"[ws_gateway] reconcile feed={result['feed_id']} "
                f"added={len(result['added'])} removed={len(result['removed'])} "
                f"subscribed={len(supervisor.bases)}"
            )
            stream_task.cancel()
            await asyncio.gather(stream_task, return_exceptions=True)
            stream_task = asyncio.create_task(_run(provider, supervisor.bases, queue, source))
            _HEALTH["subscribed_count"] = len(supervisor.bases)
            _HEALTH["feed_id"] = supervisor.feed.get("feed_id")
            _HEALTH["fallback_state"] = supervisor.feed.get("fallback_reason")
    finally:
        stream_task.cancel()
        await asyncio.gather(stream_task, return_exceptions=True)


async def run_async() -> None:
    config.init_market_db()
    bases, feed = subscription_state()
    if not bases:
        print("[ws_gateway] empty universe; check WS_SYMBOL_SOURCE / static_universe.json")
        return
    print(f"[ws_gateway] universe={len(bases)} symbols; bybit={config.WS_BYBIT_ENABLED} binance={config.WS_BINANCE_ENABLED}; streamed TFs={STREAMED_TFS}")
    _HEALTH["subscribed_count"] = len(bases)
    _HEALTH["feed_id"] = feed.get("feed_id")
    _HEALTH["fallback_state"] = feed.get("fallback_reason")

    if config.WS_BYBIT_ENABLED:
        n = backfill_via_rest("bybit", bases, config.WS_BACKFILL_HOURS)
        print(f"[ws_gateway] bybit backfill wrote {n} bars")
    if config.WS_BINANCE_ENABLED:
        n = backfill_via_rest("binance", bases, config.WS_BACKFILL_HOURS)
        print(f"[ws_gateway] binance backfill wrote {n} bars")

    queue: asyncio.Queue = asyncio.Queue()
    ws_source = config.BYBIT_WS_SOURCE if config.WS_BYBIT_ENABLED else config.BINANCE_WS_SOURCE
    _write_health("starting")
    supervisor = SubscriptionSupervisor(bases, feed)
    tasks = [writer_task(queue, supervisor.bases, ws_source), health_monitor()]
    if config.WS_BYBIT_ENABLED:
        tasks.append(_supervise_provider("bybit", supervisor, queue, config.BYBIT_WS_SOURCE))
    if config.WS_BINANCE_ENABLED:
        tasks.append(_supervise_provider("binance", supervisor, queue, config.BINANCE_WS_SOURCE))
    await asyncio.gather(*tasks)


def main() -> None:
    try:
        asyncio.run(run_async())
    except KeyboardInterrupt:
        print("[ws_gateway] stopped")


if __name__ == "__main__":
    main()
