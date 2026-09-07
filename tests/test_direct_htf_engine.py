from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys

import config
from regime_history import init_regime_history_schema
from strategy_v2_context import (
    cutoff_from_id,
    direct_htf_context,
    load_bars_for_interval,
)


UTC = timezone.utc


def _insert_direct_bars(conn, asset, interval, through, count):
    hours = 1 if interval == "1h" else 4
    table = f"regime_{interval}_bars"
    version = f"bybit-rest-{interval}-v1"
    rows = []
    for index in range(count):
        end = through - timedelta(hours=hours * (count - index - 1))
        close = 100.0 + index
        rows.append((
            f"direct-{asset}-{interval}-{end.isoformat()}-{index}", asset, end.isoformat(), "bybit_rest", "bybit",
            close - 1.0, close + 1.0, close - 2.0, close, 10.0,
            (end - timedelta(hours=hours)).isoformat(), end.isoformat(), None,
            end.isoformat(), version,
        ))
    conn.executemany(
        f"""INSERT INTO {table}
            (bar_id, asset, bar_end, source, venue, open, high, low, close, volume,
             source_start, source_end, request_id, retrieved_at, bar_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def _connections(tmp_path):
    market = tmp_path / "market.sqlite3"
    regime = tmp_path / "regime.sqlite3"
    config.init_market_db(market)
    market_conn = config.get_db_connection(db_path=market)
    regime_conn = config.get_db_connection(db_path=regime)
    init_regime_history_schema(regime_conn)
    return market, regime, market_conn, regime_conn


def test_config_has_no_hybrid_runtime_switches():
    environment = os.environ.copy()
    environment.pop("HYBRID_HTF_ENABLED", None)
    environment.pop("HYBRID_HTF_MODE", None)
    environment.pop("HYBRID_HTF_PARITY_VALIDATED", None)
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(Path(__file__).resolve().parents[1] / "src" / "research_analyst"),
        environment.get("PYTHONPATH"),
    ]))
    result = subprocess.run(
        [sys.executable, "-c", "import config; assert not hasattr(config, 'HYBRID_HTF_MODE')"],
        env=environment, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_cutoff_parser_preserves_explicit_iso_datetime():
    cutoff = datetime(2026, 9, 4, 21, 44, tzinfo=UTC)
    assert cutoff_from_id(str(cutoff), datetime(2026, 9, 4, tzinfo=UTC)) == cutoff


def test_direct_loader_uses_native_bars_only(tmp_path, monkeypatch):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    monkeypatch.setattr(config, "DIRECT_HTF_1H_SEED_BARS", 4, raising=False)
    try:
        cutoff = datetime(2026, 9, 4, 11, tzinfo=UTC)
        _insert_direct_bars(regime_conn, "ROTATED", "1h", cutoff, 4)

        with direct_htf_context(regime, cutoff) as context:
            result = load_bars_for_interval(market_conn, "ROTATED", "1h", cutoff)
            details = context.summary()["ROTATED"]["1h"]

        assert result.height == 4
        assert result["timestamp"].to_list()[-1] == cutoff
        assert result["close"].to_list()[-1] == 103.0
        assert details["source_mode"] == "direct_rest"
        assert details["data_contract_version"] == "direct-htf-v1"
        assert len(details["direct_bar_ids"]) == 4
    finally:
        market_conn.close()
        regime_conn.close()


def test_direct_loader_excludes_future_native_bars(tmp_path, monkeypatch):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    monkeypatch.setattr(config, "DIRECT_HTF_4H_SEED_BARS", 3, raising=False)
    try:
        cutoff = datetime(2026, 9, 4, 12, tzinfo=UTC)
        _insert_direct_bars(regime_conn, "ROTATED", "4h", cutoff, 3)
        _insert_direct_bars(regime_conn, "ROTATED", "4h", cutoff + timedelta(hours=4), 1)

        with direct_htf_context(regime, cutoff):
            result = load_bars_for_interval(market_conn, "ROTATED", "4h", cutoff)

        assert result.height == 3
        assert result["timestamp"].to_list()[-1] == cutoff
    finally:
        market_conn.close()
        regime_conn.close()


def test_direct_loader_fails_closed_without_regime_history(tmp_path, monkeypatch):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    monkeypatch.setattr(config, "DIRECT_HTF_1H_SEED_BARS", 3, raising=False)
    try:
        cutoff = datetime(2026, 9, 4, 11, tzinfo=UTC)
        with direct_htf_context(regime, cutoff) as context:
            result = load_bars_for_interval(market_conn, "ROTATED", "1h", cutoff)
            details = context.summary()["ROTATED"]["1h"]

        assert result.is_empty()
        assert details["reason"] == "direct_history_missing"
    finally:
        market_conn.close()
        regime_conn.close()


def test_direct_loader_rejects_gap_and_invalid_native_rows(tmp_path, monkeypatch):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    monkeypatch.setattr(config, "DIRECT_HTF_1H_SEED_BARS", 3, raising=False)
    try:
        cutoff = datetime(2026, 9, 4, 11, tzinfo=UTC)
        _insert_direct_bars(regime_conn, "ROTATED", "1h", cutoff, 3)
        regime_conn.execute(
            "DELETE FROM regime_1h_bars WHERE bar_id = ?",
            ("direct-ROTATED-1h-2026-09-04T10:00:00+00:00-1",),
        )
        regime_conn.execute(
            "UPDATE regime_1h_bars SET high = 0 WHERE bar_id = ?",
            ("direct-ROTATED-1h-2026-09-04T09:00:00+00:00-0",),
        )
        regime_conn.commit()

        with direct_htf_context(regime, cutoff) as context:
            result = load_bars_for_interval(market_conn, "ROTATED", "1h", cutoff)
            details = context.summary()["ROTATED"]["1h"]

        assert result.is_empty()
        assert details["reason"] == "direct_history_incomplete"
    finally:
        market_conn.close()
        regime_conn.close()


def test_direct_context_rejects_cutoff_mismatch(tmp_path):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    try:
        cutoff = datetime(2026, 9, 4, 11, tzinfo=UTC)
        with direct_htf_context(regime, cutoff):
            try:
                load_bars_for_interval(market_conn, "ROTATED", "1h", cutoff + timedelta(hours=1))
            except ValueError as exc:
                assert "direct HTF context evaluation cutoff" in str(exc)
            else:
                raise AssertionError("expected direct context cutoff mismatch")
    finally:
        market_conn.close()
        regime_conn.close()


def test_htf_load_without_direct_context_fails_closed(tmp_path):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    try:
        cutoff = datetime(2026, 9, 4, 11, tzinfo=UTC)
        assert load_bars_for_interval(market_conn, "ROTATED", "1h", cutoff).is_empty()
        assert load_bars_for_interval(market_conn, "ROTATED", "4h", cutoff).is_empty()
    finally:
        market_conn.close()
        regime_conn.close()
