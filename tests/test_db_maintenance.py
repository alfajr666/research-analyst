from datetime import datetime, timedelta, timezone

import config
from db_maintenance import prune_analyst_db, prune_market_db


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
        assert result["structure_zones"] == 1
        assert conn.execute("SELECT COUNT(*) FROM structure_zones").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM feature_snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM alpha_events WHERE status='active'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM alpha_events WHERE status='expired'").fetchone()[0] == 0
    finally:
        conn.close()
