"""Independent PM2 worker for completed-hour + 10m-liquid Binance OI rotation scans."""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

import config
from binance_oi_prune import run_prune_once
from binance_oi_rotation_scanner import SOURCE, completed_bar, completed_hour, run_scanner

# Prune at most once per hour (ADR-013 fail-open retention).
_LAST_PRUNE_MONO = 0.0
_PRUNE_INTERVAL_SEC = 3600


def _scan_exists(conn, interval: datetime, bar_minutes: int) -> bool:
    row = conn.execute("""
        SELECT 1 FROM binance_oi_rotation_scans
        WHERE source = ? AND completed_interval_at = ? AND scanner_version = ?
          AND bar_minutes = ? AND status = 'complete'
        LIMIT 1
    """, (SOURCE, interval, config.BINANCE_OI_ROTATION_SCANNER_VERSION, bar_minutes)).fetchone()
    return bool(row)


def _maybe_prune(*, force: bool = False) -> None:
    """Hourly hard-prune of aged OI tables. Fail-open."""
    global _LAST_PRUNE_MONO
    now_m = time.monotonic()
    if (
        _LAST_PRUNE_MONO > 0
        and not force
        and (now_m - _LAST_PRUNE_MONO) < _PRUNE_INTERVAL_SEC
    ):
        return
    try:
        run_prune_once()
        _LAST_PRUNE_MONO = now_m
    except Exception as exc:
        print(f"[oi-prune] error (fail-open): {exc}")
        _LAST_PRUNE_MONO = now_m


def run_due_scan(now: datetime | None = None) -> bool:
    """Run 1h (full) and/or 10m/15m (liquid) when their respective completed bar is unscanned."""
    ran = False
    any_enabled = config.BINANCE_OI_ROTATION_ENABLED or getattr(config, "BINANCE_OI_10M_ENABLED", False)
    if any_enabled:
        config.init_binance_oi_db()

    # 1h full (existing authority). Close RO before run_scanner opens RW —
    # DuckDB forbids mixed configs on the same file in one process.
    if config.BINANCE_OI_ROTATION_ENABLED:
        interval = completed_hour(now)
        conn = config.get_db_connection(read_only=True, db_path=config.BINANCE_OI_DB_PATH)
        try:
            need_1h = not _scan_exists(conn, interval, 60)
        finally:
            conn.close()
        if need_1h:
            feed = run_scanner(now=now, bar_minutes=60)
            print(
                f"Binance OI 1h feed published for {interval.isoformat()} "
                f"with {len(feed.get('candidates', []))} candidates."
            )
            ran = True

    # Fast liquid path
    if getattr(config, "BINANCE_OI_10M_ENABLED", False):
        bm = getattr(config, "BINANCE_OI_10M_BAR_MINUTES", 15)
        interval = completed_bar(now, bar_minutes=bm)
        conn = config.get_db_connection(read_only=True, db_path=config.BINANCE_OI_DB_PATH)
        try:
            need_short = not _scan_exists(conn, interval, bm)
        finally:
            conn.close()
        if need_short:
            feed = run_scanner(now=now, bar_minutes=bm)
            print(
                f"Binance OI {bm}m liquid feed published for {interval.isoformat()} "
                f"with {len(feed.get('candidates', []))} candidates."
            )
            ran = True

    # Retention prune when OI DB is in use (not when scanner fully disabled)
    if any_enabled:
        _maybe_prune(force=ran)
    return ran


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Binance OI rotation scans independently of legacy ingestion."
    )
    parser.add_argument(
        "--once", action="store_true", help="Check the current completed hour once and exit."
    )
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
