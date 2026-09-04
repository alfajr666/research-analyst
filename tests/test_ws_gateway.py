"""Tests for ws_gateway (pure normalization, planning, resample, emit-gate)."""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from pathlib import Path

import config
import ws_gateway as wsg


def test_plan_bybit_streams_shards():
    syms = [f"BASE{i}" for i in range(45)]  # 45 bases -> 2 kline topics each
    shards = wsg.plan_bybit_streams(syms, shard=20)
    total = sum(len(s) for s in shards)
    assert total == 90, total  # 45 * (kline.1 + kline.5)
    # Soft cap: a symbol can overshoot the 20 cap by up to (topics per symbol - 1).
    assert max(len(s) for s in shards) <= 20 + len(wsg.STREAMED_TFS), max(len(s) for s in shards)
    assert shards[0][0] == "kline.1.BASE0USDT"
    assert shards[0][1] == "kline.5.BASE0USDT"


def test_gateway_static_mode_is_independent_from_compact_evaluation_universe(monkeypatch):
    monkeypatch.setattr(config, "WS_SYMBOL_SOURCE", "static")
    monkeypatch.setattr(config, "load_static_symbols", lambda: ["BTC", "ETH", "PAXG", "QQQ", "SOL"])
    monkeypatch.setattr(config, "SYMBOL_ROTATION_ENABLED", False)
    monkeypatch.setattr(config, "EXECUTOR_SNAPSHOT_DIR", "")
    assert wsg.select_universe() == ["BTC", "ETH", "PAXG", "QQQ", "SOL"]
    assert config.COMPACT_STRATEGY_ASSETS == frozenset({"BTC", "ETH", "PAXG", "QQQ"})


def test_gateway_keeps_fresh_open_position_after_rotation_drop(monkeypatch, tmp_path):
    import json
    from datetime import datetime, timezone

    snapshot = tmp_path / "snapshots" / "bybit" / "fundamo"
    snapshot.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    (snapshot / "latest.json").write_text(json.dumps({
        "timestamp": now.isoformat(),
        "positions": [{
            "status": "OPEN",
            "symbol": "SOL/USDT:USDT",
            "original_json": "{}",
        }],
    }))
    monkeypatch.setattr(config, "EXECUTOR_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setattr(
        "symbol_rotation.subscription_assets",
        lambda at=None: (["BTC", "ETH"], {"feed_id": "rotated", "status": "ready"}),
    )

    assert wsg.select_universe() == ["BTC", "ETH", "SOL"]


def test_plan_binance_streams_single_conn():
    streams = wsg.plan_binance_streams(["BTC", "ETH"])
    assert "btcusdt@kline_1m" in streams
    assert "btcusdt@kline_5m" in streams
    assert "btcusdt@markPrice@1s" in streams
    assert len(streams) == 6  # 2 symbols * (1m + 5m + mark)


def test_normalize_bybit_kline():
    msg = {
        "topic": "kline.1.BTCUSDT", "ts": 1700000000000, "type": "snapshot",
        "data": [{"start": 1700000000000, "end": 1700000060000, "open": "100", "high": "110",
                  "low": "95", "close": "105", "volume": "12.5", "turnover": "1300", "confirm": 1}],
    }
    rec = wsg.normalize_bybit_kline(msg)
    assert rec == {
        "native_symbol": "BTCUSDT", "asset": "BTC", "interval": "1m",
        "open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 12.5,
        "source_start_ms": 1700000000000, "source_end_ms": 1700000060000, "confirm": 1,
    }


def test_normalize_bybit_kline_5m():
    msg = {
        "topic": "kline.5.BTCUSDT", "ts": 1700000000000,
        "data": [{"start": 1700000000000, "end": 1700000300000, "open": "100", "high": "110",
                  "low": "95", "close": "105", "volume": "12.5", "turnover": "1300", "confirm": 1}],
    }
    rec = wsg.normalize_bybit_kline(msg)
    assert rec["interval"] == "5m"
    assert rec["source_end_ms"] == 1700000300000


def test_normalize_binance_kline():
    msg = {"e": "kline", "E": 1, "s": "BTCUSDT", "k": {
        "t": 1700000000000, "T": 1700000060000, "s": "BTCUSDT", "i": "1m",
        "o": "100", "h": "110", "l": "95", "c": "105", "v": "12.5", "x": True}}
    rec = wsg.normalize_binance_kline(msg)
    assert rec["close"] == 105.0 and rec["confirm"] == 1 and rec["asset"] == "BTC"


