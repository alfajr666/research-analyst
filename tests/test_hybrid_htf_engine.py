from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import config
from regime_history import init_regime_history_schema
from strategy_v2_context import cutoff_from_id, hybrid_htf_context, load_bars_for_interval


UTC = timezone.utc


def test_config_rejects_unvalidated_enforcement():
    environment = os.environ.copy()
    environment.update({
        "HYBRID_HTF_ENABLED": "true",
        "HYBRID_HTF_MODE": "enforce",
        "HYBRID_HTF_PARITY_VALIDATED": "false",
        "PYTHONPATH": os.pathsep.join(filter(None, [
            str(Path(__file__).resolve().parents[1] / "src" / "research_analyst"),
            environment.get("PYTHONPATH"),
        ])),
    })
    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "HYBRID_HTF_PARITY_VALIDATED" in result.stderr


def test_cutoff_parser_preserves_explicit_iso_datetime():
    cutoff = datetime(2026, 9, 4, 21, 44, tzinfo=UTC)
    assert cutoff_from_id(str(cutoff), datetime(2026, 9, 4, tzinfo=UTC)) == cutoff


def _insert_direct_bars(conn, asset, interval, through, count):
    hours = 1 if interval == "1h" else 4
    table = f"regime_{interval}_bars"
    version = f"bybit-rest-{interval}-v1"
    rows = []
    for index in range(count):
        end = through - timedelta(hours=hours * (count - index - 1))
        close = 100.0 + index
        rows.append((
            f"direct-{asset}-{interval}-{index}", asset, end.isoformat(), "bybit_rest", "bybit",
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


def _insert_5m_tail(conn, asset, start, count):
    rows = []
    for index in range(count):
        end = start + timedelta(minutes=5 * (index + 1))
        close = 1000.0 + index
        rows.append((
            f"tail-{asset}-{start.isoformat()}-{index}", "bybit_ws", "bybit", f"{asset}USDT", asset,
            "perpetual", "5m", end - timedelta(minutes=5), end, end, "stream",
            json.dumps({"open": close - 1.0, "high": close + 1.0,
                        "low": close - 2.0, "close": close, "volume": 2.0}),
        ))
    conn.executemany(
        """INSERT INTO source_observations
           (observation_id, source, venue, native_symbol, asset, market_kind, interval,
            source_start, source_end, retrieved_at, retrieval_kind, payload_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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


def test_engine_stitches_direct_seed_to_canonical_1h_tail(tmp_path, monkeypatch):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    monkeypatch.setattr(config, "HYBRID_HTF_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "HYBRID_HTF_1H_SEED_BARS", 4, raising=False)
    try:
        handoff = datetime(2026, 9, 4, 10, tzinfo=UTC)
        cutoff = datetime(2026, 9, 4, 11, tzinfo=UTC)
        _insert_direct_bars(regime_conn, "ROTATED", "1h", handoff, 4)
        _insert_5m_tail(market_conn, "ROTATED", handoff, 12)

        with hybrid_htf_context(market, regime, cutoff) as context:
            result = load_bars_for_interval(market_conn, "ROTATED", "1h", cutoff)
            details = context.summary()["ROTATED"]["1h"]

        assert result["timestamp"].to_list()[-1] == cutoff
        assert result["close"].to_list()[-1] == 1011.0
        assert result.height == 5
        assert details["source_mode"] == "hybrid"
        assert details["handoff_at"] == handoff.isoformat()
        assert len(details["direct_bar_ids"]) == 4
        assert len(details["canonical_5m_observation_ids"]) == 12
    finally:
        market_conn.close()
        regime_conn.close()


def test_engine_uses_completed_5m_htf_cutoff_for_1m_evaluation(tmp_path, monkeypatch):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    monkeypatch.setattr(config, "HYBRID_HTF_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "HYBRID_HTF_4H_SEED_BARS", 3, raising=False)
    try:
        handoff = datetime(2026, 9, 4, 8, tzinfo=UTC)
        evaluation_cutoff = datetime(2026, 9, 4, 12, 4, tzinfo=UTC)
        htf_cutoff = datetime(2026, 9, 4, 12, tzinfo=UTC)
        _insert_direct_bars(regime_conn, "ROTATED", "4h", handoff, 3)
        _insert_5m_tail(market_conn, "ROTATED", handoff, 48)

        with hybrid_htf_context(
            market, regime, htf_cutoff, evaluation_cutoff=evaluation_cutoff
        ) as context:
            result = load_bars_for_interval(market_conn, "ROTATED", "4h", evaluation_cutoff)
            details = context.summary()["ROTATED"]["4h"]

        assert result["timestamp"].to_list()[-1] == htf_cutoff
        assert details["cutoff_at"] == htf_cutoff.isoformat()
        assert details["evaluation_cutoff_at"] == evaluation_cutoff.isoformat()
    finally:
        market_conn.close()
        regime_conn.close()


def test_engine_reconciles_exact_and_boundary_minus_one_ms_rows(tmp_path, monkeypatch):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    monkeypatch.setattr(config, "HYBRID_HTF_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "HYBRID_HTF_1H_SEED_BARS", 3, raising=False)
    try:
        handoff = datetime(2026, 9, 4, 10, tzinfo=UTC)
        cutoff = datetime(2026, 9, 4, 11, tzinfo=UTC)
        _insert_direct_bars(regime_conn, "ROTATED", "1h", handoff, 3)
        _insert_5m_tail(market_conn, "ROTATED", handoff, 12)
        market_conn.execute(
            """UPDATE source_observations
                  SET source_end = '2026-09-04T10:04:59.999000+00:00'
                WHERE observation_id LIKE 'tail-ROTATED-%-0'"""
        )
        market_conn.execute(
            """INSERT INTO source_observations
               SELECT 'backfill-equivalent', source, venue, native_symbol, asset,
                      market_kind, interval, source_start, '2026-09-04T10:05:00+00:00',
                      retrieved_at, 'backfill', payload_json
                 FROM source_observations
                WHERE observation_id LIKE 'tail-ROTATED-%-0'"""
        )
        market_conn.commit()

        with hybrid_htf_context(market, regime, cutoff) as context:
            result = load_bars_for_interval(market_conn, "ROTATED", "1h", cutoff)
            details = context.summary()["ROTATED"]["1h"]

        assert result.height == 4
        assert details["source_mode"] == "hybrid"
    finally:
        market_conn.close()
        regime_conn.close()


def test_engine_excludes_rows_after_htf_cutoff(tmp_path, monkeypatch):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    monkeypatch.setattr(config, "HYBRID_HTF_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "HYBRID_HTF_4H_SEED_BARS", 3, raising=False)
    try:
        handoff = datetime(2026, 9, 4, 8, tzinfo=UTC)
        cutoff = datetime(2026, 9, 4, 12, tzinfo=UTC)
        _insert_direct_bars(regime_conn, "ROTATED", "4h", handoff, 3)
        _insert_5m_tail(market_conn, "ROTATED", handoff, 48)
        _insert_5m_tail(market_conn, "ROTATED", cutoff, 1)

        with hybrid_htf_context(market, regime, cutoff) as context:
            result = load_bars_for_interval(market_conn, "ROTATED", "4h", cutoff)
            details = context.summary()["ROTATED"]["4h"]

        assert result["timestamp"].to_list()[-1] == cutoff
        assert details["availability"] == "ready"
    finally:
        market_conn.close()
        regime_conn.close()


def test_engine_returns_unavailable_when_live_tail_is_not_contiguous(tmp_path, monkeypatch):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    monkeypatch.setattr(config, "HYBRID_HTF_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "HYBRID_HTF_1H_SEED_BARS", 3, raising=False)
    try:
        handoff = datetime(2026, 9, 4, 10, tzinfo=UTC)
        cutoff = datetime(2026, 9, 4, 11, tzinfo=UTC)
        _insert_direct_bars(regime_conn, "ROTATED", "1h", handoff, 3)
        _insert_5m_tail(market_conn, "ROTATED", handoff, 12)
        market_conn.execute(
            "DELETE FROM source_observations WHERE observation_id LIKE ?",
            ("tail-ROTATED-2026-09-04T10:00:00+00:00-5",),
        )
        market_conn.commit()

        with hybrid_htf_context(market, regime, cutoff) as context:
            result = load_bars_for_interval(market_conn, "ROTATED", "1h", cutoff)
            details = context.summary()["ROTATED"]["1h"]

        assert result.is_empty()
        assert details["availability"] == "unavailable"
        assert details["reason"] == "canonical_tail_gap"
    finally:
        market_conn.close()
        regime_conn.close()


def test_engine_supports_long_4h_seed_required_by_ema200(tmp_path, monkeypatch):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    monkeypatch.setattr(config, "HYBRID_HTF_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "HYBRID_HTF_4H_SEED_BARS", 240, raising=False)
    try:
        handoff = datetime(2026, 9, 4, 8, tzinfo=UTC)
        cutoff = datetime(2026, 9, 4, 12, tzinfo=UTC)
        _insert_direct_bars(regime_conn, "ROTATED", "4h", handoff, 240)
        _insert_5m_tail(market_conn, "ROTATED", handoff, 48)

        with hybrid_htf_context(market, regime, cutoff):
            result = load_bars_for_interval(market_conn, "ROTATED", "4h", cutoff)

        assert result.height == 241
        assert result["timestamp"].to_list()[-1] == cutoff
    finally:
        market_conn.close()
        regime_conn.close()


def test_engine_requires_direct_seed_in_enforce_mode(tmp_path, monkeypatch):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    monkeypatch.setattr(config, "HYBRID_HTF_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "HYBRID_HTF_MODE", "enforce", raising=False)
    monkeypatch.setattr(config, "HYBRID_HTF_1H_SEED_BARS", 3, raising=False)
    try:
        handoff = datetime(2026, 9, 4, 10, tzinfo=UTC)
        cutoff = datetime(2026, 9, 4, 11, tzinfo=UTC)
        _insert_5m_tail(market_conn, "ROTATED", handoff, 12)

        with hybrid_htf_context(market, regime, cutoff) as context:
            result = load_bars_for_interval(market_conn, "ROTATED", "1h", cutoff)
            details = context.summary()["ROTATED"]["1h"]

        assert result.is_empty()
        assert details["reason"] == "direct_seed_missing"
    finally:
        market_conn.close()
        regime_conn.close()


def test_engine_rejects_malformed_direct_seed_in_shadow_mode(tmp_path, monkeypatch):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    monkeypatch.setattr(config, "HYBRID_HTF_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "HYBRID_HTF_MODE", "shadow", raising=False)
    monkeypatch.setattr(config, "HYBRID_HTF_1H_SEED_BARS", 3, raising=False)
    try:
        handoff = datetime(2026, 9, 4, 10, tzinfo=UTC)
        cutoff = datetime(2026, 9, 4, 11, tzinfo=UTC)
        _insert_direct_bars(regime_conn, "ROTATED", "1h", handoff, 3)
        regime_conn.execute(
            "UPDATE regime_1h_bars SET high=0 WHERE bar_id=?",
            ("direct-ROTATED-1h-0",),
        )
        regime_conn.commit()
        _insert_5m_tail(market_conn, "ROTATED", handoff, 12)

        with hybrid_htf_context(market, regime, cutoff) as context:
            result = load_bars_for_interval(market_conn, "ROTATED", "1h", cutoff)
            details = context.summary()["ROTATED"]["1h"]

        assert result.is_empty()
        assert details["reason"] == "direct_seed_incomplete"
        assert details["hybrid_readiness"] == "not_ready"
    finally:
        market_conn.close()
        regime_conn.close()


def test_engine_allows_explicit_canonical_only_in_shadow_mode(tmp_path, monkeypatch):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    monkeypatch.setattr(config, "HYBRID_HTF_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "HYBRID_HTF_MODE", "shadow", raising=False)
    monkeypatch.setattr(config, "HYBRID_HTF_1H_SEED_BARS", 3, raising=False)
    try:
        handoff = datetime(2026, 9, 4, 10, tzinfo=UTC)
        cutoff = datetime(2026, 9, 4, 11, tzinfo=UTC)
        _insert_5m_tail(market_conn, "ROTATED", handoff, 12)

        with hybrid_htf_context(market, regime, cutoff) as context:
            result = load_bars_for_interval(market_conn, "ROTATED", "1h", cutoff)
            details = context.summary()["ROTATED"]["1h"]

        assert result.height == 1
        assert details["source_mode"] == "canonical_only"
        assert details["hybrid_readiness"] == "not_ready"
    finally:
        market_conn.close()
        regime_conn.close()


def test_engine_rejects_duplicate_canonical_source_rows(tmp_path, monkeypatch):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    monkeypatch.setattr(config, "HYBRID_HTF_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "HYBRID_HTF_1H_SEED_BARS", 3, raising=False)
    try:
        handoff = datetime(2026, 9, 4, 10, tzinfo=UTC)
        cutoff = datetime(2026, 9, 4, 11, tzinfo=UTC)
        _insert_direct_bars(regime_conn, "ROTATED", "1h", handoff, 3)
        _insert_5m_tail(market_conn, "ROTATED", handoff, 12)
        market_conn.execute(
            "INSERT INTO source_observations SELECT 'duplicate', source, venue, native_symbol, asset, market_kind, interval, source_start, source_end, retrieved_at, retrieval_kind, payload_json FROM source_observations WHERE observation_id LIKE ?",
            ("tail-ROTATED-%-0",),
        )
        market_conn.execute(
            "UPDATE source_observations SET source='failover_ws' WHERE observation_id='duplicate'"
        )
        market_conn.commit()

        with hybrid_htf_context(market, regime, cutoff) as context:
            result = load_bars_for_interval(market_conn, "ROTATED", "1h", cutoff)
            details = context.summary()["ROTATED"]["1h"]

        assert result.is_empty()
        assert details["reason"] == "canonical_tail_duplicate"
    finally:
        market_conn.close()
        regime_conn.close()


def test_engine_rejects_malformed_canonical_row_even_with_valid_duplicate(tmp_path, monkeypatch):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    monkeypatch.setattr(config, "HYBRID_HTF_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "HYBRID_HTF_1H_SEED_BARS", 3, raising=False)
    try:
        handoff = datetime(2026, 9, 4, 10, tzinfo=UTC)
        cutoff = datetime(2026, 9, 4, 11, tzinfo=UTC)
        _insert_direct_bars(regime_conn, "ROTATED", "1h", handoff, 3)
        _insert_5m_tail(market_conn, "ROTATED", handoff, 12)
        market_conn.execute(
            "INSERT INTO source_observations SELECT 'malformed', source, venue, native_symbol, asset, market_kind, interval, source_start, source_end, retrieved_at, retrieval_kind, payload_json FROM source_observations WHERE observation_id LIKE ?",
            ("tail-ROTATED-%-0",),
        )
        market_conn.execute(
            "UPDATE source_observations SET source='malformed_ws', payload_json=? WHERE observation_id='malformed'",
            (json.dumps({"open": 0, "high": 0, "low": 0, "close": 0, "volume": 1}),),
        )
        market_conn.commit()

        with hybrid_htf_context(market, regime, cutoff) as context:
            result = load_bars_for_interval(market_conn, "ROTATED", "1h", cutoff)
            details = context.summary()["ROTATED"]["1h"]

        assert result.is_empty()
        assert details["reason"] == "canonical_tail_invalid"
    finally:
        market_conn.close()
        regime_conn.close()


def test_loader_remains_canonical_outside_engine_context(tmp_path):
    market = tmp_path / "market.sqlite3"
    config.init_market_db(market)
    conn = config.get_db_connection(db_path=market)
    try:
        handoff = datetime(2026, 9, 4, 10, tzinfo=UTC)
        cutoff = datetime(2026, 9, 4, 11, tzinfo=UTC)
        _insert_5m_tail(conn, "ROTATED", handoff, 12)
        result = load_bars_for_interval(conn, "ROTATED", "1h", cutoff)
        assert result.height == 1
        assert result["source"].to_list() == ["bybit_ws"]
    finally:
        conn.close()


def test_engine_context_rejects_cutoff_mismatch(tmp_path, monkeypatch):
    market, regime, market_conn, regime_conn = _connections(tmp_path)
    monkeypatch.setattr(config, "HYBRID_HTF_ENABLED", True, raising=False)
    try:
        cutoff = datetime(2026, 9, 4, 11, tzinfo=UTC)
        with hybrid_htf_context(market, regime, cutoff):
            try:
                load_bars_for_interval(market_conn, "ROTATED", "1h", cutoff + timedelta(hours=1))
            except ValueError as exc:
                assert "does not match requested cutoff" in str(exc)
            else:
                raise AssertionError("expected hybrid context cutoff mismatch")
    finally:
        market_conn.close()
        regime_conn.close()


def test_plugin_engine_receives_hybrid_frame_without_plugin_source_knowledge(tmp_path, monkeypatch):
    import strategy_plugins

    market, regime, market_conn, regime_conn = _connections(tmp_path)
    analyst = tmp_path / "analyst.sqlite3"
    config.init_analyst_db(analyst)
    old_registry = strategy_plugins._REGISTRY.copy()
    monkeypatch.setattr(config, "HYBRID_HTF_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "HYBRID_HTF_1H_SEED_BARS", 3, raising=False)
    monkeypatch.setattr(config, "MARKET_DB_PATH", str(market), raising=False)
    monkeypatch.setattr(config, "REGIME_DB_PATH", str(regime), raising=False)
    monkeypatch.setattr(config, "SYMBOL_ROTATION_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "STATIC_SYMBOLS_OVERRIDE", "ROTATED", raising=False)
    monkeypatch.setattr(config, "STRATEGY_ENABLED_IDS", ("failed-break-v3",), raising=False)
    monkeypatch.setattr(config, "STRATEGY_ACTIVE_IDS", ("failed-break-v3",), raising=False)
    monkeypatch.setattr(config, "STRUCTURAL_STOP_ADMISSION_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "DEEP_WARMUP_GATE_ENABLED", False, raising=False)
    observed = []

    def run_plugin(_cutoff_id, _snapshot):
        from strategy_v2_context import load_bars_for_interval
        bars = load_bars_for_interval(market_conn, "ROTATED", "1h", cutoff)
        observed.append((bars.height, bars["source"].to_list()[-1]))
        return []

    strategy_plugins._REGISTRY["failed-break-v3"] = strategy_plugins.StrategyPlugin(
        "failed-break-v3", "test", (), (), run_plugin, cadence="5m", market_family="reversal"
    )
    cutoff = datetime(2026, 9, 4, 11, tzinfo=UTC)
    handoff = datetime(2026, 9, 4, 10, tzinfo=UTC)
    _insert_direct_bars(regime_conn, "ROTATED", "1h", handoff, 3)
    _insert_5m_tail(market_conn, "ROTATED", handoff, 12)
    try:
        result = strategy_plugins._run_plugins_for_cutoff(
            analyst, "5m:2026-09-04T11:00:00Z", cutoff, False,
            snapshot={"eval_interval": "5m", "feature_snapshots": {},
                      "market_db_path": str(market), "now": cutoff},
            market_db_path=market,
        )
        assert result["failed-break-v3"] == {"emitted": 0, "events": []}
        assert observed == [(4, "bybit_ws")]
    finally:
        strategy_plugins._REGISTRY.clear()
        strategy_plugins._REGISTRY.update(old_registry)
        market_conn.close()
        regime_conn.close()
