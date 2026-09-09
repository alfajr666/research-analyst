from datetime import datetime, timedelta, timezone

import config
from db_maintenance import prune_analyst_db, prune_market_db, prune_regime_db
from regime_history import init_regime_history_schema
from regime_session import init_regime_db


def test_market_retention_uses_interval_tiers(tmp_path, monkeypatch):
    db = tmp_path / "market.sqlite3"
    config.init_market_db(db)
    monkeypatch.setattr(config, "PRUNE_INTERVAL_DAYS", {"1m": 2, "5m": 5})
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    conn = config.get_db_connection(db_path=db)
    try:
        for observation_id, interval, age in (
            ("old-1m", "1m", 3),
            ("keep-1m", "1m", 1),
            ("old-5m", "5m", 6),
            ("keep-5m", "5m", 1),
        ):
            timestamp = now - timedelta(days=age)
            conn.execute(
                """INSERT INTO source_observations
                   (observation_id, source, venue, native_symbol, asset, market_kind,
                    interval, source_start, source_end, retrieved_at, retrieval_kind, payload_json)
                   VALUES (?, 'bybit_ws', 'bybit', 'BTCUSDT', 'BTC', 'usdt_perp', ?, ?, ?, ?, 'test', '{}')""",
                (observation_id, interval, timestamp, timestamp, timestamp),
            )
        conn.commit()
        result = prune_market_db(conn, now)
        assert result["source_observations"] == 2
        assert conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 2
    finally:
        conn.close()


def test_analyst_retention_removes_snapshots_but_keeps_active_events(tmp_path, monkeypatch):
    db = tmp_path / "analyst.sqlite3"
    config.init_analyst_db(db)
    monkeypatch.setattr(config, "ANALYST_SNAPSHOT_RETENTION_DAYS", 2)
    monkeypatch.setattr(config, "ANALYST_EVENT_RETENTION_DAYS", 2)
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    old = now - timedelta(days=3)
    conn = config.get_db_connection(db_path=db)
    try:
        conn.execute(
            """INSERT INTO cutoff_runs
               (cutoff_id, cutoff_at, status, started_at, finalized_at, source_observation_ids, error)
               VALUES ('old-cutoff', ?, 'finalized', ?, ?, '[]', NULL)""",
            (old, old, old),
        )
        conn.execute(
            """INSERT INTO structure_zones
               (zone_id, cutoff_id, asset, kind, direction, strength, low, high, state,
                source_evidence_ids, confidence_status, created_at)
               VALUES ('old-zone', 'old-cutoff', 'BTC', 'fvg_4h', 'bullish', 1, 1, 2,
                       'active', '[\"obs\"]', 'uncalibrated', ?)""",
            (old,),
        )
        conn.execute(
            """INSERT INTO structure_zones
               (zone_id, cutoff_id, asset, kind, direction, strength, low, high, state,
                source_evidence_ids, confidence_status, created_at)
               VALUES ('recent-zone', 'old-cutoff', 'BTC', 'fvg_4h', 'bullish', 1, 1, 2,
                       'active', '[\"obs\"]', 'uncalibrated', ?)""",
            (now,),
        )
        conn.execute(
            """INSERT INTO feature_snapshots
               (snapshot_id, cutoff_id, asset, feature_set, version, computed_at, payload_json)
               VALUES ('old-feature', 'old-cutoff', 'BTC', 'zones', 'v1', ?, '{}')""",
            (old,),
        )
        conn.execute(
            """INSERT INTO alpha_events
               (dedupe_key, alpha_id, strategy_id, asset, direction, setup_class, phase,
                status, observed_at, valid_until, event_json, persisted_at)
               VALUES ('active-key', 'active-alpha', 'test', 'BTC', 'long', 'test', 'test',
                       'active', ?, ?, '{}', ?)""",
            (old, old + timedelta(days=1), old),
        )
        conn.execute(
            """INSERT INTO alpha_events
               (dedupe_key, alpha_id, strategy_id, asset, direction, setup_class, phase,
                status, observed_at, valid_until, event_json, persisted_at)
               VALUES ('expired-key', 'expired-alpha', 'test', 'BTC', 'long', 'test', 'test',
                       'expired', ?, ?, '{}', ?)""",
            (old, old + timedelta(days=1), old),
        )
        conn.commit()
        result = prune_analyst_db(conn, now)
        assert result["structure_zones"] == 2
        assert conn.execute("SELECT COUNT(*) FROM structure_zones").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM feature_snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM alpha_events WHERE status='active'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM alpha_events WHERE status='expired'").fetchone()[0] == 0
    finally:
        conn.close()


