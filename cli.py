#!/usr/bin/env python3
"""Read-only local operator CLI for Research Analyst.

The CLI deliberately does not import worker modules.  In particular, all
observational SQLite access uses ``mode=ro`` and never initializes a schema.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
SERVICE_NAMES = (
    "research-analyst-symbol-rotation",
    "research-analyst-ws",
    "research-analyst-regime-session",
    "research-analyst-orchestrator",
)
SECRET_KEYS = {
    "api_key", "api_secret", "secret", "password", "private_key", "token",
    "webhook_url", "authorization", "auth", "credential", "access_token",
}
SECRET_VALUE_NAMES = re.compile(r"(?:api[_-]?key|api[_-]?secret|password|private[_-]?key|token|webhook|credential|authorization)", re.I)
WEBHOOK_VALUE = re.compile(r"https?://(?:discord(?:app)?\.com/api/webhooks|hooks\.slack\.com)/", re.I)
REPORT_POLICY_WORDS = re.compile(r"\b(?:guaranteed|certain|leverage|position sizing|execute|execution|buy|sell)\b", re.I)


class CliError(Exception):
    """An expected CLI failure with the public exit code."""

    def __init__(self, message: str, code: int = 3, *, warnings: Iterable[str] = (), data: Any = None):
        super().__init__(message)
        self.code = code
        self.warnings = list(warnings)
        self.data = data


class ArgumentError(Exception):
    pass


def _load_dotenv() -> None:
    """Match worker configuration without making dotenv a runtime requirement."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env", override=False)


_load_dotenv()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _path(name: str, default: str) -> Path:
    value = Path(_env(name, default)).expanduser()
    return value if value.is_absolute() else ROOT / value


def _cli_enabled() -> bool:
    return _env("RESEARCH_ANALYST_LOCAL_CLI_ENABLED", "true").strip().lower() in {
        "1", "true", "yes", "on"
    }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _manager_timestamp(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return timestamp(datetime.fromtimestamp(value / 1000, timezone.utc))
        except (OverflowError, OSError, ValueError):
            return None
    return timestamp(value)


def _parsed_time(value: Any) -> datetime | None:
    normalized = timestamp(value)
    if normalized is None:
        return None
    return datetime.fromisoformat(normalized.replace("Z", "+00:00"))


def _fresh(value: Any, max_age: float | None = None) -> bool:
    parsed = _parsed_time(value)
    if parsed is None:
        return False
    age = (utc_now() - parsed).total_seconds()
    return 0 <= age <= (max_age if max_age is not None else _freshness_limit())


def _freshness_limit() -> float:
    try:
        return max(0.0, float(_env("DATA_FRESHNESS_MAX_SECONDS", "600")))
    except ValueError:
        return 600.0


def _secret_values() -> set[str]:
    return {
        value for key, value in os.environ.items()
        if value and SECRET_VALUE_NAMES.search(key)
    }


def _key_is_secret(key: Any) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return normalized in SECRET_KEYS or normalized.endswith(("_secret", "_token", "_password"))


def redact(value: Any) -> Any:
    """Redact credential-shaped keys and known secret values recursively."""
    known = _secret_values()

    def visit(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: "***REDACTED***" if _key_is_secret(key) else visit(child)
                    for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, tuple):
            return [visit(child) for child in item]
        if isinstance(item, str) and (item in known or WEBHOOK_VALUE.search(item) or item.lower().startswith("bearer ")):
            return "***REDACTED***"
        return item

    return visit(value)


def _bounded(value: Any, *, depth: int = 0, list_limit: int = MAX_LIMIT) -> Any:
    """Keep serialized records finite even when a legacy payload is malformed."""
    if depth > 8:
        return "[truncated]"
    if isinstance(value, dict):
        return {str(key): _bounded(child, depth=depth + 1, list_limit=list_limit)
                for key, child in list(value.items())[:MAX_LIMIT]}
    if isinstance(value, (list, tuple)):
        items = list(value)[:list_limit]
        result = [_bounded(child, depth=depth + 1, list_limit=list_limit) for child in items]
        return result if len(value) <= list_limit else result + ["[truncated]"]
    if isinstance(value, str):
        return value if len(value) <= 2000 else value[:1997] + "..."
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:2000]


def safe_json(value: Any, *, list_limit: int = MAX_LIMIT) -> Any:
    return _bounded(redact(value), list_limit=list_limit)


def _json(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise CliError(f"required source is unavailable: {path}", 3)
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection
    except (OSError, sqlite3.Error) as error:
        raise CliError(f"required source cannot be read: {path}", 3) from error


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )}


def _require_tables(connection: sqlite3.Connection, *names: str) -> None:
    missing = [name for name in names if name not in _tables(connection)]
    if missing:
        raise CliError(f"required schema is unavailable: {', '.join(missing)}", 3)


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _limit(value: int | None) -> int:
    result = DEFAULT_LIMIT if value is None else value
    if result < 1 or result > MAX_LIMIT:
        raise CliError(f"--limit must be between 1 and {MAX_LIMIT}", 1)
    return result


