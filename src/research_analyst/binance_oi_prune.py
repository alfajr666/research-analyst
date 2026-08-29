"""Hard-prune aged rows in binance_oi.db (ADR-013 / binance-oi-rotation-retention)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import config


def prune_enabled() -> bool:
    return bool(getattr(config, "BINANCE_OI_PRUNE_ENABLED", True))


def _cutoff(days: int, now: datetime) -> datetime:
    return now - timedelta(days=max(1, int(days)))


def prune_binance_oi_db(
    conn: Any,
    *,
    now: datetime | None = None,
    db_path: str | Path | None = None,
) -> dict[str, int]:
    """DELETE aged OI research tables. Returns deleted counts per table key."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    hist_d = int(getattr(config, "BINANCE_OI_WATCHLIST_HISTORY_RETENTION_DAYS", 14))
    obs_d = int(getattr(config, "BINANCE_OI_OBSERVATIONS_RETENTION_DAYS", 30))
    raw_d = int(getattr(config, "BINANCE_OI_RAW_OI_RETENTION_DAYS", 30))
    ev_d = int(getattr(config, "BINANCE_OI_EVENTS_RETENTION_DAYS", 90))
    sc_d = int(getattr(config, "BINANCE_OI_SCANS_RETENTION_DAYS", 30))

    counts: dict[str, int] = {
        "observations": 0,
        "raw_oi": 0,
        "watchlist_hist": 0,
        "events": 0,
        "scans": 0,
        "discord_deliveries": 0,
    }

    specs = [
        (
            "binance_oi_rotation_observations",
            "completed_interval_at",
            _cutoff(obs_d, now),
            "observations",
        ),
        (
            "binance_oi_rotation_raw_oi_history",
            "observed_at",
            _cutoff(raw_d, now),
            "raw_oi",
        ),
        (
            "binance_oi_rotation_watchlist_history",
            "observed_at",
            _cutoff(hist_d, now),
            "watchlist_hist",
        ),
        (
            "binance_oi_rotation_events",
            "observed_at",
            _cutoff(ev_d, now),
            "events",
        ),
        (
            "binance_oi_rotation_scans",
            "completed_interval_at",
            _cutoff(sc_d, now),
            "scans",
        ),
        (
            "discord_oi_deliveries",
            "attempted_at",
            _cutoff(sc_d, now),
            "discord_deliveries",
        ),
    ]

    for table, col, cutoff, key in specs:
        try:
            n = conn.execute(
                f"SELECT count(*) FROM {table} WHERE {col} < ?",
                (cutoff,),
            ).fetchone()[0]
            n = int(n or 0)
            if n > 0:
                conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
            counts[key] = n
        except Exception:
            counts[key] = 0

    try:
        conn.commit()
    except Exception:
        pass

    path = Path(db_path or getattr(config, "BINANCE_OI_DB_PATH", "") or "")
    mb = 0.0
    if path and path.exists():
        try:
            mb = path.stat().st_size / (1024 * 1024)
        except Exception:
            mb = 0.0
    counts["db_mb_x100"] = int(round(mb * 100))
    return counts


def format_prune_log(counts: dict[str, int]) -> str:
    mb = counts.get("db_mb_x100", 0) / 100.0
    return (
        f"[oi-prune] observations=-{counts.get('observations', 0)} "
        f"raw_oi=-{counts.get('raw_oi', 0)} "
        f"watchlist_hist=-{counts.get('watchlist_hist', 0)} "
        f"events=-{counts.get('events', 0)} "
        f"scans=-{counts.get('scans', 0)} "
        f"discord=-{counts.get('discord_deliveries', 0)} "
        f"db_mb={mb:.1f}"
    )


def run_prune_once(now: datetime | None = None) -> dict[str, int] | None:
    """Open RW on OI DB, prune, log. Returns counts or None if disabled."""
    if not prune_enabled():
        return None
    now = now or datetime.now(timezone.utc)
    config.init_binance_oi_db()
    conn = config.get_db_connection(read_only=False, db_path=config.BINANCE_OI_DB_PATH)
    try:
        counts = prune_binance_oi_db(conn, now=now, db_path=config.BINANCE_OI_DB_PATH)
        print(format_prune_log(counts))
        return counts
    finally:
        conn.close()
