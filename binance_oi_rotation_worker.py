"""Independent PM2 worker for completed-hour Binance OI rotation scans."""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

import config
from binance_oi_rotation_scanner import SOURCE, completed_hour, run_scanner


def run_due_scan(now: datetime | None = None) -> bool:
    """Run only when the latest completed hour has no successful scan record."""
    interval = completed_hour(now)
    conn = config.get_db_connection(read_only=True)
    try:
        complete = conn.execute("""
            SELECT 1 FROM binance_oi_rotation_scans
            WHERE source = ? AND completed_interval_at = ? AND scanner_version = ?
              AND status = 'complete'
            LIMIT 1
        """, (SOURCE, interval, config.BINANCE_OI_ROTATION_SCANNER_VERSION)).fetchone()
    finally:
        conn.close()
    if complete:
        return False
    feed = run_scanner(now=now)
    print(f"Binance OI rotation feed published for {interval.isoformat()} with {len(feed['candidates'])} candidates.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Binance OI rotation scans independently of legacy ingestion.")
    parser.add_argument("--once", action="store_true", help="Check the current completed hour once and exit.")
    args = parser.parse_args()
    if args.once:
        run_due_scan()
        return
    while True:
        try:
            run_due_scan()
        except Exception as exc:  # Keep PM2 worker alive for the next retry.
            print(f"Binance OI rotation worker error: {exc}")
        time.sleep(60)


if __name__ == "__main__":
    main()
