#!/usr/bin/env python3
"""oxmgr health probe for the research-analyst WebSocket gateway."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HEALTH = Path(os.environ.get(
    "WS_HEALTH_PATH",
    str(ROOT / "data" / "ws_health.json"),
))


def process_running() -> bool:
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="ignore")
        except OSError:
            continue
        if "src/research_analyst/ws_gateway.py" in command:
            return True
    return False


def max_health_age_seconds() -> int:
    try:
        return max(30, int(os.environ.get("WS_HEALTH_MAX_AGE_SECONDS", "30")))
    except ValueError:
        return 30


def latest_health(path: Path | None = None) -> tuple[float, dict] | None:
    path = path or HEALTH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("status") not in {"healthy", "ready"}:
            return None
        recorded_at = datetime.fromisoformat(str(payload["ts"]).replace("Z", "+00:00"))
        if recorded_at.tzinfo is None:
            return None
        last_bar_at = payload.get("last_bar_at")
        if not last_bar_at:
            return None
        last_bar = datetime.fromisoformat(str(last_bar_at).replace("Z", "+00:00"))
        if last_bar.tzinfo is None:
            return None
        if payload.get("active_connections", 0) <= 0:
            return None
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    return recorded_at.astimezone(timezone.utc).timestamp(), payload


def main() -> int:
    if not process_running():
        return 1
    health = latest_health()
    if health is None:
        return 1
    recorded_at, payload = health
    now = time.time()
    if now - recorded_at > max_health_age_seconds():
        return 1
    last_bar = datetime.fromisoformat(str(payload["last_bar_at"]).replace("Z", "+00:00"))
    if now - last_bar.astimezone(timezone.utc).timestamp() > max_health_age_seconds():
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
