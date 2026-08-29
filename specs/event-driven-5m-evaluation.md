# 5m Event-Driven Strategy Evaluation

Status: implementation specification

## 1. Goal

Replace the research-analyst orchestrator's fixed 15-minute evaluation loop with
5-minute cutoff-driven evaluation. A completed 5m market bar must trigger one
evaluation for that cutoff and selected candidates must reach the bybit-executor
intent inbox immediately after admission and clash resolution.

This change affects evaluation scheduling only. Strategy logic, admission rules,
intent schema, executor ownership, and protection behavior remain unchanged.

## 2. Current State

The current production topology is managed by `oxmgr`:

```text
research-analyst-ws -> market.sqlite3
research-analyst-orchestrator -> analyst.sqlite3 + alpha_outbox + intent inbox
bybit-executor <- shared intent inbox
```

The WebSocket gateway is the sole market database writer. Its writer task batches
incoming observations, flushes them to `source_observations`, and periodically
derives 15m/1h/4h bars from 5m data. The orchestrator sleeps according to
the legacy `INGEST_INTERVAL_MINS` timer and invokes all configured evaluation
intervals together.

Intent delivery is already immediate after a selected candidate is passed to
`alpha_outbox.write_event`: `write_event` calls `_maybe_deliver_intent`, which
builds, validates, and atomically writes the executor envelope. The missing seam
is the trigger between completed market bars and plugin evaluation.

## 3. Target Behavior

```text
Bybit WebSocket 1m/5m update
        |
        v
gateway writer persists observation
        |
        +-- completed 5m cutoff detected
                |
                v
        durable evaluation trigger
                |
                v
        research evaluator claims cutoff
                |
                v
        feature materialization + 5m evaluation
                |
                v
        admission + clash resolution
                |
                v
        alpha event + TradeIntent file
```

The evaluator may also evaluate 1m and 15m as enrichment/observability work, but
the first implementation must make the 5m path independently triggerable and
must not wait for a 15-minute timer.

## 4. Non-Goals

- Do not move strategy execution into the WebSocket process.
- Do not add a second market database writer.
- Do not change strategy thresholds, ranking, hard gates, or clash policy.
- Do not change the TradeIntent schema or executor sizing/protection ownership.
- Do not make every tick an evaluation trigger.
- Do not use wall-clock polling as the primary trigger.
- Do not change `bybit-executor` behavior unless its inbox contract requires a
  small compatibility adjustment.

## 5. Trigger Contract

### 5.1 Trigger unit

The unit of work is one completed 5m cutoff:

```text
interval = "5m"
cutoff_at = completed 5m bar end timestamp
cutoff_id = "5m:<cutoff_at in canonical UTC form>"
```

The trigger must be emitted only after all required 5m observations for the
cutoff have been persisted. A still-open bar must never trigger evaluation.

### 5.2 Trigger storage

Add a durable trigger table to the analyst database, for example:

```sql
CREATE TABLE IF NOT EXISTS evaluation_triggers (
    trigger_id TEXT PRIMARY KEY,
    interval TEXT NOT NULL,
    cutoff_at TIMESTAMP NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    claimed_at TIMESTAMP,
    completed_at TIMESTAMP,
    lease_until TIMESTAMP,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    UNIQUE(interval, cutoff_at)
);
```

Allowed states:

```text
pending -> claimed -> completed
                  \-> pending   (retry after failure/lease expiry)
                  \-> failed    (terminal only after explicit policy)
```

The unique `(interval, cutoff_at)` constraint makes gateway retries and websocket
reconnects harmless.

The gateway should not directly write the analyst database because it is the sole
owner of the market database and must remain isolated from analyst DB schema and
transaction failures. Use one of these boundaries, in preferred order:

1. A filesystem trigger spool watched by the evaluator.
2. A local Unix socket or pipe owned by the evaluator, with durable spool fallback.
3. A lightweight trigger database owned by the gateway and claimed by the evaluator.

The simplest deployable design is a filesystem spool under the analyst data
directory. The gateway atomically creates:

