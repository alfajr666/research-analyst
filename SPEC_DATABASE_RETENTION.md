# Database Retention and Offline Compaction

## Policy

The research analyst has three independently owned SQLite stores:

| Store | Owner | Online retention |
| --- | --- | --- |
| `market.sqlite3` | `ws_gateway` | Tiered market and auxiliary TTLs |
| `analyst.sqlite3` | `orchestrator` | Snapshot and terminal-audit TTLs |
| `regime.sqlite3` | `regime-session` | Score, gate, and direct-history TTLs |

The policy follows the option-scanner retention contract while respecting the
analyst's three database ownership boundaries. Operational intent, delivery,
position, fill, and outcome records are not removed merely to reduce file size.
Recomputable market snapshots and analyst materializations may have explicit
age-based retention; LLM, regime, and research audit records use longer,
configurable windows.

Online cleanup runs every six hours by default. Each delete statement removes at
most 5,000 rows, commits immediately, yields between batches, and uses a passive
WAL checkpoint. A pass may drain an existing backlog through multiple short
batches. Active, pending, running, and retryable work is preserved.

Default windows are:

| Store/table family | Retention |
| --- | ---: |
| Market option-chain snapshots | 3 days |
| Market 1m / 5m / 15m bars | 7 / 30 / 90 days |
| Market 1h / 4h bars | 365 days |
| Market discovery / watchlist history | 90 / 365 days |
| Analyst feature snapshots | 2 days |
| Raw signals and status history | 90 days |
| Candidate ledger | 90 days |
| Alpha, PM, delivery, and research audit data | 30-365 days by table |
| Regime scores / gate decisions | 30 / 90 days |
| Direct regime 1h / 4h seed history | 3 / 14 days minimum |

The legacy analyst `structure_zones` table is retained in the schema for
compatibility but is no longer written or read. Retention drains any remaining
rows so weekly compaction can reclaim their pages.

The Binance OI database belongs to the separate `binance-scanner-oi` project and
is never opened or maintained here.

## Offline Compaction

SQLite deletes free pages for reuse but do not reliably shrink the database file.
`scripts/compact_databases.sh` runs from the installed weekly low-activity cron
schedule, `30 4 * * 0` (Sunday 04:30 UTC). It uses a process lock, verifies
free disk space, stops and verifies all registered research-analyst services,
optionally backs up each owned database when `DB_COMPACTION_BACKUP=true`,
deletes rows outside the retention windows, runs
`wal_checkpoint(TRUNCATE)`, `VACUUM`, and `PRAGMA optimize`, verifies
`integrity_check`, keeps the two newest backups per database, and restores only
the services that were active before compaction.

The job is fail-closed: missing databases, missing required managed services,
insufficient free space, or a service that will not stop prevent database
changes. Optional backups are not required for normal compaction because old
raw history is intentionally discarded.

## Provenance Limits

Retention controls age, not row width. High-frequency snapshots and repeated
provenance lists must remain bounded at write time. Recomputable zone context is
kept in memory rather than materialized as one row per zone, and regime score
rows should not duplicate an entire long lookback of source IDs when a smaller
audit reference is sufficient. Regime score provenance is capped at 128 source
IDs per persisted reference by default (`REGIME_PROVENANCE_MAX_IDS`).