def test_analyst_retention_prunes_evaluation_coverage(tmp_path, monkeypatch):
    db = tmp_path / "analyst.sqlite3"
    config.init_analyst_db(db)
    monkeypatch.setattr(config, "ANALYST_COVERAGE_RETENTION_DAYS", 2)
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    old = (now - timedelta(days=3)).isoformat().replace("+00:00", "Z")
    recent = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    conn = config.get_db_connection(db_path=db)
    try:
        conn.executemany(
            "INSERT INTO raw_signal_evaluation_coverage "
            "(strategy_id, asset, evaluated_at, emitted_count) VALUES (?, ?, ?, ?)",
            [("strategy", "OLD", old, 0), ("strategy", "RECENT", recent, 0)],
        )
        conn.commit()
        result = prune_analyst_db(conn, now)
        assert result["raw_signal_evaluation_coverage"] == 1
        assert conn.execute(
            "SELECT asset FROM raw_signal_evaluation_coverage"
        ).fetchone()[0] == "RECENT"
    finally:
        conn.close()


def test_online_prune_can_limit_each_table_to_one_batch(tmp_path, monkeypatch):
    db = tmp_path / "analyst.sqlite3"
    config.init_analyst_db(db)
    monkeypatch.setattr(config, "DB_MAINTENANCE_BATCH_SIZE", 2)
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    old = now - timedelta(days=31)
    conn = config.get_db_connection(db_path=db)
    try:
        for index in range(105):
            conn.execute(
                "INSERT INTO pipeline_runs (run_id, started_at, status, details_json) VALUES (?, ?, 'completed', '{}')",
                (f"run-{index}", old, ),
            )
        conn.commit()
        result = prune_analyst_db(conn, now, max_batches=1)
        assert result["pipeline_runs"] == 100
        assert conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 5
    finally:
        conn.close()


def test_regime_retention_removes_old_scores_and_gates(tmp_path, monkeypatch):
    db = tmp_path / "regime.sqlite3"
    init_regime_db(db)
    monkeypatch.setattr(config, "REGIME_SCORE_RETENTION_DAYS", 2)
    monkeypatch.setattr(config, "REGIME_GATE_RETENTION_DAYS", 2)
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    old = (now - timedelta(days=3)).isoformat()
    recent = (now - timedelta(days=1)).isoformat()
    conn = config.get_db_connection(db_path=db)
    try:
        for cutoff, suffix in ((old, "old"), (recent, "recent")):
            conn.execute(
                """INSERT INTO regime_scores
                   (observation_id, asset, cutoff_at, rotation_feed_id, score_version,
                    status, inputs_json, components_json, source_observation_ids,
                    source_references_json, recorded_at)
                   VALUES (?, 'BTC', ?, 'feed', 'score', 'ready', '{}', '{}', '[]', '{}', ?)""",
                (f"score-{suffix}", cutoff, cutoff),
            )
            conn.execute(
                """INSERT INTO regime_gate_decisions
                   (decision_id, asset, cutoff_at, rotation_feed_id, gate_version,
                    decision, session_name, session_phase, reasons_json,
                    family_activation_json, recorded_at)
                   VALUES (?, 'BTC', ?, 'feed', 'gate', 'allow', 'session', 'phase', '[]', '{}', ?)""",
                (f"gate-{suffix}", cutoff, cutoff),
            )
        conn.commit()
        result = prune_regime_db(conn, now)
        assert result["regime_scores"] == 1
        assert result["regime_gate_decisions"] == 1
        assert conn.execute("SELECT COUNT(*) FROM regime_scores").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM regime_gate_decisions").fetchone()[0] == 1
    finally:
        conn.close()


