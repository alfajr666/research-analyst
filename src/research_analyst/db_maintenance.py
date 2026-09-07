"""Owned database retention for the research analyst services.

Market pruning must run on the WebSocket gateway's single writer connection.
Analyst pruning runs from the orchestrator and regime pruning from the regime
worker, each on its own database writer.  The Binance OI database is a
separate database owned by its producer and is intentionally not opened here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
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


def _delete(
    conn: Any,
    table: str,
    predicate: str,
    params: tuple[Any, ...],
    *,
    max_batches: int | None = None,
) -> int:
    if not _exists(conn, table):
        return 0
    batch_size = min(5000, max(100, int(getattr(config, "DB_MAINTENANCE_BATCH_SIZE", 5000))))
    yield_seconds = max(0.0, float(getattr(config, "DB_MAINTENANCE_YIELD_SECONDS", 0.01)))
    total = 0
    batches = 0
    while True:
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE rowid IN ("
            f"SELECT rowid FROM {table} WHERE {predicate} LIMIT ?)",
            params + (batch_size,),
        )
        deleted = max(int(cursor.rowcount or 0), 0)
        conn.commit()
        total += deleted
        batches += 1
        if deleted < batch_size or (max_batches is not None and batches >= max_batches):
            break
        if yield_seconds:
            time.sleep(yield_seconds)
    return total


def _limit(now: datetime, days: int) -> datetime:
    return now - timedelta(days=max(0, int(days)))


def prune_market_db(
    conn: Any,
    now: datetime | None = None,
    *,
    max_batches: int | None = None,
) -> dict[str, int]:
    """Prune market-owned rows using the configured per-interval TTLs."""
    now = _utc(now)
    deleted: dict[str, int] = {}
    option_limit = _limit(now, getattr(config, "MARKET_OPTION_RETENTION_DAYS", 30))
    deleted["option_chains"] = _delete(
        conn, "option_chains", "timestamp < ?", (option_limit,), max_batches=max_batches
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
            max_batches=max_batches,
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
                max_batches=max_batches,
            )
        else:
            source_deleted += _delete(
                conn,
                "source_observations",
                "source_end < ?",
                (_limit(now, fallback_days),),
                max_batches=max_batches,
            )
    deleted["source_observations"] = source_deleted

    for table, (column, setting) in {
        "brain_outputs": ("timestamp", "MARKET_AUXILIARY_RETENTION_DAYS"),
        "confluence_alerts": ("alert_time", "MARKET_AUXILIARY_RETENTION_DAYS"),
        "scanner_history": ("timestamp", "MARKET_AUXILIARY_RETENTION_DAYS"),
        "universe_snapshots": ("observed_at", "MARKET_AUXILIARY_RETENTION_DAYS"),
        "source_request_log": ("requested_at", "MARKET_AUXILIARY_RETENTION_DAYS"),
        "broad_discovery_snapshots": ("observed_at", "MARKET_DISCOVERY_RETENTION_DAYS"),
        "discovery_watchlist_history": ("observed_at", "MARKET_WATCHLIST_RETENTION_DAYS"),
    }.items():
        deleted[table] = _delete(
            conn,
            table,
            f"{column} < ?",
            (_limit(now, getattr(config, setting, 30)),),
            max_batches=max_batches,
        )
    deleted["daily_options_summary"] = _delete(
        conn,
        "daily_options_summary",
        "date < ?",
        (_limit(now, getattr(config, "MARKET_DAILY_SUMMARY_RETENTION_DAYS", 365)).date().isoformat(),),
        max_batches=max_batches,
    )
    deleted["regime_signals"] = _delete(
        conn,
        "regime_signals",
        "date < ?",
        (_limit(now, getattr(config, "MARKET_REGIME_RETENTION_DAYS", 365)).date().isoformat(),),
        max_batches=max_batches,
    )
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    return deleted


def prune_analyst_db(
    conn: Any,
    now: datetime | None = None,
    *,
    max_batches: int | None = None,
) -> dict[str, int]:
    """Prune recomputable analyst snapshots and aged terminal audit rows.

    Non-terminal records are retained even when old so a retry or execution
    reconciliation cannot be broken by retention.
    """
    now = _utc(now)
    deleted: dict[str, int] = {}

    # Feature snapshots are point-in-time materializations. Their source bars
    # and strategy ledgers remain available independently of these snapshots.
    cutoff_limit = _limit(now, getattr(config, "ANALYST_SNAPSHOT_RETENTION_DAYS", 7))
    completed_cutoff = (
        "cutoff_at < ? AND status NOT IN ('running', 'pending', 'retry', 'retrying')"
    )
    # Persisted zone rows are a retired compatibility table. Zones are computed
    # in memory from the market/regime bars and are never read from this table.
    deleted["structure_zones"] = _delete(
        conn,
        "structure_zones",
        "1 = 1",
        (),
        max_batches=max_batches,
    )
    deleted["feature_snapshots"] = _delete(
        conn,
        "feature_snapshots",
        "cutoff_id IN (SELECT cutoff_id FROM cutoff_runs WHERE " + completed_cutoff + ")",
        (cutoff_limit,),
        max_batches=max_batches,
    )
    deleted["cutoff_runs"] = _delete(
        conn,
        "cutoff_runs",
        completed_cutoff,
        (_limit(now, getattr(config, "ANALYST_CUTOFF_RETENTION_DAYS", 30)),),
        max_batches=max_batches,
    )

    direct_retention = {
        "pipeline_runs": ("started_at", "ANALYST_PIPELINE_RETENTION_DAYS"),
        "raw_signals": ("created_at", "ANALYST_RAW_SIGNAL_RETENTION_DAYS"),
        "raw_signal_status_history": ("recorded_at", "ANALYST_RAW_SIGNAL_RETENTION_DAYS"),
        "alpha_candidates": ("observed_at", "ANALYST_CANDIDATE_RETENTION_DAYS"),
        "alpha_confidence_observations": ("observed_at", "ANALYST_EVENT_RETENTION_DAYS"),
        "alpha_event_status_history": ("recorded_at", "ANALYST_EVENT_RETENTION_DAYS"),
        "research_run_metrics": ("recorded_at", "ANALYST_METRICS_RETENTION_DAYS"),
        "research_evidence": ("retrieved_at", "ANALYST_RESEARCH_RETENTION_DAYS"),
        "research_artifacts": ("generated_at", "ANALYST_RESEARCH_RETENTION_DAYS"),
        "entry_policy_observations": ("observed_at", "ANALYST_EVENT_RETENTION_DAYS"),
    }
    for table, (column, setting) in direct_retention.items():
        predicate = f"{column} < ?"
        if table == "pipeline_runs":
            predicate += " AND status NOT IN ('running', 'pending', 'retry', 'retrying')"
        deleted[table] = _delete(
            conn,
            table,
            predicate,
            (_limit(now, getattr(config, setting, 30)),),
            max_batches=max_batches,
        )

    member_limit = _limit(now, getattr(config, "ANALYST_RAW_SIGNAL_RETENTION_DAYS", 90)).isoformat()
    member_predicate = (
        "window_start < ? AND (NOT EXISTS (SELECT 1 FROM discord_signal_batches b "
        "WHERE b.window_start = discord_signal_batch_members.window_start) OR "
        "window_start IN (SELECT window_start FROM discord_signal_batches WHERE "
        "status NOT IN ('pending', 'claimed', 'running', 'retry', 'retrying')))"
    )
    batch_limit = _limit(now, getattr(config, "ANALYST_METRICS_RETENTION_DAYS", 30)).isoformat()
    batch_predicate = (
        "window_start < ? AND status NOT IN "
        "('pending', 'claimed', 'running', 'retry', 'retrying')"
    )
    deleted["discord_signal_batch_members"] = _delete(
        conn,
        "discord_signal_batch_members",
        member_predicate,
        (member_limit,),
        max_batches=max_batches,
    )
    deleted["discord_signal_batches"] = _delete(
        conn,
        "discord_signal_batches",
        batch_predicate,
        (batch_limit,),
        max_batches=max_batches,
    )

    # Keep active/pending work and only remove completed research requests.
    deleted["research_requests"] = _delete(
        conn,
        "research_requests",
        "created_at < ? AND status NOT IN ('pending', 'running', 'in_progress', 'retry')",
        (_limit(now, getattr(config, "ANALYST_RESEARCH_RETENTION_DAYS", 30)),),
        max_batches=max_batches,
    )
    deleted["alpha_events"] = _delete(
        conn,
        "alpha_events",
        "observed_at < ? AND status NOT IN ('active', 'pending', 'running', 'retry')",
        (_limit(now, getattr(config, "ANALYST_EVENT_RETENTION_DAYS", 365)),),
        max_batches=max_batches,
    )
    deleted["signal_deliveries"] = _delete(
        conn,
        "signal_deliveries",
        "attempted_at < ? AND status NOT IN ('pending', 'running', 'retry', 'retrying')",
        (_limit(now, getattr(config, "ANALYST_DELIVERY_RETENTION_DAYS", 365)),),
        max_batches=max_batches,
    )
    deleted["execution_deliveries"] = _delete(
        conn,
        "execution_deliveries",
        "(written_at < ? OR (written_at IS NULL AND acknowledged_at < ?)) "
        "AND status NOT IN ('pending', 'running', 'retry', 'retrying')",
        (
            _limit(now, getattr(config, "ANALYST_DELIVERY_RETENTION_DAYS", 365)),
            _limit(now, getattr(config, "ANALYST_DELIVERY_RETENTION_DAYS", 365)),
        ),
        max_batches=max_batches,
    )
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    return deleted


def prune_regime_db(
    conn: Any,
    now: datetime | None = None,
    *,
    max_batches: int | None = None,
) -> dict[str, int]:
    """Prune regime observations and direct history on the regime writer."""
    now = _utc(now)
    deleted: dict[str, int] = {}
    direct_retention = {
        "regime_scores": ("cutoff_at", "REGIME_SCORE_RETENTION_DAYS"),
        "regime_gate_decisions": ("cutoff_at", "REGIME_GATE_RETENTION_DAYS"),
        "regime_1h_bars": ("source_end", "DIRECT_HTF_1H_RETAIN_DAYS"),
        "regime_4h_bars": ("source_end", "DIRECT_HTF_4H_RETAIN_DAYS"),
    }
    for table, (column, setting) in direct_retention.items():
        deleted[table] = _delete(
            conn,
            table,
            f"{column} < ?",
            (_limit(now, getattr(config, setting, 30)),),
            max_batches=max_batches,
        )
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    return deleted