def test_normalize_marks():
    bmsg = {"topic": "markPrice.BTCUSDT", "ts": 1, "data": {
        "symbol": "BTCUSDT", "markPrice": "101.5", "fundingRate": "0.0001", "time": 1700000000000}}
    r = wsg.normalize_bybit_mark(bmsg)
    assert r["mark_price"] == 101.5 and r["funding_rate"] == 0.0001

    bmsg2 = {"e": "markPriceUpdate", "E": 1700000000000, "s": "BTCUSDT", "p": "101.5", "r": "0.0001"}
    r2 = wsg.normalize_binance_mark(bmsg2)
    assert r2["mark_price"] == 101.5


def test_make_observation_id_deterministic():
    end = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    a = wsg.make_observation_id("bybit_ws", "bybit", "BTCUSDT", "1m", end)
    b = wsg.make_observation_id("bybit_ws", "bybit", "BTCUSDT", "1m", end)
    assert a == b
    c = wsg.make_observation_id("bybit_ws", "bybit", "BTCUSDT", "5m", end)
    assert a != c


def test_publish_base_triggers_excludes_future_bars(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    published = []
    monkeypatch.setattr(
        wsg,
        "publish_evaluation_trigger",
        lambda cutoff, interval="5m": published.append((cutoff, interval)),
    )

    wsg._publish_base_triggers([
        {"interval": "5m", "source_end": now - dt.timedelta(seconds=1)},
        {"interval": "5m", "source_end": now + dt.timedelta(minutes=5)},
    ])

    assert len(published) == 1
    assert published[0][1] == "5m"


def test_bar_record_to_row_shape():
    rec = {"native_symbol": "BTCUSDT", "asset": "BTC", "interval": "1m", "open": 1, "high": 2,
           "low": 0.5, "close": 1.5, "volume": 10, "source_start_ms": 1700000000000,
           "source_end_ms": 1700000060000, "confirm": 1}
    row = wsg.bar_record_to_row(rec, "bybit_ws", "bybit", "stream")
    assert row["source"] == "bybit_ws"
    assert row["interval"] == "1m"
    p = __import__("json").loads(row["payload_json"])
    assert p["open"] == 1.0 and p["close"] == 1.5 and p["open_interest"] is None
    assert row["observation_id"].startswith("b") or len(row["observation_id"]) == 64


def test_resample_and_persist_writes_derived(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    monkeypatch.setattr(config, "MARKET_DB_PATH", str(db))
    config.init_db(str(db))
    conn = config.get_db_connection(db_path=str(db))
    try:
        # Insert 5m bars for BTC across ~6 hours (every 5m) -> derives 15m/1h/4h.
        now = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
        rows = []
        t = now - dt.timedelta(minutes=360)
        for i in range(72):
            end = t + dt.timedelta(minutes=5 * i)
            pid = wsg.make_observation_id("bybit_ws", "bybit", "BTCUSDT", "5m", end)
            payload = __import__("json").dumps({
                "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i,
                "volume": 1.0, "open_interest": None, "funding_rate": None})
            rows.append((pid, "bybit_ws", "bybit", "BTCUSDT", "BTC", "usdt_perp", "5m",
                         end, end, now, "backfill", payload))
        conn.executemany(
            "INSERT OR IGNORE INTO source_observations "
            "(observation_id,source,venue,native_symbol,asset,market_kind,interval,"
            "source_start,source_end,retrieved_at,retrieval_kind,payload_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()

        written = wsg.resample_and_persist(conn, ["BTC"], now, "bybit_ws")
        assert written > 0, written
        # 5m is streamed (not derived); 15m/1h/4h are derived from 5m.
        cnt5 = conn.execute(
            "SELECT count(*) FROM source_observations WHERE asset='BTC' AND interval='5m' AND retrieval_kind='backfill'",
        ).fetchone()[0]
        assert cnt5 > 0, cnt5
        for tf in ("15m", "1h", "4h"):
            cnt = conn.execute(
                "SELECT count(*) FROM source_observations WHERE asset='BTC' AND interval=? AND source='bybit_ws'",
                (tf,)).fetchone()[0]
            assert cnt > 0, (tf, cnt)
    finally:
        conn.close()


def test_emit_gate_accepts_pure_ws(tmp_path, monkeypatch):
    # Pure WS must pass the alpha_outbox emit gate (regression for the gate change).
    import alpha_outbox
    monkeypatch.setattr(alpha_outbox, "OUTBOX_DIR", tmp_path)
    ev = {"strategy_id": "rsi-reclaim-v1", "asset": "BTC", "direction": "long",
          "observed_at": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
          "data_purity": "pure_ws"}
    # rsi-reclaim-v1 is a known PRICE strategy; purity gate must NOT short-circuit.
    created, dest = alpha_outbox.write_event(ev, outbox_dir=tmp_path)
    assert "blocked" not in str(dest), f"pure_ws was wrongly blocked: {dest}"
    assert created is True
