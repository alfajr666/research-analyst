#!/usr/bin/env python3
"""oxmgr health probe for the regime-session worker."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


LOG = Path(os.environ.get(
    "REGIME_SESSION_LOG",
    "/home/ubuntu/.local/share/oxmgr/logs/research-analyst-regime-session.out.log",
))
REQUIRED_FIELDS = {
    "assets",
    "cutoff_at",
    "history_1h_ready",
    "history_4h_ready",
    "score_ready",
    "gate_allow",
    "gate_block",
    "mode",
}


def max_log_age_seconds() -> int:
    """Allow one completed 5m cycle plus time for the worker to finish."""
    try:
        cadence_minutes = max(5.0, float(os.environ.get("REGIME_SESSION_CADENCE_MINUTES", "5")))
    except ValueError:
        cadence_minutes = 5
    return max(180, int(cadence_minutes * 60) + 120)


def process_running() -> bool:
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        cmdline = Path(f"/proc/{pid}/cmdline")
        try:
            command = cmdline.read_bytes().decode(errors="ignore")
        except OSError:
            continue
        if "src/research_analyst/regime_session.py" in command:
            return True
    return False


def latest_cycle(log_path: Path | None = None) -> tuple[float, dict] | None:
    log_path = log_path or LOG
    if not log_path.exists():
        return None
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if len(line) < 22 or line[19:21] != ": ":
            continue
        try:
            recorded_at = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            payload = json.loads(line[21:])
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and REQUIRED_FIELDS <= payload.keys():
            return recorded_at.timestamp(), payload
    return None


def main() -> int:
    if not process_running():
        return 1
    cycle = latest_cycle()
    if cycle is None:
        return 1
    recorded_at, payload = cycle
    if time.time() - recorded_at > max_log_age_seconds():
        return 1
    try:
        datetime.fromisoformat(str(payload["cutoff_at"]).replace("Z", "+00:00"))
        assets = int(payload["assets"])
        if assets <= 0 or payload["mode"] not in {"off", "shadow", "enforce"}:
            return 1
    except (TypeError, ValueError, OverflowError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