```text
data/evaluation_triggers/5m-2026-08-29T14-50-00Z.json
```

The evaluator treats the filename/cutoff as the idempotency key and records
completion in `evaluation_triggers`.

## 6. Gateway Changes

Modify `ws_gateway.writer_task` and its persistence seam as follows:

1. Keep buffering and `INSERT OR IGNORE` behavior unchanged.
2. After a batch flush, determine which 5m bars have become complete.
3. For each newly complete cutoff, atomically publish one trigger.
4. Publish only after the corresponding observations are committed.
5. On startup, backfill triggers for recent completed 5m cutoffs that exist in
   `source_observations` but have no trigger file.
6. Never block websocket reads on evaluator work.
7. Bound trigger publication work; coalesce duplicate cutoffs before writing.

The gateway currently derives higher-timeframe bars on a roughly 60-second
maintenance timer. The 5m trigger should be based on the persisted streamed 5m
bar, not on the 15m/1h/4h resampling loop. If the provider sends only 1m bars,
the trigger must be based on the locally completed 5m aggregation and published
after that aggregate is committed.

## 7. Evaluator Changes

Refactor the current `_run_pipeline` work into an interval-scoped function with
an explicit cutoff:

```python
run_evaluation(cutoff_at: datetime, interval: str = "5m") -> EvaluationResult
```

The function must:

1. Validate that `cutoff_at` is a completed 5m boundary.
2. Confirm required market observations exist and are fresh enough.
3. Materialize features for that cutoff.
4. Invoke plugins with `eval_interval="5m"` and the explicit cutoff.
5. Run admission and clash resolution once for the candidate set.
6. Write selected alpha events and executor intents synchronously.
7. Persist observability and trigger completion only after all writes finish.

The daemon becomes a trigger consumer:

```text
watch spool
  -> claim oldest pending cutoff
  -> run_evaluation(cutoff, "5m")
  -> mark completed or retry
```

The existing 15m sleep loop must not remain as a second evaluation owner. A
short fallback recovery scan is acceptable, but it must only discover missing
triggers, not run duplicate evaluations.

## 8. Claiming, Leases, and Recovery

Use an exclusive claim so only one evaluator instance can process a cutoff:

1. Atomically rename `pending` to `claimed`, or acquire a database lease.
2. Set `attempt_count`, `claimed_at`, and `lease_until`.
3. Renew the lease only if evaluation can exceed the lease duration.
4. On process restart, reclaim expired leases.
5. Re-running a claimed cutoff must be safe because candidate identity and intent
   delivery are idempotent.

A failed evaluation must remain visible with its exception and cutoff. Retry with
bounded backoff. Do not silently mark a trigger complete when feature materialization,
plugin execution, admission, or intent writing fails.

Recommended defaults:

```text
lease: 10 minutes
retry backoff: 5s, 30s, 2m, 10m
max automatic retries: 5
retention: 7 days for completed trigger records
```

## 9. Ordering and Backlog Policy

Process cutoffs in ascending timestamp order. If multiple bars arrive during an
outage, replay each cutoff rather than evaluating only the newest one. Do not let a
stale backlog produce valid live intents: the existing `entry_valid_until` and
executor expiry rules remain authoritative.

Before writing an intent, the existing geometry and validity checks must still run.
An expired candidate may remain auditable in the alpha ledger but must not become
an executable opportunity.

## 10. 1m and 15m Interaction

The first target is 5m only.

- 1m observations remain available for strategy context and future evaluation.
- 15m/1h/4h derived bars remain enrichment data.
- 15m evaluation should not be triggered by the 5m event unless explicitly
  configured as a separate interval trigger.
- `invoke_plugins_for_intervals` should eventually support an explicit interval
  selection so the 5m event does not rerun unrelated intervals.

This avoids the current triple evaluation of the same cutoff and reduces CPU,
database contention, and duplicate candidate churn.

## 11. Executor Compatibility

The research analyst continues writing schema-version-1 JSON files to the shared
`INTENT_INBOX`. The executor remains responsible for:

