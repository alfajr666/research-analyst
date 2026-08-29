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
  over one DuckDB connection, preserving single-writer discipline for DB_PATH.

Run:  python ws_gateway.py            # foreground daemon
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import polars as pl

import config
from strategy_v2_context import resample_ohlcv


MARKET_KIND = "usdt_perp"
BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"
BINANCE_WS_URL = "wss://fstream.binance.com/stream"

# How far back the resample window looks when building derived TFs from 5m.
RESAMPLE_LOOKBACK_MIN = int(os.getenv("WS_RESAMPLE_LOOKBACK_MIN", "1440"))  # 24h of 5m

# Streamed base timeframes -> exchange-specific tokens.
STREAMED_TFS = [t.strip() for t in os.getenv("WS_STREAM_TIMEFRAMES", "1m,5m").split(",") if t.strip()]
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


def select_universe() -> List[str]:
    """Return canonical base symbols for the configured WS_SYMBOL_SOURCE."""
    src = (getattr(config, "WS_SYMBOL_SOURCE", "static") or "static").lower()
    bases: List[str] = []
    if src in ("static", "both"):
        bases += config.load_static_symbols()
    if src in ("rotated", "both"):
        bases += load_rotated_bases()
    return sorted(set(b.strip().upper() for b in bases if b and b.strip()))


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
        if getattr(config, "WS_MARKPRICE_ENABLED", True):
            shards[-1].append(f"markPrice.{sym}")
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
    conn = config.get_db_connection()
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


def resample_and_persist(conn, bases: List[str], now: datetime, ws_source: str) -> int:
    """Build 15m/1h/4h derived bars from recent 5m and upsert them.

    5m is streamed directly; 1m is used as-is. Derived bars keep the originating
    ws source (e.g. bybit_ws) so the emit-gate purity check (pure_*) and
    _get_bar_purity treat them as pure.
    """
    written = 0
    start = now - timedelta(minutes=RESAMPLE_LOOKBACK_MIN)
    for asset in bases:
        rows = conn.execute(
            """
            SELECT source_end,
                   json_extract(payload_json,'$.open')::DOUBLE,
                   json_extract(payload_json,'$.high')::DOUBLE,
                   json_extract(payload_json,'$.low')::DOUBLE,
                   json_extract(payload_json,'$.close')::DOUBLE,
                   COALESCE(json_extract(payload_json,'$.volume')::DOUBLE,0.0)
            FROM source_observations
            WHERE asset=? AND interval='5m'
              AND source IN (?,?)
              AND source_end <= ? AND source_end >= ?
            ORDER BY source_end ASC
            LIMIT 400
            """,
            (asset, config.BYBIT_WS_SOURCE, config.BINANCE_WS_SOURCE, now, start),
        ).fetchall()
        if len(rows) < 3:
            continue
        df = pl.DataFrame(
            {
                "timestamp": [(_ts(int(r[0].timestamp() * 1000)) if hasattr(r[0], "timestamp") else _ts(int(r[0]))) for r in rows],
                "open": [float(r[1]) for r in rows],
                "high": [float(r[2]) for r in rows],
                "low": [float(r[3]) for r in rows],
                "close": [float(r[4]) for r in rows],
                "volume": [float(r[5]) for r in rows],
            }
        )
        for every in ("15m", "1h", "4h"):
            res = resample_ohlcv(df, every)
            if res.is_empty():
                continue
            for t, o, h, l, c, v in zip(
                res["timestamp"].to_list(), res["open"].to_list(), res["high"].to_list(),
                res["low"].to_list(), res["close"].to_list(), res["volume"].to_list(),
            ):
                end = (_ts(int(t.timestamp() * 1000)) if hasattr(t, "timestamp") else _ts(int(t)))
                row = {
                    "observation_id": make_observation_id(ws_source, "derived", asset + "USDT", every, end),
                    "source": ws_source, "venue": "derived", "native_symbol": asset + "USDT",
                    "asset": asset, "market_kind": MARKET_KIND, "interval": every,
                    "source_start": end, "source_end": end,
                    "retrieved_at": now, "retrieval_kind": "resampled",
                    "payload_json": json.dumps({
                        "open": o, "high": h, "low": l, "close": c, "volume": v,
                        "open_interest": None, "funding_rate": None,
                    }),
                }
                _PENDING.append(row)
                written += 1
    if _PENDING:
        _executemany_rows(conn, _PENDING.copy())
        _PENDING.clear()
    return written


async def writer_task(queue: asyncio.Queue, bases: List[str], ws_source: str) -> None:
    conn = config.get_db_connection()
    batch: List[Dict[str, Any]] = []
    last_resample = time.monotonic()
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=1.0)
                batch.append(item)
                if len(batch) >= 500:
                    _executemany_rows(conn, batch)
                    batch.clear()
            except asyncio.TimeoutError:
                if batch:
                    _executemany_rows(conn, batch)
                    batch.clear()
                if time.monotonic() - last_resample >= 60:
                    resample_and_persist(conn, bases, datetime.now(timezone.utc), ws_source)
                    last_resample = time.monotonic()
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
                await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    topic = msg.get("topic", "")
                    if topic.startswith("kline."):
                        rec = normalize_bybit_kline(msg)
                        if rec:
                            queue.put_nowait(bar_record_to_row(rec, source, venue, "stream"))
                    elif topic.startswith("markPrice."):
                        rec = normalize_bybit_mark(msg)
                        if rec:
                            queue.put_nowait(mark_record_to_row(rec, source, venue, "stream"))
        except Exception as e:
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
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    data = msg.get("data", msg)
                    etype = data.get("e")
                    if etype == "kline":
                        rec = normalize_binance_kline(data)
                        if rec:
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


async def run_async() -> None:
    config.init_db()
    bases = select_universe()
    if not bases:
        print("[ws_gateway] empty universe; check WS_SYMBOL_SOURCE / static_universe.json")
        return
    print(f"[ws_gateway] universe={len(bases)} symbols; bybit={config.WS_BYBIT_ENABLED} binance={config.WS_BINANCE_ENABLED}; streamed TFs={STREAMED_TFS}")

    if config.WS_BYBIT_ENABLED:
        n = backfill_via_rest("bybit", bases, config.WS_BACKFILL_HOURS)
        print(f"[ws_gateway] bybit backfill wrote {n} bars")
    if config.WS_BINANCE_ENABLED:
        n = backfill_via_rest("binance", bases, config.WS_BACKFILL_HOURS)
        print(f"[ws_gateway] binance backfill wrote {n} bars")

    queue: asyncio.Queue = asyncio.Queue()
    ws_source = config.BYBIT_WS_SOURCE if config.WS_BYBIT_ENABLED else config.BINANCE_WS_SOURCE
    tasks = [writer_task(queue, bases, ws_source)]
    if config.WS_BYBIT_ENABLED:
        tasks.append(_run("bybit", bases, queue, config.BYBIT_WS_SOURCE))
    if config.WS_BINANCE_ENABLED:
        tasks.append(_run("binance", bases, queue, config.BINANCE_WS_SOURCE))
    await asyncio.gather(*tasks)


def main() -> None:
    try:
        asyncio.run(run_async())
    except KeyboardInterrupt:
        print("[ws_gateway] stopped")


if __name__ == "__main__":
    main()
