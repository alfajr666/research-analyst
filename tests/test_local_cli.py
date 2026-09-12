from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import cli


ROOT = Path(__file__).resolve().parents[1]


def invoke(monkeypatch, *arguments):
    if "RESEARCH_ANALYST_LOCAL_CLI_ENABLED" not in os.environ:
        monkeypatch.setenv("RESEARCH_ANALYST_LOCAL_CLI_ENABLED", "true")
    result = subprocess.run([str(ROOT / "cli.py"), *arguments], cwd=ROOT,
                            capture_output=True, text=True, check=False)
    return result.returncode, json.loads(result.stdout)


def test_redaction_and_envelope_are_bounded(monkeypatch):
    monkeypatch.setenv("RESEARCH_ANALYST_LOCAL_CLI_ENABLED", "false")
    code, envelope = invoke(monkeypatch, "research", "candidates")
    assert code == 4
    assert envelope["ok"] is False
    assert envelope["command"] == "research.candidates"
    assert envelope["observed_at"].endswith("Z")


def test_candidates_are_read_only_and_bounded(monkeypatch, tmp_path):
    database = tmp_path / "analyst.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("""CREATE TABLE alpha_candidates (
        candidate_id TEXT PRIMARY KEY, observed_at TEXT, asset TEXT, source_symbol TEXT,
        direction TEXT, setup_class TEXT, phase TEXT, strategy_id TEXT, liquidity_tier TEXT,
        status TEXT, valid_until TEXT, entry_condition TEXT, invalidation_price REAL,
        targets TEXT, feature_snapshot TEXT, promoted_alpha_id TEXT)""")
    connection.execute(
        "INSERT INTO alpha_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("c1", "2026-09-10T10:00:00+00:00", "BTC", "BTCUSDT", "LONG", "x", "p",
         "strategy", "unknown", "active", "2026-09-10T10:05:00+00:00", "{}", 1.0, "[]",
         json.dumps({"evaluation_cutoff": "2026-09-10T10:00:00Z"}), None),
    )
    connection.commit()
    connection.close()
    monkeypatch.setenv("ANALYST_DB_PATH", str(database))
    code, envelope = invoke(monkeypatch, "research", "candidates", "--limit", "1")
    assert code == 0
    assert envelope["data"]["count"] == 1
    assert envelope["data"]["candidates"][0]["evaluation_cutoff"].endswith("Z")
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(f"file:{database}?mode=ro", uri=True).execute("CREATE TABLE changed (id INTEGER)")


def test_publish_and_service_safety(monkeypatch):
    code, envelope = invoke(monkeypatch, "research", "publish", "alpha-1")
    assert code == 4
    assert "excluded" in envelope["warnings"][0]
    code, envelope = invoke(monkeypatch, "service", "stop", "research-analyst-ws")
    assert code == 4
    assert "--confirm" in envelope["warnings"][0]


def test_service_name_allowlist(monkeypatch):
    code, envelope = invoke(monkeypatch, "service", "logs", "not-research-analyst")
    assert code == 4
    assert "allowlisted" in envelope["warnings"][0]


def test_health_reports_degraded_for_stale_artifacts(monkeypatch, tmp_path):
    old = "2020-01-01T00:00:00Z"
    orchestrator = tmp_path / "health.json"
    websocket = tmp_path / "ws_health.json"
    orchestrator.write_text(json.dumps({"lastCycleAt": old, "dataFreshness": {}, "evaluation": {}, "ts": old}))
    websocket.write_text(json.dumps({"status": "stale", "ts": old, "last_bar_at": old}))
    monkeypatch.setenv("RESEARCH_HEALTH_PATH", str(orchestrator))
    monkeypatch.setenv("WS_HEALTH_PATH", str(websocket))
    code, envelope = invoke(monkeypatch, "health")
    assert code == 0
    assert envelope["data"]["status"] == "degraded"
    assert envelope["provenance"]["fresh"] is False


def test_status_includes_strategy_runner_health_and_normalizes_manager_time(monkeypatch):
    observed = cli.timestamp(cli.utc_now())
    manager_time = cli.utc_now().timestamp()
    manager_observed = cli._manager_timestamp(manager_time)
    services = [
        {
            "name": name,
            "status": "running",
            "desired_state": "running",
            "health_status": "healthy",
            "pid": 123,
            "restart_count": 0,
            "last_health_check": manager_time,
            "last_started_at": manager_time,
        }
        for name in cli.SERVICE_NAMES
    ]
    monkeypatch.setattr(cli, "_run_oxmgr_list", lambda: services)
    monkeypatch.setattr(cli, "_read_feed", lambda: ({"status": "ready"}, observed))
    monkeypatch.setattr(cli, "_regime_log_latest", lambda path: ({"cutoff_at": observed}, observed))
    monkeypatch.setattr(
        cli,
        "_health_file",
        lambda path: ({"status": "healthy", "lastCycleAt": observed, "dataFreshness": {}, "evaluation": {}}, observed),
    )

    data, _, warnings = cli.command_status(None)

    runner = next(item for item in data if item["name"] == "research-analyst-strategy-runner")
    assert runner["health_file"] == {"status": "healthy", "observed_at": manager_observed}
    assert runner["process_manager"]["last_started_at"] == manager_observed
    assert warnings == []


def test_health_includes_strategy_runner_health(monkeypatch):
    observed = cli.timestamp(cli.utc_now())
    manager_time = cli.utc_now().timestamp()
    manager_observed = cli._manager_timestamp(manager_time)
    runner = {
        "name": "research-analyst-strategy-runner",
        "status": "running",
        "desired_state": "running",
        "health_status": "healthy",
        "last_health_check": manager_time,
    }
    orchestrator = {"lastCycleAt": observed, "dataFreshness": {}, "evaluation": {}}
    websocket = {"status": "healthy", "ts": observed}
    monkeypatch.setattr(cli, "_run_oxmgr_list", lambda: [runner])
    monkeypatch.setattr(
        cli,
        "_health_file",
        lambda path: (orchestrator, observed) if path.name == "health.json" else (websocket, observed),
    )

    data, _, warnings = cli.command_health(None)

    assert data["status"] == "healthy"
    assert data["health_artifacts"]["orchestrator"]["status"] == "healthy"
    assert data["health_artifacts"]["strategy_runner"] == {
        "status": "healthy",
        "observed_at": manager_observed,
    }
    assert warnings == []


def test_bus_inspection_redacts_payload_without_writing(monkeypatch, tmp_path):
    database = tmp_path / "intent_bus.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("""CREATE TABLE bus_deliveries (
        delivery_id TEXT, event_id TEXT, target TEXT, exchange_id TEXT,
        account_id TEXT, payload_json TEXT, payload_schema_version INTEGER,
        status TEXT, attempts INTEGER, lease_owner TEXT, lease_until_ms INTEGER,
        next_attempt_at_ms INTEGER, last_error TEXT, created_at_ms INTEGER,
        claimed_at_ms INTEGER, completed_at_ms INTEGER)""")
    connection.execute(
        "INSERT INTO bus_deliveries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("d1", "e1", "bybit", "bybit", "hyro", json.dumps({"api_key": "secret", "asset": "BTC"}),
         1, "AVAILABLE", 0, None, None, None, None, 0, None, None),
    )
    connection.commit()
    connection.close()
    monkeypatch.setenv("INTENT_BUS_DB", str(database))
    code, envelope = invoke(monkeypatch, "bus", "deliveries")
    assert code == 0
    payload = envelope["data"]["deliveries"][0]["payload"]
    assert payload["api_key"] == "***REDACTED***"
    assert payload["asset"] == "BTC"
