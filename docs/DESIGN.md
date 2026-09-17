# Research Analyst Design

`AGENTS.md` is the authoritative runtime and safety contract. This document is
a compact map of the active implementation.

## Runtime boundary

```text
Bybit public market data
  -> symbol rotation + WebSocket gateway
  -> market.sqlite3

completed 5m cutoff
  -> regime/session worker -> regime.sqlite3
  -> orchestrator + strategy runner
  -> raw candidate ledger
  -> deterministic admission and clash resolution
  -> alpha outbox + analyst.sqlite3
  -> TradeIntent schema v2
  -> shared SQLite intent bus
  -> venue executors
```

Research Analyst stops at shared-bus publication. It has no venue adapter,
venue inbox writer, exchange credentials, order client, sizing logic, fill
state, or position-management loop.

## Notifications

The only notification surface is the advisory raw-signal Discord batch. It is
not an intent handoff and never reports fills, positions, or execution state.

## Deliberate exclusions

- Analyst-local LLM research and selection.
- Telegram or per-alpha Discord trade cards.
- Filesystem delivery to venue-specific inboxes.
- Direct calls to venue executors.
- Online database compaction.

Historical tables from retired paths may remain in an existing SQLite file,
but current schema initialization, runtime code, retention, and CLI surfaces do
not create or use them. Destructive database removal requires a separate
offline migration.

See `specs/repository-cleanup-v1.md` for the retirement record and phased
cleanup decisions.
