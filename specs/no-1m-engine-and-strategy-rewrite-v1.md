# No-1m Engine And Strategy Rewrite v1

**Status:** implementation contract

This document removes 1m market-data and evaluation work from Research Analyst.
The engine uses completed 5m observations as its only streamed and triggerable
base interval. Strategies that previously used 1m inputs are rewritten to use
completed 5m inputs with explicit 5m provenance.

## 1. Scope

### In scope

- Bybit and optional Binance market ingestion.
- REST backfill and WebSocket subscriptions.
- Durable evaluation triggers and orchestrator interval routing.
- Shared strategy feature materialization and sequential frame caching.
- Production strategy registry and all registered strategy implementations that
  currently depend on 1m market data.
- Market-data retention and related tests and documentation.

### Out of scope

- Executor-owned 1m position snapshots consumed by the PM sidecar. They are an
  independent executor handoff contract and remain unchanged.
- Regime-owned direct 1h/4h history.
- Hybrid 1h/4h seed and canonical 5m-tail behavior.
- Structural admission, executor sizing, protection, fills, and receipts.
- Deleting historical 1m rows or quarantining old 1m trigger artifacts. The
  engine stops reading and writing them; cleanup is a separate operations task.

## 2. Locked invariants

1. `5m` is the only streamed base market interval.
2. The gateway publishes durable evaluation triggers only for completed `5m`
   observations.
3. Production evaluation defaults to `EVAL_INTERVALS=5m`.
4. Derived `15m`, `1h`, and `4h` observations remain available from the
   completed 5m canonical tail. Existing explicitly configured 15m evaluation
   remains a non-production extension point, not a base stream.
5. No production strategy reads, requires, or feature-materializes 1m data.
6. The evaluator rejects 1m trigger payloads rather than silently converting
   them to 5m.
7. Existing 1m trigger files are ignored by pending/claim/recovery scans and
   are not deleted by the service.
8. The executor PM sidecar continues to read 1m position snapshots.
9. Account-symbol admission remains downstream of strategy evaluation.
10. Strategy IDs remain stable for the configured production handoff, while
    their `plugin_version` and feature provenance advance to `v2` where the
    strategy semantics changed.

## 3. Strategy rewrites

The following strategies retain their configured IDs for operational continuity,
but their plugin version changes to `v2` and their emitted provenance explicitly
declares the new 5m semantics.

| Strategy | Previous dependency | New contract |
| --- | --- | --- |
| `williams-fractal-scalp-v1` | 1m execution bars | Williams fractal and EMA20/50/100 rules on completed 5m bars |
| `ema9-continuation-stochrsi-v1` | 5m setup + 1m trigger | 5m setup and 5m StochRSI trigger |
| `ema9-adx-stochrsi-state-v1` | 1m trigger + 5m structure + 1h ADX | 5m trigger/structure + 1h ADX |
| `ema99-double-touch-stochrsi-state-v1` | 1m touch/oscillator + 5m execution + 1h ADX | 5m touch/oscillator/execution + 1h ADX |

For the stateful strategies:

- A second touch must occur on a later completed 5m candle.
- Entry, exit, observed-at, expiry, and feature fields use the completed 5m
  observation.
- Former `*_1m` feature keys are replaced by `*_5m` keys.
- The proposed stop and target policy remain unchanged unless a strategy's
  existing implementation explicitly derives them from the rewritten 5m state.
- No implementation may resample 1m data in memory to preserve the old signal.

This is a semantic strategy rewrite, not an interval alias. Historical v1
signals remain historical records; new signals identify the v2 plugin version.

## 4. Engine changes

### Gateway

- Default and production `WS_STREAM_TIMEFRAMES` is `5m`.
- Bybit and Binance 1m topic mappings are removed from the live path.
- REST backfill loops only over configured 5m base bars.
- Base trigger publication accepts only `5m` rows.
- 5m remains the source for local 15m/1h/4h resampling.
- Health reports the 5m topic count and effective watchlist universe.

### Trigger spool

- `publish`, `pending`, claim recovery, and cutoff normalization support only
  `5m` evaluation triggers.
- A supplied `1m` interval raises a contract error.
- Existing 1m files remain untouched and are not returned as pending work.

### Orchestrator and context

- The default interval list is `5m`.
- The 1m-specific regime cutoff translation is removed.
- Shared sequential frame caching treats `5m` as the only base evaluation
  interval; HTF state remains engine-owned.
- An unexpected 1m trigger fails closed and is recorded as unsupported rather
  than being evaluated.

### Retention

- No new 1m observations are written.
- The active retention policy has no 1m tier. Historical 1m rows are not
  deleted by this change.

## 5. Rollout and recovery

1. Deploy code and config defaults together.
2. Restart the managed gateway, orchestrator, regime worker, symbol rotation
   worker, and PM sidecar through `oxmgr`.
3. Verify the gateway reports only 5m market topics and fresh 5m bars.
4. Verify the trigger spool and orchestrator report only 5m evaluations.
5. Verify each rewritten strategy's required intervals contain no 1m entry.
6. Keep executor 1m snapshots and PM decisions operationally unchanged.
7. If rollback is required, restore the prior code and configuration; do not
   rewrite or delete historical 1m observations or trigger files.

## 6. Acceptance criteria

- No production source path subscribes to, backfills, persists, or triggers on
  1m market bars.
- No production strategy registry entry requires a 1m dataset or interval.
- The four rewritten strategies evaluate correctly using completed 5m data and
  emit v2 provenance.
- 1m trigger files are ignored and never executed.
- Derived 15m/1h/4h and hybrid HTF tests remain green.
- PM sidecar continues to process executor 1m snapshots.
- Full unit and integration test suites pass.
