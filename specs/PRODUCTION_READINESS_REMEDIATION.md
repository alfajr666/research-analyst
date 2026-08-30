# Production Readiness Remediation

Status: implementation plan
Scope: confirmed findings from the cross-repository production audit

## 1. Goal

Make the Research Analyst pipeline durable, observable, and correctly wired to
the executor handoff while preserving strategy calculations and the existing
advisory semantics.

## 2. Invariants To Preserve

- The analyst remains an intent producer, not an executor or position manager.
- Market database ownership remains with the WebSocket gateway.
- The shared SQLite bus remains authoritative when enabled.
- Admission, routing, sizing, leverage, protection, and execution remain
  executor-owned.
- LLM failures default to HOLD and never create an executable exit by
  themselves.
- Existing strategy output and dedupe identities remain stable.

## 3. Required Changes

### 3.1 Make pipeline failures durable and retryable

- Cutoff, feature materialization, and plugin failures must propagate to
  `run_pipeline()` as failed runs, or be represented as an explicit skipped
  state with a reason when the failure is intentionally non-fatal.
- A failed event trigger must remain recoverable and must not be renamed
  `.processed`.
- `pipeline_runs.status`, `error_message`, and trigger state must agree.

Acceptance:

- A plugin exception creates a failed pipeline run and leaves the trigger in
  retryable form.
- A successful run is processed exactly once.
- Health output distinguishes failed, skipped, and completed runs.

### 3.2 Run the signal publisher in the production daemon

- The daemon must invoke the same persistence and delivery cycle as the
  one-shot path after a successful pipeline cycle.
- Publisher failure must be isolated from market ingestion but visible in
  operational metrics.
- Execution delivery remains independent from Telegram/Discord delivery.

Acceptance:

- A daemon cycle moves a valid alpha outbox event into `alpha_events`.
- A configured Bybit bus path receives the expected target-scoped delivery.
- Publisher failures do not falsely mark the pipeline failed when ingestion
  itself succeeded.

### 3.3 Preserve PM metadata across executor snapshots

- Snapshot-derived positions must carry a valid strategy identity or be
  explicitly classified as unmanaged.
- Unmanaged positions must not cause a NOT NULL insertion failure.
- PM advice for unmanaged positions must either use a documented neutral
  strategy identity or be skipped with a durable reason; it must never silently
  disappear.

Acceptance:

- A snapshot with empty `original_json` produces an observable skip or a valid
  advice row according to the selected policy.
- A normal executor snapshot produces one advice and one decision handoff.

### 3.4 Retry failed first-write bus publication

- A durable alpha outbox event must have a delivery state independent of the
  file dedupe result.
- A duplicate write must not suppress a previously failed bus publication.
- Retries remain bounded and preserve the original delivery identity.

Acceptance:

- Simulated first publication failure is retried on a later publisher cycle.
- Successful duplicate handling does not create a second alpha event or bus
  delivery.

## 4. Explicit Non-Goals

- No strategy formula changes.
- No changes to executor risk or order behavior.
- No changes to shared bus mechanics.
- No automatic Discord/Telegram retry queue beyond existing publisher policy
  unless separately specified.

## 5. Rollout Order

1. Fix daemon publisher invocation and failure-state transitions.
2. Add PM metadata policy and regression tests.
3. Add durable outbox publication state and retry tests.
4. Verify live counts and trigger recovery before enabling additional strategy
   families.
