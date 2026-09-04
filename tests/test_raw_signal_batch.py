from datetime import datetime, timezone
from pathlib import Path

import config
from raw_signal_batch import capture, record_status, window_start, publish_once
import orchestrator


def _event(ts):
    return {"strategy_id": "demo", "asset": "BTC", "direction": "long",
            "observed_at": ts.isoformat(), "valid_until": ts.isoformat(),
            "entry_condition": {"price": 100}, "invalidation_price": 90,
            "targets": [120]}


def test_capture_is_deduplicated_and_uses_utc_half_hour(tmp_path, monkeypatch):
    db = Path(tmp_path) / "analyst.sqlite3"
    monkeypatch.setattr(config, "ANALYST_DB_PATH", str(db))
    config.init_analyst_db(db)
    ts = datetime(2026, 8, 29, 5, 44, tzinfo=timezone.utc)
    capture(_event(ts), db)
    capture(_event(ts), db)
    conn = config.get_db_connection(read_only=True, db_path=db)
    assert conn.execute("select count(*) from raw_signals").fetchone()[0] == 1
    conn.close()
    assert window_start(ts).isoformat() == "2026-08-29T05:30:00+00:00"


def test_batch_claim_retry_does_not_duplicate_send(tmp_path, monkeypatch):
    db = Path(tmp_path) / "analyst.sqlite3"
    monkeypatch.setattr(config, "ANALYST_DB_PATH", str(db))
    monkeypatch.setattr(config, "RAW_SIGNAL_DISCORD_BATCH_ENABLED", True)
    config.init_analyst_db(db)
    raw_id = capture(_event(datetime(2026, 8, 29, 5, 40, tzinfo=timezone.utc)), db)
    record_status(raw_id, hard_gate_status="pass", db_path=db)

    class Transport:
        calls = 0
        def send(self, text):
            self.calls += 1
            return "ok"

    transport = Transport()
    assert publish_once(datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc), db, transport)
    assert not publish_once(datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc), db, transport)
    assert transport.calls == 1


def test_raw_batch_publisher_failure_is_isolated(monkeypatch, capsys):
    def fail():
        raise RuntimeError("discord unavailable")

    monkeypatch.setattr("raw_signal_batch.publish_once", fail)
    orchestrator._publish_raw_signal_batch()

    assert "discord unavailable" in capsys.readouterr().err


def test_raw_batch_trigger_is_non_blocking(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target, self.name, self.daemon = target, name, daemon
        def start(self):
            started.append(self)

    monkeypatch.setattr(orchestrator.threading, "Thread", FakeThread)
    worker = orchestrator.trigger_raw_signal_batch()

    assert worker is started[0]
    assert worker.daemon is True
    assert worker.name == "raw-signal-batch"