- claiming/processing inbox files;
- delivery-id deduplication;
- expiry handling;
- symbol and account allowlists;
- quantity, risk, and leverage policy;
- order placement and lifecycle;
- protective stop-loss and take-profit behavior.

The event-driven change must not make the analyst call executor APIs directly.
Before implementation, verify the executor's file watcher/poll cadence and ensure
the new producer does not rely on an in-memory notification reaching the executor.
The file write is the cross-process contract.

## 12. Configuration

Evaluation no longer uses `INGEST_INTERVAL_MINS`. Use explicit settings:

```text
EVALUATION_INTERVAL=5m
EVALUATION_TRIGGER_DIR=./data/evaluation_triggers
EVALUATION_LEASE_SECONDS=600
EVALUATION_MAX_RETRIES=5
EVALUATION_RECOVERY_SCAN_SECONDS=30
```

Keep a recovery scan interval only as a safety net for missed filesystem events.
It must consume pending trigger records and must not recreate the old 15-minute
timer behavior.

## 13. Observability

Add structured fields to logs and `data/health.json`:

```text
lastTriggeredInterval
lastTriggeredCutoff
lastCompletedCutoff
evaluationLagSeconds
pendingTriggerCount
claimedTriggerCount
failedTriggerCount
evaluationsLastCycle
intentsWrittenLastCutoff
```

Distinguish these states in metrics and logs:

```text
candidate emitted
candidate selected
alpha event written
TradeIntent written
TradeIntent processed by executor
TradeIntent accepted/rejected
order filled
```

## 14. Testing Requirements

### Unit tests

- Completed 5m boundary detection.
- Incomplete 5m bars do not trigger.
- Duplicate gateway notifications produce one trigger.
- Trigger filenames/IDs are canonical and timezone-stable.
- Claiming is exclusive.
- Expired leases can be reclaimed.
- Failed evaluations retry and preserve the error.
- Successful evaluation marks completion only after intent write.

### Integration tests

- Persist a completed 5m observation, publish a trigger, consume it, and assert a
  selected candidate reaches `INTENT_INBOX`.
- Restart the evaluator between claim and completion and assert safe replay.
- Replay several missed cutoffs in order.
- Assert no duplicate alpha events or intents on trigger replay.
- Assert an expired replay does not create an executable intent.
- Assert websocket ingestion continues while evaluation is deliberately slowed.

### Regression tests

- Existing strategy plugin tests.
- Existing intent geometry and idempotency tests.
- Existing WebSocket persistence/resampling tests.
- Existing operational metrics tests.
- Full `pytest` suite.

## 15. Rollout Plan

1. Implement trigger publication and consumer in disabled/shadow mode.
2. In shadow mode, record which 5m cutoffs would have been evaluated and compare
   them with the current 15m loop.
3. Enable event-driven 5m evaluation with intent delivery still disabled.
4. Verify cutoff lag, backlog, CPU, database lock time, and duplicate counts.
5. Enable intent delivery against the paper executor path.
6. Confirm executor processing and expiry behavior.
7. Keep the legacy timer disabled; event consumption is the only daemon mode.
8. Remove obsolete timer configuration and update `oxmgr` definitions.

Rollback must disable event consumption and re-enable the single legacy evaluator
mode without starting a second database writer or evaluator concurrently.

## 16. Acceptance Criteria

- A completed 5m bar causes evaluation without waiting for a 15-minute timer.
- End-to-end trigger-to-intent latency is normally under 30 seconds after bar
  completion.
- Each `(interval, cutoff)` is evaluated at most once concurrently.
- Missed cutoffs replay after evaluator restart.
- Duplicate websocket messages do not duplicate alpha events or intents.
- A slow evaluator does not block websocket ingestion.
- Expired candidates do not reach the executor as valid opportunities.
- Existing admission, geometry, routing, and executor contracts remain unchanged.
- `oxmgr` remains the only production process manager.
- Full test suite and event-driven integration tests pass.
