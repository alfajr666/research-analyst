#!/usr/bin/env python3
"""oxmgr health probe for the research-analyst orchestrator."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HEALTH = Path(os.environ.get(
    "ORCHESTRATOR_HEALTH_PATH",
    str(ROOT / "data" / "health.json"),
))
ANALYST_DB = Path(os.environ.get(
    "ANALYST_DB_PATH",
    str(ROOT / "data" / "analyst.sqlite3"),
))
REQUIRED_FIELDS = {"bot", "lastCycleAt", "dataFreshness", "evaluation", "ts"}


def max_health_age_seconds() -> int:
    """Allow one orchestrator cycle plus time for the pipeline to finish."""
    try:
        cadence_minutes = max(5.0, float(os.environ.get(
            "ORCHESTRATOR_CADENCE_MINUTES",
            os.environ.get("INGEST_INTERVAL_MINS", "5"),
        )))
    except ValueError:
        cadence_minutes = 5
    return max(180, int(cadence_minutes * 60) + 120)


def process_running() -> bool:
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="ignore")
        except OSError:
            continue
        if "src/research_analyst/orchestrator.py" in command:
            return True
    return False


def latest_health(path: Path | None = None) -> tuple[float, dict] | None:
    path = path or HEALTH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not REQUIRED_FIELDS <= payload.keys():
            return None
        if payload.get("bot") != "research-analyst":
            return None
        recorded_at = datetime.fromisoformat(
            str(payload["lastCycleAt"]).replace("Z", "+00:00")
        )
        if recorded_at.tzinfo is None:
            return None
        if not isinstance(payload["dataFreshness"], dict):
            return None
        if not isinstance(payload["evaluation"], dict):
            return None
        evaluations = payload["evaluation"].get("strategy_evaluations")
        if not isinstance(evaluations, int) or evaluations < 0:
            return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return recorded_at.astimezone(timezone.utc).timestamp(), payload


def active_pipeline_recent(path: Path | None = None) -> bool:
    """Keep a long-running valid cycle from being killed by a stale health file."""
    path = path or ANALYST_DB
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        try:
            row = connection.execute(
                "SELECT started_at FROM pipeline_runs WHERE status = 'running' "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return False
        started_at = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        if started_at.tzinfo is None:
            return False
        age = time.time() - started_at.astimezone(timezone.utc).timestamp()
        return 0 <= age <= max_health_age_seconds()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return False


def main() -> int:
    if not process_running():
        return 1
    health = latest_health()
    if health is None:
        return 1
    recorded_at, _ = health
    age = time.time() - recorded_at
    if age < 0 or age > max_health_age_seconds():
        return 0 if active_pipeline_recent() else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
