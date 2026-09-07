#!/usr/bin/env python3
"""oxmgr health probe for the symbol-rotation worker."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "research_analyst"))

from symbol_rotation import _feed_valid, effective_state_at, read_feed


FEED = Path(os.environ.get(
    "SYMBOL_ROTATION_FEED_PATH",
    str(ROOT / "data" / "symbol_rotation_feed.json"),
))


def process_running() -> bool:
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="ignore")
        except OSError:
            continue
        if "src/research_analyst/symbol_rotation.py" in command:
            return True
    return False


def feed_ready(now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc)
    feed = read_feed(FEED, at=current, allow_expired=True)
    if not isinstance(feed, dict):
        return False
    if feed.get("schema_version") == 1:
        return _feed_valid(feed, current) and feed.get("status") in {"ready", "fallback"}
    _, metadata = effective_state_at(feed, current)
    status = metadata.get("status")
    if status in {"ready", "degraded"}:
        return True
    if status == "permanent_fallback":
        return _feed_valid(feed, current)
    return False


def main() -> int:
    if not process_running() or not feed_ready():
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
