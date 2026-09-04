"""Owned database retention for the research analyst services.

Market pruning must run on the WebSocket gateway's single writer connection.
Analyst pruning may run from the orchestrator because that database is already
WAL-enabled and uses short transactions.  The Binance OI database is a
separate DuckDB owned by its producer and is intentionally not opened here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import config


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _exists(conn: Any, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def _delete(conn: Any, table: str, predicate: str, params: tuple[Any, ...]) -> int:
    if not _exists(conn, table):
        return 0
    return int(conn.execute(f"DELETE FROM {table} WHERE {predicate}", params).rowcount or 0)


def _limit(now: datetime, days: int) -> datetime:
    return now - timedelta(days=max(0, int(days)))


def prune_market_db(conn: Any, now: datetime | None = None) -> dict[str, int]:
    """Prune market-owned rows using the configured per-interval TTLs."""
    now = _utc(now)
    deleted: dict[str, int] = {}
    option_limit = _limit(now, getattr(config, "MARKET_OPTION_RETENTION_DAYS", 30))
    deleted["option_chains"] = _delete(
        conn, "option_chains", "timestamp < ?", (option_limit,)
    )

    source_deleted = 0
    tiers = getattr(config, "PRUNE_INTERVAL_DAYS", {})
    for interval, days in tiers.items():
        if int(days) <= 0:
            continue
        source_deleted += _delete(
            conn,
            "source_observations",
            "interval = ? AND source_end < ?",
            (interval, _limit(now, days)),
        )
    fallback_days = int(getattr(config, "FUTURES_RETENTION_DAYS", 365))
    if fallback_days > 0:
        if tiers:
            placeholders = ",".join("?" for _ in tiers)
            source_deleted += _delete(
                conn,
                "source_observations",
                f"interval NOT IN ({placeholders}) AND source_end < ?",
                tuple(tiers.keys()) + (_limit(now, fallback_days),),
            )
        else:
            source_deleted += _delete(
                conn, "source_observations", "source_end < ?", (_limit(now, fallback_days),)
            )
    deleted["source_observations"] = source_deleted

    auxiliary_limit = _limit(now, getattr(config, "MARKET_AUXILIARY_RETENTION_DAYS", 30))
    for table, column in {
        "brain_outputs": "timestamp",
        "confluence_alerts": "alert_time",
        "scanner_history": "timestamp",
        "universe_snapshots": "observed_at",
        "source_request_log": "requested_at",
    }.items():
        deleted[table] = _delete(conn, table, f"{column} < ?", (auxiliary_limit,))
    deleted["daily_options_summary"] = _delete(
        conn,
        "daily_options_summary",
        "date < ?",
        (_limit(now, getattr(config, "MARKET_DAILY_SUMMARY_RETENTION_DAYS", 365)).date().isoformat(),),
    )
    conn.commit()
    return deleted


def prune_analyst_db(conn: Any, now: datetime | None = None) -> dict[str, int]:
    """Prune recomputable analyst snapshots and aged terminal audit rows.

    Non-terminal records are retained even when old so a retry or execution
    reconciliation cannot be broken by retention.
    """
    now = _utc(now)
    deleted: dict[str, int] = {}

    # These two tables are point-in-time materializations.  Their source bars
    # and strategy ledgers remain available independently of these snapshots.
    cutoff_limit = _limit(now, getattr(config, "ANALYST_SNAPSHOT_RETENTION_DAYS", 7))
    deleted["structure_zones"] = _delete(
        conn,
        "structure_zones",
        "cutoff_id IN (SELECT cutoff_id FROM cutoff_runs WHERE cutoff_at < ?)",
        (cutoff_limit,),
    )
    deleted["feature_snapshots"] = _delete(
        conn,
        "feature_snapshots",
        "cutoff_id IN (SELECT cutoff_id FROM cutoff_runs WHERE cutoff_at < ?)",
        (cutoff_limit,),
    )
    deleted["cutoff_runs"] = _delete(
        conn, "cutoff_runs", "cutoff_at < ?", (_limit(now, getattr(config, "ANALYST_CUTOFF_RETENTION_DAYS", 30)),)
    )

    direct_retention = {
        "pipeline_runs": ("started_at", "ANALYST_PIPELINE_RETENTION_DAYS"),
        "raw_signals": ("created_at", "ANALYST_RAW_SIGNAL_RETENTION_DAYS"),
        "raw_signal_status_history": ("recorded_at", "ANALYST_RAW_SIGNAL_RETENTION_DAYS"),
        "alpha_candidates": ("observed_at", "ANALYST_CANDIDATE_RETENTION_DAYS"),
        "alpha_confidence_observations": ("observed_at", "ANALYST_EVENT_RETENTION_DAYS"),
        "alpha_event_status_history": ("recorded_at", "ANALYST_EVENT_RETENTION_DAYS"),
        "pm_advice": ("cutoff_at", "ANALYST_PM_RETENTION_DAYS"),
        "research_run_metrics": ("recorded_at", "ANALYST_METRICS_RETENTION_DAYS"),
        "discord_signal_batches": ("window_start", "ANALYST_METRICS_RETENTION_DAYS"),
        "research_evidence": ("retrieved_at", "ANALYST_RESEARCH_RETENTION_DAYS"),
        "research_artifacts": ("generated_at", "ANALYST_RESEARCH_RETENTION_DAYS"),
        "entry_policy_observations": ("observed_at", "ANALYST_EVENT_RETENTION_DAYS"),
        "broad_discovery_snapshots": ("observed_at", "ANALYST_DISCOVERY_RETENTION_DAYS"),
        "discovery_watchlist_history": ("observed_at", "ANALYST_WATCHLIST_RETENTION_DAYS"),
    }
    for table, (column, setting) in direct_retention.items():
        deleted[table] = _delete(
            conn, table, f"{column} < ?", (_limit(now, getattr(config, setting, 30)),)
        )

    # Keep active/pending work and only remove completed research requests.
    deleted["research_requests"] = _delete(
        conn,
        "research_requests",
        "created_at < ? AND status NOT IN ('pending', 'running', 'in_progress', 'retry')",
        (_limit(now, getattr(config, "ANALYST_RESEARCH_RETENTION_DAYS", 30)),),
    )
    deleted["alpha_events"] = _delete(
        conn,
        "alpha_events",
        "observed_at < ? AND status NOT IN ('active', 'pending', 'running', 'retry')",
        (_limit(now, getattr(config, "ANALYST_EVENT_RETENTION_DAYS", 365)),),
    )
    deleted["signal_deliveries"] = _delete(
        conn,
        "signal_deliveries",
        "attempted_at < ? AND status NOT IN ('pending', 'running', 'retry', 'retrying')",
        (_limit(now, getattr(config, "ANALYST_DELIVERY_RETENTION_DAYS", 365)),),
    )
    deleted["execution_deliveries"] = _delete(
        conn,
        "execution_deliveries",
        "written_at < ? AND status NOT IN ('pending', 'running', 'retry', 'retrying')",
        (_limit(now, getattr(config, "ANALYST_DELIVERY_RETENTION_DAYS", 365)),),
    )
    deleted["deep_backfill_jobs"] = _delete(
        conn,
        "deep_backfill_jobs",
        "updated_at < ? AND status NOT IN ('pending', 'running')",
        (_limit(now, getattr(config, "ANALYST_DISCOVERY_RETENTION_DAYS", 90)),),
    )

    conn.commit()
    return deleted


def vacuum_sqlite(conn: Any) -> None:
    """Checkpoint and compact an owned SQLite database after retention."""
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    conn.execute("VACUUM")