def _since(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = timestamp(value)
    if parsed is None:
        raise CliError("--since must be a valid ISO-8601 timestamp", 1)
    return parsed


def _source(path: Path, observed: Any, fresh: bool, warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "service": "research-analyst",
        "source": str(path),
        "fresh": bool(fresh),
        "observed_at": timestamp(observed),
    }


def _envelope(command: str, data: Any, *, warnings: Iterable[str] = (), provenance: dict[str, Any] | None = None,
              ok: bool = True) -> dict[str, Any]:
    observed = timestamp(utc_now())
    return {
        "ok": ok,
        "command": command,
        "observed_at": observed,
        "data": safe_json(data),
        "warnings": safe_json(list(warnings)),
        "provenance": safe_json(provenance or _source(ROOT, observed, True)),
    }


def _failure(command: str, error: CliError) -> tuple[dict[str, Any], int]:
    warnings = [str(error), *error.warnings]
    return _envelope(command, error.data, warnings=warnings,
                     provenance=_source(ROOT, utc_now(), False), ok=False), error.code


def _run_oxmgr(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["oxmgr", *arguments], capture_output=True, text=True,
                              timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise CliError(f"oxmgr is unavailable: {error}", 3) from error


def _parse_oxmgr_list(output: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise CliError("oxmgr returned invalid JSON", 3) from error
    if not isinstance(value, list):
        raise CliError("oxmgr returned an invalid process list", 3)
    return [item for item in value if isinstance(item, dict)]


def _health_file(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None, None
    return (value, timestamp(value.get("lastCycleAt") or value.get("last_bar_at") or value.get("ts"))) \
        if isinstance(value, dict) else (None, None)


def _regime_log_latest(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, None
    for line in reversed(lines):
        candidate = line
        if len(line) >= 21 and line[19:21] == ": ":
            candidate = line[21:]
        value = _json(candidate)
        if isinstance(value, dict) and "cutoff_at" in value:
            return value, timestamp(value.get("cutoff_at"))
    return None, None


def command_status(args: argparse.Namespace) -> tuple[Any, dict[str, Any], list[str]]:
    result = _run_oxmgr_list()
    by_name = {item.get("name"): item for item in result}
    data: list[dict[str, Any]] = []
    warnings: list[str] = []
    all_fresh = True
    health_paths = {
        "research-analyst-orchestrator": _path("RESEARCH_HEALTH_PATH", "data/health.json"),
        "research-analyst-ws": _path("WS_HEALTH_PATH", "data/ws_health.json"),
    }
    for name in SERVICE_NAMES:
        manager = by_name.get(name, {})
        health, observed = (None, None)
        if name in health_paths:
            health, observed = _health_file(health_paths[name])
            health_status = health.get("status") if health else "unknown"
            if not health_status:
                health_status = "healthy" if observed and _fresh(observed) else "unknown"
        elif name == "research-analyst-symbol-rotation":
            feed, observed = _read_feed()
            health_status = feed.get("status", "unknown") if feed else "unknown"
        else:
            cycle, observed = _regime_log_latest(_path("REGIME_SESSION_LOG", "/home/ubuntu/.local/share/oxmgr/logs/research-analyst-regime-session.out.log"))
            health_status = "fresh" if cycle else "unknown"
        if not health and name in health_paths:
            warnings.append(f"health artifact unavailable for {name}")
        service_fresh = bool(observed and _fresh(observed))
        if name == "research-analyst-symbol-rotation":
            service_fresh = bool(observed and _fresh(observed, _rotation_source_max_age_seconds())) \
                and health_status in {"ready", "degraded"}
        if name in health_paths:
            service_fresh = service_fresh and health_status not in {"stale", "unknown", None}
        if not manager:
            warnings.append(f"process-manager record unavailable for {name}")
            service_fresh = False
        all_fresh = all_fresh and service_fresh
        data.append({
            "name": name,
            "process_manager": {
                "status": manager.get("status", "unknown"),
                "desired_state": manager.get("desired_state", "unknown"),
                "health_status": manager.get("health_status", "unknown"),
                "pid": manager.get("pid"),
                "restart_count": manager.get("restart_count"),
                "last_started_at": _manager_timestamp(manager.get("last_started_at")),
            },
            "health_file": {"status": health_status, "observed_at": observed},
            "last_observed_cycle": observed,
            "stale_reason": None if observed and _fresh(observed) else "missing_or_stale_observation",
        })
    return data, _source(ROOT, utc_now(), all_fresh), warnings


def _run_oxmgr_list() -> list[dict[str, Any]]:
    completed = _run_oxmgr(["list", "--json"])
    if completed.returncode != 0:
        raise CliError("oxmgr list failed", 3)
    return _parse_oxmgr_list(completed.stdout)


def _read_feed() -> tuple[dict[str, Any] | None, str | None]:
    path = _path("SYMBOL_ROTATION_FEED_PATH", "data/symbol_rotation_feed.json")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None, None
    return (value, timestamp(value.get("generated_at") or value.get("valid_from"))) \
        if isinstance(value, dict) else (None, None)


def command_health(args: argparse.Namespace) -> tuple[Any, dict[str, Any], list[str]]:
    health_path = _path("RESEARCH_HEALTH_PATH", "data/health.json")
    ws_path = _path("WS_HEALTH_PATH", "data/ws_health.json")
    orchestrator, orch_observed = _health_file(health_path)
    ws, ws_observed = _health_file(ws_path)
    if isinstance(orchestrator, dict) and not {
        "lastCycleAt", "dataFreshness", "evaluation"
    } <= orchestrator.keys():
        orchestrator = None
    if isinstance(ws, dict) and not {"status", "ts"} <= ws.keys():
        ws = None
    if orchestrator is None or ws is None or orch_observed is None or ws_observed is None:
        missing = health_path if orchestrator is None or orch_observed is None else ws_path
        raise CliError(
            f"required health artifact cannot be read: {missing}", 3,
            data={"status": "unknown", "worker_anomalies": [{"source": str(missing), "status": "unknown"}]},
        )
    warnings: list[str] = []
    fresh = True
    for label, value, age in (("orchestrator", orch_observed, _freshness_limit()), ("ws", ws_observed, _ws_stale_seconds())):
        if not _fresh(value, age):
            fresh = False
            warnings.append(f"{label} health observation is stale or invalid")
    if str(ws.get("status", "")).lower() not in {"healthy", "ready"}:
        fresh = False
        warnings.append(f"ws gateway status is {ws.get('status', 'unknown')}")
    anomalies: list[Any] = []
    if ws.get("last_error"):
        anomalies.append({"component": "ws_gateway", "reason": ws["last_error"]})
    evaluation = orchestrator.get("evaluation", {})
    if isinstance(evaluation, dict):
        for key in ("error", "anomaly", "anomalies", "failed", "pipeline_error"):
            if evaluation.get(key):
                anomalies.append({"component": "orchestrator", "reason": evaluation[key]})
    if anomalies:
        fresh = False
        warnings.append("blocking worker anomaly is present")
    data = {
        "status": "healthy" if fresh else "degraded",
        "data_freshness": orchestrator.get("dataFreshness"),
        "cutoff_progress": evaluation.get("cutoff_progress", evaluation.get("last_cutoff")) if isinstance(evaluation, dict) else None,
        "regime_readiness": evaluation.get("regime_readiness") if isinstance(evaluation, dict) else None,
        "pipeline_counts": evaluation.get("pipeline_counts", evaluation.get("counts")) if isinstance(evaluation, dict) else None,
        "publisher_state": evaluation.get("publisher_state", evaluation.get("publisher")) if isinstance(evaluation, dict) else None,
        "worker_anomalies": anomalies,
        "health_artifacts": {
            "orchestrator": {"status": orchestrator.get("status", "unknown"), "observed_at": orch_observed},
            "ws": {"status": ws.get("status", "unknown"), "observed_at": ws_observed},
        },
    }
    observed = max((item for item in (orch_observed, ws_observed) if item), default=None)
    return data, _source(health_path, observed, fresh), warnings


def _ws_stale_seconds() -> float:
    try:
        return max(0.0, float(_env("WS_STALE_SECONDS", "180")))
    except ValueError:
        return 180.0


def _latest_status(connection: sqlite3.Connection, raw_signal_id: str) -> dict[str, Any]:
    if "raw_signal_status_history" not in _tables(connection):
        return {}
    row = connection.execute(
        "SELECT * FROM raw_signal_status_history WHERE raw_signal_id=? ORDER BY recorded_at DESC LIMIT 1",
        (raw_signal_id,),
    ).fetchone()
    return _row_dict(row) or {}


def _event_for(connection: sqlite3.Connection, alpha_id: str | None) -> dict[str, Any]:
    if not alpha_id or "alpha_events" not in _tables(connection):
        return {}
    row = connection.execute(
        "SELECT event_json, status FROM alpha_events WHERE alpha_id=? ORDER BY persisted_at DESC LIMIT 1",
        (alpha_id,),
    ).fetchone()
    value = _json(row[0]) if row else {}
    if isinstance(value, dict) and row:
        value["_persisted_status"] = row[1]
    return value if isinstance(value, dict) else {}


def _proof(event: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    candidates = (
        event.get("_admission_result"), event.get("_score_result"),
        (event.get("metadata") or {}).get("admission_result") if isinstance(event.get("metadata"), dict) else None,
        payload.get("_admission_result"), payload.get("admission_result"),
    )
    return next((value for value in candidates if isinstance(value, dict)), {})


def _cutoff(payload: dict[str, Any], event: dict[str, Any]) -> str | None:
    for source in (payload, event, payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}):
        for key in ("evaluation_cutoff", "evaluation_cutoff_at", "cutoff", "cutoff_at"):
            if isinstance(source, dict) and source.get(key) is not None:
                return timestamp(source[key]) or str(source[key])
    return None


def _candidate_record(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    raw = _row_dict(row) or {}
    feature = _json(raw.get("feature_snapshot"), {})
    feature = feature if isinstance(feature, dict) else {}
    event = _event_for(connection, raw.get("promoted_alpha_id") or raw.get("candidate_id"))
    proof = _proof(event, feature)
    status = _latest_status(connection, str(raw["candidate_id"]))
    admission = status.get("hard_gate_status") or proof.get("hard_gate") or proof.get("status") or "unknown"
    structural = proof.get("structural_stop_gate") or proof.get("structural_admission_status") or "unknown"
    return {
        "candidate_id": raw.get("candidate_id"),
        "asset": raw.get("asset"),
        "direction": raw.get("direction"),
        "strategy_id": raw.get("strategy_id"),
        "evaluation_cutoff": _cutoff(feature, event),
        "candidate_state": raw.get("status") or "unknown",
        "admission_status": admission,
        "score_status": status.get("score_status") or ("available" if proof.get("quality_score") is not None else "unknown"),
        "structural_proof_status": structural,
        "observed_at": timestamp(raw.get("observed_at")),
        "valid_until": timestamp(raw.get("valid_until")),
        "promoted_alpha_id": raw.get("promoted_alpha_id"),
    }


def command_candidates(args: argparse.Namespace) -> tuple[Any, dict[str, Any], list[str]]:
    path = _path("ANALYST_DB_PATH", "data/analyst.sqlite3")
    limit = _limit(args.limit)
    connection = _read_only(path)
    try:
        _require_tables(connection, "alpha_candidates")
        clauses: list[str] = []
        params: list[Any] = []
        if args.asset:
            clauses.append("UPPER(asset)=UPPER(?)")
            params.append(args.asset)
        if args.strategy:
            clauses.append("strategy_id=?")
            params.append(args.strategy)
        if args.direction:
            clauses.append("UPPER(direction)=UPPER(?)")
            params.append(args.direction)
        if args.status:
            clauses.append("status=?")
            params.append(args.status)
        if args.since:
            clauses.append("observed_at>=?")
            params.append(_since(args.since))
        query = "SELECT * FROM alpha_candidates"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY observed_at DESC, candidate_id DESC LIMIT ?"
        rows = connection.execute(query, (*params, limit)).fetchall()
        records = [_candidate_record(connection, row) for row in rows]
        observed = max((item["observed_at"] for item in records if item["observed_at"]), default=None)
        fresh = observed is None or _fresh(observed)
        warnings = [] if fresh else ["candidate observations are stale"]
        data = {"candidates": records, "count": len(records)}
    finally:
        connection.close()
    return data, _source(path, observed or utc_now(), fresh), warnings


def _delivery_rows(connection: sqlite3.Connection, alpha_id: str) -> list[dict[str, Any]]:
    if "execution_deliveries" not in _tables(connection):
        return []
    rows = connection.execute(
        "SELECT target, status, reason, inbox_path, written_at, acknowledged_at FROM execution_deliveries WHERE alpha_id=? ORDER BY target",
        (alpha_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = _row_dict(row) or {}
        state = str(item.get("status") or "unknown").lower()
        if state in {"filled", "open", "position_open"}:
            state = "unknown"
        item["status"] = state
        item["written_at"] = timestamp(item.get("written_at"))
        item["acknowledged_at"] = timestamp(item.get("acknowledged_at"))
        item.pop("inbox_path", None)
        result.append(item)
    return result


def _signal_record(connection: sqlite3.Connection, row: sqlite3.Row, *, detail: bool = False) -> dict[str, Any]:
    raw = _row_dict(row) or {}
    payload = _json(raw.get("payload_json"), {})
    payload = payload if isinstance(payload, dict) else {}
    status = _latest_status(connection, str(raw.get("raw_signal_id")))
    event = _event_for(connection, str(raw.get("candidate_id")))
    proof = _proof(event, payload)
    alpha_id = event.get("alpha_id") or raw.get("candidate_id")
    deliveries = _delivery_rows(connection, str(alpha_id)) if alpha_id else []
    delivery_published = any(item.get("status") in {"written", "acknowledged"} for item in deliveries)
    persisted_status = str(event.get("_persisted_status") or "unknown").lower()
    alpha_state = "published" if delivery_published else persisted_status
    result: dict[str, Any] = {
        "raw_signal_id": raw.get("raw_signal_id"),
        "candidate_id": raw.get("candidate_id"),
        "asset": raw.get("asset"),
        "direction": raw.get("direction"),
        "strategy_id": raw.get("strategy_id"),
        "evaluation_cutoff": _cutoff(payload, event),
        "raw_candidate_state": "emitted",
        "admission_state": {
            "hard_gate": status.get("hard_gate_status") or proof.get("hard_gate") or "unknown",
            "score": status.get("score_status") or "unknown",
            "clash": status.get("clash_status") or "unknown",
        },
        "alpha_event_state": alpha_state if event else "not_published",
        "executor_delivery_state": deliveries,
        "observed_at": timestamp(raw.get("observed_at")),
        "valid_until": timestamp(raw.get("valid_until")),
    }
    if detail:
        result["admission_proof"] = proof or None
        result["source_evidence_references"] = _evidence_references(payload, proof)
        result["target_delivery_references"] = result["executor_delivery_state"]
        result["record"] = {
            key: payload.get(key) for key in
            ("entry_condition", "invalidation_price", "targets", "setup_class", "phase", "source_symbol")
            if key in payload
        }
    return result


def _evidence_references(*values: Any) -> list[Any]:
    found: list[Any] = []
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if "evidence" in str(key).lower() and isinstance(child, (str, list)):
                    found.append(child)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    for value in values:
        walk(value)
    return found[:MAX_LIMIT]


def command_signals(args: argparse.Namespace) -> tuple[Any, dict[str, Any], list[str]]:
    path = _path("ANALYST_DB_PATH", "data/analyst.sqlite3")
    limit = _limit(args.limit)
    connection = _read_only(path)
    try:
        _require_tables(connection, "raw_signals")
        clauses: list[str] = []
        params: list[Any] = []
        if args.asset:
            clauses.append("UPPER(asset)=UPPER(?)")
            params.append(args.asset)
        if args.strategy:
            clauses.append("strategy_id=?")
            params.append(args.strategy)
        if args.direction:
            clauses.append("UPPER(direction)=UPPER(?)")
            params.append(args.direction)
        if args.since:
            clauses.append("observed_at>=?")
            params.append(_since(args.since))
        query = "SELECT * FROM raw_signals"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY observed_at DESC, raw_signal_id DESC LIMIT ?"
        rows = connection.execute(query, (*params, limit)).fetchall()
        records = [_signal_record(connection, row) for row in rows]
    finally:
        connection.close()
    observed = max((item["observed_at"] for item in records if item["observed_at"]), default=None)
    fresh = observed is None or _fresh(observed)
    warnings = [] if fresh else ["raw signal observations are stale"]
    return {"signals": records, "count": len(records)}, _source(path, observed or utc_now(), fresh), warnings


def command_signal(args: argparse.Namespace) -> tuple[Any, dict[str, Any], list[str]]:
    path = _path("ANALYST_DB_PATH", "data/analyst.sqlite3")
    connection = _read_only(path)
    try:
        _require_tables(connection, "raw_signals")
        row = connection.execute("SELECT * FROM raw_signals WHERE raw_signal_id=?", (args.signal_id,)).fetchone()
        if row is None:
            raise CliError(f"signal not found: {args.signal_id}", 2)
        data = _signal_record(connection, row, detail=True)
    finally:
        connection.close()
    fresh = data.get("observed_at") is None or _fresh(data.get("observed_at"))
    return data, _source(path, data.get("observed_at"), fresh), ([] if fresh else ["raw signal observation is stale"])


def _report_rows(connection: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    tables = _tables(connection)
    if "research_artifacts" in tables and "research_requests" in tables:
        rows = connection.execute(
            """SELECT a.artifact_id, a.request_id, a.generated_at, a.verdict,
                      r.subject_type, r.subject_id
                 FROM research_artifacts a LEFT JOIN research_requests r ON r.request_id=a.request_id
                ORDER BY a.generated_at DESC LIMIT ?""", (limit,)
        ).fetchall()
        result = []
        for row in rows:
            item = _row_dict(row) or {}
            item["generated_at"] = timestamp(item.get("generated_at"))
            item["research_only"] = True
            result.append(item)
        return result
    if "research_reports" in tables:
        rows = connection.execute("SELECT * FROM research_reports ORDER BY 1 DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = _row_dict(row) or {}
            report = _json(item.get("report_json") or item.get("report"), {})
            if report and _report_is_unsafe(report):
                raise CliError("report contains prohibited execution language", 4)
            item.pop("report_json", None)
            item["report"] = report if isinstance(report, dict) else None
            item["generated_at"] = timestamp(item.get("generated_at") or item.get("created_at") or item.get("persisted_at"))
            item["research_only"] = True
            item["evidence"] = []
            result.append(item)
        return result
    raise CliError("required schema is unavailable: research reports", 3)


def _report_detail(connection: sqlite3.Connection, report_id: str) -> dict[str, Any] | None:
    tables = _tables(connection)
    if "research_artifacts" in tables:
        if "research_requests" in tables:
            row = connection.execute(
                """SELECT a.*, r.subject_type, r.subject_id FROM research_artifacts a
                    LEFT JOIN research_requests r ON r.request_id=a.request_id
                    WHERE a.artifact_id=?""", (report_id,),
            ).fetchone()
        else:
            row = connection.execute("SELECT * FROM research_artifacts WHERE artifact_id=?", (report_id,)).fetchone()
        if row is None:
            return None
        item = _row_dict(row) or {}
        report = _json(item.pop("report_json", None), {})
        if not isinstance(report, dict) or _report_is_unsafe(report):
            raise CliError("report contains prohibited execution language", 4)
        item.pop("input_json", None)
        item.pop("provider_usage_json", None)
        item["report"] = report
        item["evidence"] = []
        if "research_evidence" in tables:
            evidence = connection.execute(
                "SELECT source_type, source_ref, observed_at, retrieved_at, excerpt FROM research_evidence WHERE artifact_id=? ORDER BY evidence_id LIMIT ?",
                (report_id, MAX_LIMIT),
            ).fetchall()
            item["evidence"] = [{**(_row_dict(row) or {}), "observed_at": timestamp(row["observed_at"]),
                                  "retrieved_at": timestamp(row["retrieved_at"])} for row in evidence]
        item["generated_at"] = timestamp(item.get("generated_at"))
        item["research_only"] = True
        return item
    if "research_reports" in tables:
        row = connection.execute("SELECT * FROM research_reports WHERE report_id=?", (report_id,)).fetchone()
        item = _row_dict(row) if row else None
        if item is not None:
            report = _json(item.get("report_json") or item.get("report"), {})
            if report and _report_is_unsafe(report):
                raise CliError("report contains prohibited execution language", 4)
            item.pop("report_json", None)
            item["report"] = report if isinstance(report, dict) else None
            item["generated_at"] = timestamp(item.get("generated_at") or item.get("created_at") or item.get("persisted_at"))
            item["research_only"] = True
            item["evidence"] = []
        return item
    raise CliError("required schema is unavailable: research reports", 3)


def _report_is_unsafe(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_report_is_unsafe(key) or _report_is_unsafe(child) for key, child in value.items())
    if isinstance(value, list):
        return any(_report_is_unsafe(child) for child in value)
    return isinstance(value, str) and bool(REPORT_POLICY_WORDS.search(value))


def command_reports(args: argparse.Namespace) -> tuple[Any, dict[str, Any], list[str]]:
    path = _path("ANALYST_DB_PATH", "data/analyst.sqlite3")
    connection = _read_only(path)
    try:
        records = _report_rows(connection, _limit(args.limit))
    finally:
        connection.close()
    observed = max((item.get("generated_at") for item in records if item.get("generated_at")), default=None)
    fresh = bool(records) and all(_fresh(item.get("generated_at")) for item in records)
    warnings = [] if fresh else ["research report artifacts are stale or have unknown timestamps"]
    data = {"reports": records, "count": len(records), "research_only": True}
    if not fresh:
        raise CliError("research report artifacts are stale or unavailable", 3, data=data, warnings=warnings)
    return data, _source(path, observed, True), warnings


def command_report(args: argparse.Namespace) -> tuple[Any, dict[str, Any], list[str]]:
    path = _path("ANALYST_DB_PATH", "data/analyst.sqlite3")
    connection = _read_only(path)
    try:
        data = _report_detail(connection, args.report_id)
    finally:
        connection.close()
    if data is None:
        raise CliError(f"report not found: {args.report_id}", 2)
    fresh = _fresh(data.get("generated_at"))
    warnings = [] if fresh else ["research report artifact is stale or has an unknown timestamp"]
    if not fresh:
        raise CliError("research report artifact is stale or unavailable", 3, data=data, warnings=warnings)
    return data, _source(path, data.get("generated_at"), True), warnings


def command_regime(args: argparse.Namespace) -> tuple[Any, dict[str, Any], list[str]]:
    path = _path("REGIME_DB_PATH", "data/regime.sqlite3")
    connection = _read_only(path)
    try:
        _require_tables(connection, "regime_scores", "regime_gate_decisions")
        cutoff_row = connection.execute(
            "SELECT MAX(cutoff_at) FROM regime_gate_decisions"
        ).fetchone()
        cutoff = cutoff_row[0] if cutoff_row else None
        if cutoff is None:
            raise CliError("regime observations are unavailable", 3)
        rows = connection.execute(
            """SELECT g.*, s.status AS score_status, s.trend_weight, s.mean_reversion_weight,
                      s.reversal_weight, s.confidence, s.recorded_at AS score_recorded_at,
                      s.source_observation_ids
                 FROM regime_gate_decisions g LEFT JOIN regime_scores s
                   ON s.observation_id=g.score_observation_id
                WHERE g.cutoff_at=? ORDER BY g.asset LIMIT ?""", (cutoff, _limit(args.limit)),
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        raise CliError("regime observations are unavailable", 3)
    mode = _env("REGIME_SESSION_MODE", "shadow").strip().lower()
    if mode not in {"off", "shadow", "enforce"}:
        mode = "unknown"
    assets: list[dict[str, Any]] = []
    blocked: list[str] = []
    families: set[str] = set()
    for row in rows:
        item = _row_dict(row) or {}
        activation = _json(item.get("family_activation_json"), {})
        active = []
        if isinstance(activation, dict):
            family_values = activation.get("families", {})
            if isinstance(family_values, dict):
                active = [family for family, value in family_values.items()
                          if isinstance(value, dict) and value.get("active")]
        families.update(active)
        decision = item.get("decision") or "unknown"
        if decision == "block":
            blocked.append(str(item.get("asset")))
        assets.append({
            "asset": item.get("asset"), "decision": decision,
            "score_status": item.get("score_status") or "unknown",
            "readiness": "ready" if item.get("score_status") == "ok" else "unknown",
            "active_families": active,
            "reasons": _json(item.get("reasons_json"), []),
        })
    fresh = _fresh(cutoff)
    warnings = [] if fresh else ["latest regime observation is stale"]
    data = {
        "cutoff": timestamp(cutoff), "mode": mode,
        "readiness": {"assets": len(assets), "score_ready": sum(x["readiness"] == "ready" for x in assets)},
        "active_families": sorted(families),
        "blocked_assets": blocked,
        "would_block_assets": blocked if mode == "shadow" else [],
        "operational_blocked_assets": blocked if mode == "enforce" else [],
        "assets": assets,
    }
    return data, _source(path, cutoff, fresh), warnings


def command_watchlist(args: argparse.Namespace) -> tuple[Any, dict[str, Any], list[str]]:
    path = _path("SYMBOL_ROTATION_FEED_PATH", "data/symbol_rotation_feed.json")
    feed, observed = _read_feed()
    if feed is None:
        raise CliError(f"watchlist feed cannot be read: {path}", 3)
    now = utc_now()
    permanent = [str(value) for value in feed.get("permanent_symbols", ["BTC", "ETH", "PAXG", "QQQ"])]
    entries = feed.get("watchlist_entries", [])
    rotating: list[str] = []
    expiry: dict[str, str | None] = {}
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("asset"):
                continue
            expires = _parsed_time(entry.get("expires_at"))
            if expires is not None and expires <= now:
                continue
            asset = str(entry["asset"])
            rotating.append(asset)
            expiry[asset] = timestamp(entry.get("expires_at"))
    if not entries:
        rotating = [str(value) for value in feed.get("symbols", []) if str(value) not in permanent]
    rotation_age = _rotation_source_max_age_seconds()
    fresh = _fresh(observed, rotation_age) and str(feed.get("status", "")).lower() in {"ready", "degraded"}
    valid_until = _parsed_time(feed.get("valid_until"))
    if valid_until is not None and valid_until <= now:
        fresh = False
    status = feed.get("status", "unknown")
    warnings = [] if fresh else [f"rotation feed is {status} or stale"]
    requested_limit = _limit(args.limit)
    if len(rotating) > requested_limit:
        rotating = rotating[:requested_limit]
        warnings.append(f"watchlist rotating assets truncated at {requested_limit}")
    expiry = dict(list(expiry.items())[:requested_limit])
    permanent = permanent[:requested_limit]
    data = {
        "feed_id": feed.get("feed_id"),
        "universe_version": feed.get("effective_universe_version"),
        "freshness": fresh,
        "freshness_state": feed.get("freshness_state", status),
        "permanent_assets": permanent,
        "rotating_assets": rotating,
        "expiry": expiry,
        "source_status": status,
        "valid_until": timestamp(feed.get("valid_until")),
    }
    return data, _source(path, observed, fresh), warnings


def _rotation_source_max_age_seconds() -> float:
    try:
        return max(0.0, float(_env("SYMBOL_ROTATION_SOURCE_MAX_AGE_HOURS", "6")) * 3600)
    except ValueError:
        return 21600.0


def _bus_path() -> Path:
    raw = _env("INTENT_BUS_DB", "").strip()
    if not raw:
        raise CliError("shared intent bus is unavailable: INTENT_BUS_DB is not configured", 3)
    return _path("INTENT_BUS_DB", raw)


def _bus_row(row: sqlite3.Row, *, include_payload: bool = True) -> dict[str, Any]:
    item = _row_dict(row) or {}
    for key in ("created_at_ms", "claimed_at_ms", "completed_at_ms", "lease_until_ms", "next_attempt_at_ms"):
        if item.get(key) is not None:
            item[key.removesuffix("_ms")] = timestamp(datetime.fromtimestamp(item[key] / 1000, timezone.utc))
        item.pop(key, None)
    if include_payload:
        item["payload"] = _json(item.pop("payload_json", None), None)
    else:
        item.pop("payload_json", None)
    return item


def command_bus_deliveries(args: argparse.Namespace) -> tuple[Any, dict[str, Any], list[str]]:
    path = _bus_path()
    connection = _read_only(path)
    try:
        _require_tables(connection, "bus_deliveries")
        rows = connection.execute("SELECT * FROM bus_deliveries ORDER BY created_at_ms DESC LIMIT ?", (_limit(args.limit),)).fetchall()
        records = [_bus_row(row) for row in rows]
    finally:
        connection.close()
    return {"deliveries": records, "count": len(records)}, _source(path, utc_now(), True), []


def command_bus_delivery(args: argparse.Namespace) -> tuple[Any, dict[str, Any], list[str]]:
    path = _bus_path()
    connection = _read_only(path)
    try:
        _require_tables(connection, "bus_deliveries")
        row = connection.execute("SELECT * FROM bus_deliveries WHERE delivery_id=?", (args.delivery_id,)).fetchone()
    finally:
        connection.close()
    if row is None:
        raise CliError(f"delivery not found: {args.delivery_id}", 2)
    return _bus_row(row), _source(path, utc_now(), True), []


def command_bus_receipts(args: argparse.Namespace) -> tuple[Any, dict[str, Any], list[str]]:
    path = _bus_path()
    connection = _read_only(path)
    try:
        _require_tables(connection, "bus_deliveries", "bus_receipts")
        exists = connection.execute("SELECT 1 FROM bus_deliveries WHERE delivery_id=?", (args.delivery_id,)).fetchone()
        if exists is None:
            raise CliError(f"delivery not found: {args.delivery_id}", 2)
        rows = connection.execute(
            "SELECT * FROM bus_receipts WHERE delivery_id=? ORDER BY attempt LIMIT ?",
            (args.delivery_id, _limit(args.limit)),
        ).fetchall()
        receipts = []
        for row in rows:
            item = _row_dict(row) or {}
            if item.get("created_at_ms") is not None:
                item["created_at"] = timestamp(datetime.fromtimestamp(item.pop("created_at_ms") / 1000, timezone.utc))
            item["result"] = _json(item.pop("result_json", None), {})
            receipts.append(item)
    finally:
        connection.close()
    return {"delivery_id": args.delivery_id, "receipts": receipts}, _source(path, utc_now(), True), []


def command_service(args: argparse.Namespace) -> tuple[Any, dict[str, Any], list[str]]:
    name = args.name
    if name not in SERVICE_NAMES:
        raise CliError(f"service is not allowlisted: {name}", 4)
    if args.action in {"stop", "restart"} and not args.confirm:
        raise CliError(f"service {args.action} requires --confirm", 4)
    manager_action = args.action
    if args.action == "logs":
        lines = _limit(args.lines)
        completed = _run_oxmgr(["logs", name, "--lines", str(lines)])
        output = completed.stdout
    else:
        if args.action == "start":
            # oxmgr's `start` registers a new command.  Restart is its
            # existing-target operation, so never pass a service name to the
            # registration command.
            manager = next((item for item in _run_oxmgr_list() if item.get("name") == name), None)
            if manager is None:
                raise CliError(f"service is not registered with oxmgr: {name}", 4)
            if manager.get("status") in {"running", "starting"}:
                return {"service": name, "action": "start", "status": "already_running", "output": ""}, \
                    _source(ROOT, utc_now(), True), []
            manager_action = "restart"
        completed = _run_oxmgr([manager_action, name])
        output = completed.stdout
    if completed.returncode != 0:
        raise CliError(f"oxmgr {args.action} failed for {name}", 3)
    return {"service": name, "action": args.action, "manager_action": manager_action,
            "status": "completed", "output": output}, \
        _source(ROOT, utc_now(), True), []


def command_refused_publish(args: argparse.Namespace) -> tuple[Any, dict[str, Any], list[str]]:
    raise CliError("research publish is excluded from CLI version 1", 4)


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS,
                        help="indent JSON output")


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--asset")
    parser.add_argument("--strategy")
    parser.add_argument("--direction")
    parser.add_argument("--since")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    _add_output_options(parser)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ArgumentError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="./cli.py", description="Research Analyst local read-only CLI")
    parser.add_argument("--pretty", action="store_true", help="indent JSON output")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    status = commands.add_parser("status")
    _add_output_options(status)
    health = commands.add_parser("health")
    _add_output_options(health)

    research = commands.add_parser("research")
    research.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS)
    research_commands = research.add_subparsers(dest="research_command", required=True, parser_class=_Parser)
    candidates = research_commands.add_parser("candidates")
    _add_filters(candidates)
    candidates.add_argument("--status")
    signals = research_commands.add_parser("signals")
    _add_filters(signals)
    signal = research_commands.add_parser("signal")
    signal.add_argument("signal_id")
    _add_output_options(signal)
    reports = research_commands.add_parser("reports")
    reports.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    _add_output_options(reports)
    report = research_commands.add_parser("report")
    report.add_argument("report_id")
    _add_output_options(report)
    regime = research_commands.add_parser("regime")
    regime.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    _add_output_options(regime)
    watchlist = research_commands.add_parser("watchlist")
    watchlist.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    _add_output_options(watchlist)
    publish = research_commands.add_parser("publish")
    publish.add_argument("alpha_id")
    _add_output_options(publish)

    bus = commands.add_parser("bus")
    bus.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS)
    bus_commands = bus.add_subparsers(dest="bus_command", required=True, parser_class=_Parser)
    deliveries = bus_commands.add_parser("deliveries")
    deliveries.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    _add_output_options(deliveries)
    delivery = bus_commands.add_parser("delivery")
    delivery.add_argument("delivery_id")
    _add_output_options(delivery)
    receipts = bus_commands.add_parser("receipts")
    receipts.add_argument("delivery_id")
    receipts.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    _add_output_options(receipts)

    service = commands.add_parser("service")
    service.add_argument("action", choices=("start", "stop", "restart", "logs"))
    service.add_argument("name")
    service.add_argument("--confirm", action="store_true")
    service.add_argument("--lines", type=int, default=40)
    _add_output_options(service)
    return parser


def _command_name(args: argparse.Namespace) -> str:
    if args.command == "research":
        return f"research.{args.research_command}"
    if args.command == "bus":
        return f"bus.{args.bus_command}"
    if args.command == "service":
        return f"service.{args.action}"
    return args.command


def _dispatch(args: argparse.Namespace) -> tuple[Any, dict[str, Any], list[str]]:
    if args.command == "status":
        return command_status(args)
    if args.command == "health":
        return command_health(args)
    if args.command == "research":
        handlers = {
            "candidates": command_candidates, "signals": command_signals,
            "signal": command_signal, "reports": command_reports,
            "report": command_report, "regime": command_regime,
            "watchlist": command_watchlist, "publish": command_refused_publish,
        }
        return handlers[args.research_command](args)
    if args.command == "bus":
        handlers = {"deliveries": command_bus_deliveries, "delivery": command_bus_delivery,
                    "receipts": command_bus_receipts}
        return handlers[args.bus_command](args)
    return command_service(args)


def main(argv: list[str] | None = None) -> int:
    command = "unknown"
    pretty = False
    try:
        args = build_parser().parse_args(argv)
        command = _command_name(args)
        pretty = bool(getattr(args, "pretty", False))
        if not _cli_enabled():
            raise CliError("local CLI is disabled by RESEARCH_ANALYST_LOCAL_CLI_ENABLED=false", 4)
        data, provenance, warnings = _dispatch(args)
        envelope = _envelope(command, data, provenance=provenance, warnings=warnings)
        code = 0
    except ArgumentError as error:
        envelope, code = _failure(command, CliError(str(error), 1))
    except CliError as error:
        envelope, code = _failure(command, error)
    except Exception as error:  # keep stdout machine-readable for unexpected failures
        print(f"Unexpected CLI failure: {error}", file=sys.stderr)
        envelope, code = _failure(command, CliError("unexpected CLI failure", 5))
    print(json.dumps(envelope, ensure_ascii=True, indent=2 if pretty else None, separators=None if pretty else (",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