def test_regime_retention_uses_direct_htf_windows(tmp_path, monkeypatch):
    db = tmp_path / "regime.sqlite3"
    init_regime_db(db)
    conn = config.get_db_connection(db_path=db)
    init_regime_history_schema(conn)
    monkeypatch.setattr(config, "DIRECT_HTF_1H_RETAIN_DAYS", 14)
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    old = (now - timedelta(days=15)).isoformat()
    recent = (now - timedelta(days=1)).isoformat()
    try:
        for bar_id, source_end in (("old-direct", old), ("recent-direct", recent)):
            conn.execute(
                """INSERT INTO regime_1h_bars
                   (bar_id, asset, bar_end, source, venue, open, high, low, close,
                    volume, source_start, source_end, request_id, retrieved_at, bar_version)
                   VALUES (?, 'BTC', ?, 'bybit_rest', 'bybit', 99, 101, 98, 100,
                           1, ?, ?, NULL, ?, 'bybit-rest-1h-v1')""",
                (bar_id, source_end, source_end, source_end, source_end),
            )
        conn.commit()
        result = prune_regime_db(conn, now)
        assert result["regime_1h_bars"] == 1
        assert conn.execute("SELECT COUNT(*) FROM regime_1h_bars").fetchone()[0] == 1
    finally:
        conn.close()


def test_analyst_retention_keeps_claimed_batches_and_running_pipelines(tmp_path):
    db = tmp_path / "analyst.sqlite3"
    config.init_analyst_db(db)
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    old = now - timedelta(days=31)
    conn = config.get_db_connection(db_path=db)
    try:
        conn.execute(
            "INSERT INTO pipeline_runs (run_id, started_at, status, details_json) VALUES ('running', ?, 'running', '{}')",
            (old,),
        )
        conn.execute(
            "INSERT INTO pipeline_runs (run_id, started_at, status, details_json) VALUES ('done', ?, 'completed', '{}')",
            (old,),
        )
        conn.execute(
            "INSERT INTO cutoff_runs (cutoff_id, cutoff_at, status, started_at, source_observation_ids) "
            "VALUES ('running-cutoff', ?, 'running', ?, '[]')",
            (old, old),
        )
        conn.execute(
            """INSERT INTO feature_snapshots
               (snapshot_id, cutoff_id, asset, feature_set, version, computed_at, payload_json)
               VALUES ('running-feature', 'running-cutoff', 'BTC', 'test', 'v1', ?, '{}')""",
            (old,),
        )
        conn.execute(
            "INSERT INTO discord_signal_batches (window_start, window_end, status, candidate_count, message_count) "
            "VALUES ('2026-08-01T00:00:00Z', '2026-08-01T00:30:00Z', 'claimed', 1, 0)"
        )
        conn.execute(
            "INSERT INTO discord_signal_batch_members (window_start, raw_signal_id) VALUES ('2026-08-01T00:00:00Z', 'raw-1')"
        )
        conn.commit()
        prune_analyst_db(conn, now)
        assert conn.execute("SELECT COUNT(*) FROM pipeline_runs WHERE run_id='running'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM pipeline_runs WHERE run_id='done'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cutoff_runs WHERE cutoff_id='running-cutoff'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM feature_snapshots WHERE snapshot_id='running-feature'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM discord_signal_batches").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM discord_signal_batch_members").fetchone()[0] == 1
    finally:
        conn.close()
