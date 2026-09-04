import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import config
from warmup import ensure_backfill_jobs, ready_assets, refresh_backfill_jobs, required_5m_bars


def _seed_5m_history(conn, asset: str, cutoff: datetime, count: int) -> None:
    rows = []
    for index in range(count):
        end = cutoff - timedelta(minutes=5 * (count - index - 1))
        rows.append((
            f"{asset}-{index}", "bybit_ws", "bybit", f"{asset}USDT", asset,
            "perpetual", "5m", end - timedelta(minutes=5), end, end,
            "test", json.dumps({"open": 100, "high": 101, "low": 99, "close": 100}),
        ))
    conn.executemany(
        """INSERT INTO source_observations
           (observation_id, source, venue, native_symbol, asset, market_kind,
            interval, source_start, source_end, retrieved_at, retrieval_kind, payload_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def test_cold_rotation_asset_is_not_ready_and_job_is_pending():
    with TemporaryDirectory() as directory:
        db_path = Path(directory) / "market.db"
        config.init_market_db(db_path)
        conn = config.get_db_connection(db_path=db_path)
        try:
            now = datetime(2026, 9, 2, 12, 5, tzinfo=timezone.utc)
            assert ensure_backfill_jobs(conn, ["NEWCOIN"], now=now) == 1
            ready, details = ready_assets(conn, ["NEWCOIN"], now)
            assert ready == []
            assert details["NEWCOIN"]["ready"] is False
            assert details["NEWCOIN"]["status"] == "missing"
            assert conn.execute("SELECT status FROM deep_backfill_jobs WHERE symbol='NEWCOIN'").fetchone()[0] == "pending"
        finally:
            conn.close()


def test_asset_becomes_ready_only_after_complete_history_is_persisted():
    with TemporaryDirectory() as directory:
        db_path = Path(directory) / "market.db"
        config.init_market_db(db_path)
        conn = config.get_db_connection(db_path=db_path)
        try:
            cutoff = datetime(2026, 9, 2, 12, 5, tzinfo=timezone.utc)
            _seed_5m_history(conn, "NEWCOIN", cutoff, required_5m_bars())
            ensure_backfill_jobs(conn, ["NEWCOIN"], now=cutoff)
            ready, details = ready_assets(conn, ["NEWCOIN"], cutoff)
            assert ready == ["NEWCOIN"]
            assert details["NEWCOIN"]["status"] == "covered"
            assert refresh_backfill_jobs(conn, ["NEWCOIN"], cutoff, now=cutoff) == {"NEWCOIN": "completed"}
            assert conn.execute("SELECT status FROM deep_backfill_jobs WHERE symbol='NEWCOIN'").fetchone()[0] == "completed"
        finally:
            conn.close()
