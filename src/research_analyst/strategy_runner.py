"""Process boundary for cutoff-bound strategy evaluation.

The runner is deliberately a read-only strategy stage. Scoring, admission,
clash resolution, persistence, and publishing remain in the orchestrator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import config
from strategy_plugins import (
    StrategyPlugin,
    evaluate_strategy_plugins,
    load_active_plugins,
    load_enabled_plugins,
)


PROTOCOL_VERSION = 1


def _utc_timestamp(value: datetime | str) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def json_safe(value: Any) -> Any:
    """Convert process-bound values to the JSON-only runner contract."""
    if isinstance(value, datetime):
        return _utc_timestamp(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("strategy runner payload contains a non-finite number")
    return value


def _strategy_config_fingerprint() -> str:
    relevant = {
        key: json_safe(value)
        for key, value in vars(config).items()
        if key.startswith("STRATEGY_") or key.endswith("_V1") or key.endswith("_V2")
    }
    encoded = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _plugin_manifest_entry(plugin: StrategyPlugin) -> dict[str, Any]:
    return json_safe({
        "strategy_id": plugin.id,
        "plugin_version": plugin.version,
        "cadence": plugin.cadence,
        "market_family": plugin.market_family,
        "required_datasets": plugin.required_datasets,
        "optional_datasets": plugin.optional_datasets,
        "required_intervals": plugin.required_intervals,
        "feature_requirements": plugin.feature_requirements,
        "lookback_days": plugin.lookback_days,
        "stateful": plugin.stateful,
    })


def build_strategy_manifest(db_path: str | Path | None = None) -> dict[str, Any]:
    """Freeze the active strategy set for one cutoff request."""
    path = db_path or config.ANALYST_DB_PATH
    conn = config.get_db_connection(read_only=True, db_path=path)
    try:
        plugins = load_active_plugins(conn)
    finally:
        conn.close()
    entries = [_plugin_manifest_entry(plugin) for plugin in plugins]
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "configuration_fingerprint": _strategy_config_fingerprint(),
        "strategies": entries,
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return {**body, "manifest_id": hashlib.sha256(encoded.encode("utf-8")).hexdigest()}


def _plugins_for_manifest(manifest: dict[str, Any]) -> list[StrategyPlugin]:
    if manifest.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("unsupported strategy manifest protocol version")
    if manifest.get("configuration_fingerprint") != _strategy_config_fingerprint():
        raise ValueError("strategy configuration fingerprint mismatch")
    entries = manifest.get("strategies")
    if not isinstance(entries, list):
        raise ValueError("strategy manifest strategies must be a list")
    manifest_body = {
        "protocol_version": manifest.get("protocol_version"),
        "configuration_fingerprint": manifest.get("configuration_fingerprint"),
        "strategies": entries,
    }
    encoded = json.dumps(manifest_body, sort_keys=True, separators=(",", ":"))
    expected_manifest_id = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    if manifest.get("manifest_id") != expected_manifest_id:
        raise ValueError("strategy manifest identity mismatch")
    known = {plugin.id: plugin for plugin in load_enabled_plugins()}
    plugins = []
    seen: set[str] = set()
    for entry in manifest.get("strategies", []):
        if not isinstance(entry, dict):
            raise ValueError("strategy manifest entry must be an object")
        strategy_id = str(entry.get("strategy_id") or "")
        if not strategy_id or strategy_id in seen:
            raise ValueError(f"duplicate or empty strategy in manifest: {strategy_id}")
        seen.add(strategy_id)
        plugin = known.get(strategy_id)
        if plugin is None:
            raise ValueError(f"strategy is not enabled in runner: {strategy_id}")
        if entry != _plugin_manifest_entry(plugin):
            raise ValueError(f"strategy version mismatch: {strategy_id}")
        plugins.append(plugin)
    return plugins


def make_request(
    cutoff_id: str,
    cutoff_at: datetime,
    eval_interval: str,
    effective_universe: dict,
    regime_scope: dict,
    *,
    db_path: str | Path | None = None,
    market_db_path: str | Path | None = None,
    now: datetime | None = None,
    feature_snapshots: dict | None = None,
) -> dict[str, Any]:
    manifest = build_strategy_manifest(db_path)
    request_now = now or datetime.now(timezone.utc)
    timeout_seconds = float(getattr(config, "STRATEGY_RUNNER_TIMEOUT_SECONDS", 120))
    return json_safe({
        "protocol_version": PROTOCOL_VERSION,
        "request_id": f"{cutoff_id}:{manifest['manifest_id']}",
        "cutoff_id": cutoff_id,
        "cutoff_at": cutoff_at,
        "eval_interval": eval_interval,
        "effective_universe": effective_universe,
        "regime_scope": regime_scope,
        "strategy_manifest": manifest,
        "analyst_db_path": str(db_path or config.ANALYST_DB_PATH),
        "market_db_path": str(market_db_path or config.MARKET_DB_PATH),
        "now": request_now,
        "deadline_at": datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds),
        "feature_snapshots": feature_snapshots or {},
    })


def evaluate_request(request: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one request in the runner process without durable writes."""
    if request.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("unsupported strategy runner protocol version")
    cutoff_at = datetime.fromisoformat(str(request["cutoff_at"]).replace("Z", "+00:00"))
    if cutoff_at.tzinfo is None:
        cutoff_at = cutoff_at.replace(tzinfo=timezone.utc)
    cutoff_at = cutoff_at.astimezone(timezone.utc)
    cutoff_id = str(request.get("cutoff_id") or "")
    if ":" not in cutoff_id:
        raise ValueError("cutoff_id must include its interval and timestamp")
    cutoff_from_id = datetime.fromisoformat(cutoff_id.split(":", 1)[1].replace("Z", "+00:00"))
    if cutoff_from_id.tzinfo is None:
        cutoff_from_id = cutoff_from_id.replace(tzinfo=timezone.utc)
    if cutoff_from_id.astimezone(timezone.utc) != cutoff_at:
        raise ValueError("request cutoff_at does not match cutoff_id")
    deadline_at = request.get("deadline_at")
    if deadline_at is not None:
        deadline = datetime.fromisoformat(str(deadline_at).replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline.astimezone(timezone.utc) < datetime.now(timezone.utc):
            raise TimeoutError("strategy runner request deadline expired")
    manifest = request.get("strategy_manifest") or {}
    plugins = _plugins_for_manifest(manifest)
    now = request.get("now")
    if now is not None:
        now = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
    snapshot = {
        "cutoff_id": cutoff_id,
        "cutoff_at": cutoff_at,
        "eval_interval": request["eval_interval"],
        "now": now,
        "market_db_path": request.get("market_db_path") or config.MARKET_DB_PATH,
        "effective_universe": request.get("effective_universe") or {},
        "regime_scope": request.get("regime_scope") or {"mode": "off"},
        "feature_snapshots": request.get("feature_snapshots") or {},
    }
    started = time.monotonic()
    result = evaluate_strategy_plugins(
        request.get("analyst_db_path") or config.ANALYST_DB_PATH,
        cutoff_id,
        now=now,
        require_finalized=True,
        snapshot=snapshot,
        market_db_path=request.get("market_db_path") or config.MARKET_DB_PATH,
        plugins=plugins,
    )
    result["_strategy_manifest"] = manifest
    result["_runner_request_id"] = request.get("request_id")
    result["_runner_cutoff_id"] = cutoff_id
    result["_runner_cutoff_at"] = cutoff_at
    result["_runner_status"] = "completed"
    result["_runner_duration_ms"] = round((time.monotonic() - started) * 1000, 3)
    result["_runner_version"] = PROTOCOL_VERSION
    return json_safe(result)


def request_strategy_evaluation(
    request: dict[str, Any],
    *,
    socket_path: str | Path | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Call the managed runner and return its complete strategy result."""
    path = str(socket_path or config.STRATEGY_RUNNER_SOCKET)
    timeout = config.STRATEGY_RUNNER_TIMEOUT_SECONDS if timeout is None else timeout
    payload = json.dumps(json_safe(request), sort_keys=True, separators=(",", ":")) + "\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(path)
        client.sendall(payload.encode("utf-8"))
        with client.makefile("rb") as stream:
            line = stream.readline()
    if not line:
        raise RuntimeError("strategy runner closed connection without a response")
    response = json.loads(line.decode("utf-8"))
    if response.get("protocol_version") != PROTOCOL_VERSION:
        raise RuntimeError("unsupported strategy runner response version")
    if response.get("request_id") != request.get("request_id"):
        raise RuntimeError("strategy runner response request ID mismatch")
    if response.get("ok") is not True:
        raise RuntimeError(str(response.get("error") or "strategy runner request failed"))
    result = response["result"]
    if result.get("_runner_cutoff_id") != request.get("cutoff_id"):
        raise RuntimeError("strategy runner response cutoff ID mismatch")
    manifest = result.get("_strategy_manifest") or {}
    if manifest.get("manifest_id") != (request.get("strategy_manifest") or {}).get("manifest_id"):
        raise RuntimeError("strategy runner response manifest mismatch")
    return result


class StrategyRunnerServer:
    def __init__(self, socket_path: str | Path | None = None):
        self.socket_path = Path(socket_path or config.STRATEGY_RUNNER_SOCKET)
        self._server: socket.socket | None = None
        self._stopped = threading.Event()
        self._evaluation_lock = threading.Lock()

    def _handle_connection(self, connection: socket.socket) -> None:
        with connection:
            request: dict[str, Any] | None = None
            try:
                with connection.makefile("rb") as stream:
                    line = stream.readline()
                if not line:
                    return
                request = json.loads(line.decode("utf-8"))
                if request.get("type") == "health":
                    response = {
                        "ok": True,
                        "protocol_version": PROTOCOL_VERSION,
                        "request_id": request.get("request_id"),
                        "result": {"status": "ready", "runner_version": PROTOCOL_VERSION},
                    }
                else:
                    with self._evaluation_lock:
                        result = evaluate_request(request)
                    response = {
                        "ok": True,
                        "protocol_version": PROTOCOL_VERSION,
                        "request_id": request.get("request_id"),
                        "cutoff_id": request.get("cutoff_id"),
                        "manifest_id": (request.get("strategy_manifest") or {}).get("manifest_id"),
                        "result": result,
                    }
            except Exception as exc:  # one request must not kill the runner
                response = {
                    "ok": False,
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": request.get("request_id") if request else None,
                    "error": str(exc)[:500],
                }
            try:
                connection.sendall(
                    (json.dumps(json_safe(response), sort_keys=True) + "\n").encode("utf-8")
                )
            except OSError:
                pass

    def serve_forever(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.2)
                probe.connect(str(self.socket_path))
            except OSError:
                self.socket_path.unlink()
            else:
                raise RuntimeError(f"strategy runner already listening: {self.socket_path}")
            finally:
                probe.close()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            self._server = server
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            server.listen(1)
            server.settimeout(0.5)
            print(f"Strategy runner ready: {self.socket_path}", flush=True)
            while not self._stopped.is_set():
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stopped.is_set():
                        break
                    raise
                threading.Thread(
                    target=self._handle_connection,
                    args=(connection,),
                    daemon=True,
                ).start()

    def close(self) -> None:
        self._stopped.set()
        if self._server is not None:
            self._server.close()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Research Analyst strategy runner")
    parser.add_argument("--socket", default=config.STRATEGY_RUNNER_SOCKET)
    args = parser.parse_args()
    StrategyRunnerServer(args.socket).serve_forever()


if __name__ == "__main__":
    main()
