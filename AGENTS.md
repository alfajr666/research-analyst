# Agent Notes

**Last reviewed:** 2026-08-30

## Runtime Contract

- The WebSocket gateway owns `data/market.sqlite3` and publishes completed 5m
  evaluation triggers.
- The orchestrator consumes one trigger per completed 5m cutoff and writes
  selected intents to the shared executor inbox.
- The PM sidecar is a separate managed process from the orchestrator. It reads
  executor 1m snapshots and evaluates LLM/mechanical management every five minutes,
  writing PM decisions to the executor's shared decision inbox.
- Shared handoff paths are anchored to `BYBIT_EXECUTOR_DIR`: `data/intents`,
  `data/position-snapshots`, and `data/position-decisions` are the same paths
  used by the executor. Do not create analyst-local copies.
- Market freshness is a hard admission gate. Missing data or data older than
  `DATA_FRESHNESS_MAX_SECONDS` (default 600) rejects the candidate.
- Health freshness queries must exclude open/future bars with `source_end <= now`.
- The scorer ranks candidates that pass all hard gates; it is not another gate.
- Never point both services at one database or run duplicate database writers.

- The shared SQLite bus at `/home/ubuntu/shared/intent-bus/intent_bus.sqlite3`
  is the authoritative executor handoff. Publish only after admission and
  routing; never claim receipts or execution state in this repository.
- `INTENT_BUS_DB` must be an explicit absolute path and
  `INTENT_BUS_BYBIT_ENABLED` must be enabled for Bybit delivery. Legacy JSON
  inbox writing is compatibility-only and defaults off.
- Compact strategies route to `bybit/hyro`; dual-zone strategies route to
  `bybit/fundamo`.

## Production audit state (2026-08-30)

The daemon consumes completed gateway triggers, runs the pipeline, and invokes
the signal publisher after successful evaluation. Publisher failures are kept
separate from ingestion failures; the trigger is only processed after the
pipeline itself succeeds. `raw_signals` remains an observation ledger and the
30-minute Discord batch is non-blocking.

Snapshot positions without an originating intent are classified as
`strategy_id=unmanaged`. The PM sidecar emits a durable neutral `HOLD` advice
and decision for those positions without calling the LLM or creating an exit.
Normal positions retain their originating strategy metadata. First-write bus
delivery and execution handoff remain consumer-owned and must be verified from
the publisher/execution-delivery tables before enabling additional routes.

## Verification

Run `python3 -m pytest -q` and `git diff --check` before committing. Production
services are managed with `oxmgr`; do not start duplicate gateway, orchestrator,
or PM sidecar processes manually.
