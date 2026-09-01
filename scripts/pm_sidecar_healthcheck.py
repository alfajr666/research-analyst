#!/usr/bin/env python3
"""oxmgr health probe for the independent PM sidecar."""

import os
import time
from pathlib import Path


LOG = Path(os.environ.get(
    "PM_SIDECAR_LOG",
    "/home/ubuntu/.local/share/oxmgr/logs/research-analyst-pm-sidecar.out.log",
))


def max_log_age_seconds() -> int:
    """Allow one cadence interval plus time for a PM cycle to finish."""
    try:
        cadence_minutes = max(5.0, float(os.environ.get("PM_CADENCE_MINUTES", "5")))
    except ValueError:
        cadence_minutes = 5
    return max(180, int(cadence_minutes * 60) + 120)


def main() -> int:
    if not any(
        "src/research_analyst/pm_sidecar.py" in Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="ignore")
        for pid in os.listdir("/proc")
        if pid.isdigit() and Path(f"/proc/{pid}/cmdline").exists()
    ):
        return 1
    if not LOG.exists() or time.time() - LOG.stat().st_mtime > max_log_age_seconds():
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
