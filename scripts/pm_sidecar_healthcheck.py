#!/usr/bin/env python3
"""oxmgr health probe for the independent PM sidecar."""

import os
import time
from pathlib import Path


LOG = Path(os.environ.get(
    "PM_SIDECAR_LOG",
    "/home/ubuntu/.local/share/oxmgr/logs/research-analyst-pm-sidecar.out.log",
))
MAX_LOG_AGE_SECONDS = 180


def main() -> int:
    if not any(
        "src/research_analyst/pm_sidecar.py" in Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="ignore")
        for pid in os.listdir("/proc")
        if pid.isdigit() and Path(f"/proc/{pid}/cmdline").exists()
    ):
        return 1
    if not LOG.exists() or time.time() - LOG.stat().st_mtime > MAX_LOG_AGE_SECONDS:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
