# Strategy Runner Process Decoupling

## Status

Implementation specification.

This specification changes only the process placement of the strategy stage. It
does not introduce a new business stage or change the existing evaluation flow:

```text
ingestion -> regime -> strategy -> merged scorer/admission -> publisher
```

The existing strategy, scorer, admission, clash-resolution, alpha-outbox, and
publisher contracts remain authoritative unless explicitly changed below.

## Problem Statement

Strategy promotion and retirement currently require restarting the managed
orchestrator because plugin invocation runs inside that process. Restarting the
orchestrator also interrupts the surrounding evaluation cycle and couples
strategy code deployment to cutoff handling, candidate persistence, admission,
and publishing.

The strategy stage needs an independent process lifecycle without creating
independent per-strategy pipelines, changing the order of the existing stages,
or allowing a strategy process to write analyst or delivery state directly.

## Solution

Move the active-plugin invocation loop into one managed `strategy-runner`
process. The runner evaluates the complete active strategy set for one cutoff
and returns the same candidate records and per-strategy observability that the
current in-process invocation produces.

The orchestrator remains the evaluation owner around that seam. It continues to
coordinate the existing cutoff, regime scope, candidate capture, merged
scoring/admission, clash resolution, selected-event persistence, and publisher
invocation.

```text
ws_gateway
  -> market.sqlite3 + completed-cutoff trigger
  -> regime-session worker
       -> regime.sqlite3
  -> orchestrator
       -> strategy-runner
            -> candidate records + strategy results
       -> raw_signals
       -> scorer/admission + clash resolution
       -> alpha outbox + publisher
       -> shared intent bus
```

The process seam is transport only. It must not become a second scorer,
admission layer, publisher, trigger consumer, or database owner.

## User Stories

1. As an operator, I want to restart the strategy runner without restarting
   market ingestion, so that public data collection remains continuous.
2. As an operator, I want to promote or retire strategies by restarting only
   the strategy runner, so that lifecycle changes have a small blast radius.
3. As an operator, I want the existing regime worker to continue independently
   while strategies restart, so that regime observations are not interrupted.
4. As an operator, I want scorer/admission and publishing to remain available
   while strategy code is being changed, so that downstream contracts stay
   stable.
5. As a strategy author, I want the runner to evaluate all active strategies
   for one cutoff, so that cross-strategy scorer and clash behavior remains
   deterministic.
6. As a strategy author, I want each plugin to receive the same cutoff-bound
   inputs as it receives today, so that process decoupling does not change
   strategy behavior.
7. As a strategy author, I want strategy plugins to return candidates rather
   than write events, so that the strategy stage has a narrow, testable
   interface.
8. As an operator, I want a runner failure to be visible and retryable, so that
   a process crash does not silently lose a completed cutoff.
9. As an operator, I want a runner restart during an evaluation to be safe, so
   that a cutoff can be retried without duplicate raw candidates, alpha events,
   or executor intents.
10. As a researcher, I want candidate records to retain the exact strategy ID,
    plugin version, cutoff, universe, and regime scope, so that results remain
    replayable after a runner deployment.
11. As a researcher, I want strategy failures isolated per plugin, so that one
    broken strategy does not suppress valid candidates from other strategies.
12. As a database owner, I want the runner to read market, regime, and analyst
    state only, so that existing single-writer ownership remains intact.
13. As a publisher, I want to receive the same selected candidate set as before,
    so that delivery and intent-bus behavior do not change.
14. As an operator, I want retired strategies to stop producing new candidates
    while existing persisted events remain immutable, so that retirement does
    not rewrite history.
15. As an operator, I want strategy activation to be frozen at the start of a
    cutoff evaluation, so that one cutoff cannot contain a mixed strategy set.
16. As an operator, I want the runner health state to distinguish unavailable,
    failed, and successfully completed evaluations, so that restart recovery is
    diagnosable.

## Implementation Decisions

### 1. Process ownership

- Add one managed `strategy-runner` process.
- There is exactly one strategy-runner instance in production.
- The runner loads and evaluates multiple plugins; there is no process per
  strategy.
- The orchestrator remains the owner of the existing evaluation trigger and
  surrounding pipeline lifecycle.
- The regime worker, WebSocket gateway, scorer/admission implementation, and
  publisher remain separate from the runner.
- Production process management remains owned by `oxmgr`.

### 2. Single process seam

The only new external seam is a request/response interface between the
orchestrator and the strategy runner. A local Unix-domain socket is the default
transport because both processes run on the same host and no network service is
needed. The transport may use another local mechanism only if it preserves the
same request, response, timeout, and recovery contract.

The orchestrator sends one request for one evaluation cutoff. The runner
returns one complete response for that request. The response must not contain
live Python objects, database connections, Polars frames, or filesystem paths
that are meaningful only inside the runner.

### 3. Strategy request contract

Every request contains:

- protocol version;
- request ID;
- evaluation cutoff ID and exact UTC cutoff timestamp;
- evaluation interval;
- immutable effective-universe assets;
- effective-universe feed ID and version;
- regime scope and its provenance;
- active strategy manifest;
- request deadline or timeout information.

All timestamps crossing the seam are UTC ISO-8601 strings. The exact cutoff is
authoritative; the runner must not substitute wall-clock time for strategy
data, candidate timestamps, freshness, or expiry calculations.

The active strategy manifest is frozen for the request. It includes, at
minimum, each strategy ID, plugin version, cadence, family, required intervals,
feature requirements, statefulness declaration, and a configuration fingerprint.
The runner must reject a request whose manifest cannot be loaded or validated.

### 4. Strategy response contract

The response contains:

- protocol version;
- request ID;
- cutoff ID and exact cutoff timestamp;
- manifest identity and entries actually evaluated;
- per-strategy status: `completed`, `skipped`, or `failed`;
- per-strategy reason where applicable;
- zero or more candidate records for each completed strategy;
- evaluation coverage and emitted counts;
- bounded computation and timing observability;
- runner implementation version.

Candidate records must preserve the existing strategy output contract, including
strategy ID, plugin version, asset, direction, observed timestamp, expiry,
entry condition, invalidation price, targets, feature evidence, source
provenance, data freshness, and input snapshot identity.

Candidate records and all nested values crossing the seam must be JSON-safe.
Nested datetimes become UTC ISO-8601 strings. Non-finite numeric values are
rejected rather than serialized ambiguously.

### 5. Orchestrator behavior around the seam

The orchestrator keeps the current order:

1. Claim the existing completed-cutoff trigger.
2. Establish the exact cutoff and regime scope.
3. Build the active strategy manifest for that cutoff.
4. Request strategy evaluation from the runner.
5. Capture every returned candidate in `raw_signals` before admission.
6. Build candidate-owned structural context.
7. Run the existing scorer, hard admission, and clash resolution once over the
   complete candidate set.
8. Write only selected events through the existing alpha-outbox path.
9. Run the existing publisher behavior.
10. Mark the cutoff processed only after the existing pipeline success
    conditions are satisfied.

The scorer/admission stage must receive the same complete candidate set it would
have received from the current in-process plugin invocation. It must not run
once per strategy or once per runner response fragment.

### 6. Runner behavior

The runner must:

- load only the requested manifest entries;
- evaluate all requested strategies for the same cutoff and scope;
- preserve the existing per-plugin failure isolation behavior;
- keep one cutoff-bound shared computation context for all plugins within the
  request;
- read `market.sqlite3` and `regime.sqlite3` read-only;
- read analyst state needed for re-arm checks read-only;
- use the existing strategy feature and direct-HTF contracts;
- return candidates without invoking scorer/admission or clash resolution;
- return candidates without writing raw signals, alpha events, outbox files,
  intent-bus records, Discord messages, or Telegram messages.

The runner must not call Bybit REST or WebSocket APIs. Ingestion and direct
regime-history ownership remain unchanged.

### 7. Plugin purity

Every registered plugin must conform to:

```text
strategy(cutoff-bound input) -> zero or more candidate records
```

Plugin code must not call the alpha outbox, raw-signal persistence, publisher,
intent-bus publisher, or any write-capable database function. Existing legacy
plugins that write events directly must be converted to return candidates
before they can be enabled in the decoupled runner.

Read-only re-arm checks against persisted alpha events or the outbox remain
allowed when required by an existing strategy contract. They must not mutate
that state.

### 8. Strategy lifecycle and cutoff consistency

- `STRATEGY_ENABLED_IDS` controls which plugins the runner can load.
- `plugin_states` remains the runtime active/inactive/paused control unless a
  later specification changes it.
- The effective active set is resolved once at request start and frozen for the
  entire cutoff.
- A promotion or retirement takes effect on the next request after the runner
  has loaded the new configuration.
- A strategy that is already persisted remains auditable after retirement.
- A strategy configuration change that can alter candidate output requires a
  plugin version change or an equivalent manifest version change.
- The runner must not mix old and new plugin versions in one request.

### 9. Restart and failure semantics

The existing cutoff trigger remains the retry unit. No second trigger consumer
or second evaluation scheduler is introduced.

- If the runner is unavailable, the orchestrator records the evaluation as
  incomplete and retries the claimed cutoff using the existing trigger lease
  and retry policy.
- If the runner exits before sending a complete response, no partial response
  is accepted.
- If the orchestrator exits after receiving candidates but before downstream
  completion, the cutoff is retried.
- Retried strategy evaluation must be safe through existing candidate identity,
  raw-signal idempotency, alpha-outbox deduplication, and intent-bus
  deduplication.
- The runner must not acknowledge a request until its complete response has
  been written to the transport.
- The orchestrator must not mark the trigger processed merely because the
  runner returned candidates; existing scorer/admission and publisher rules
  still apply.
- A runner restart must not cause a previously completed cutoff to be evaluated
  concurrently by two runner instances.

### 10. Shared computation tradeoff

The runner retains one shared computation context across all strategies in its
request. That context is process-local and is not serialized across the seam.

The scorer/admission stage may rehydrate its own exact-cutoff market and regime
context after receiving candidates. It must use the authoritative databases and
existing provenance rules. It must not depend on transient frames from the
runner.

This is an intentional process-decoupling tradeoff. Numerical outputs,
cutoffs, warmup, source selection, lookahead protection, admission proofs, and
candidate identities must remain unchanged. Any additional recomputation must
be measured and documented.

### 11. Observability

Runner and orchestrator logs must include bounded structured fields for:

- request ID;
- cutoff ID and cutoff timestamp;
- manifest identity;
- runner process version;
- request start, response, and duration;
- runner availability and timeout state;
- strategy counts for completed, skipped, failed, and emitted;
- candidate count handed to scorer/admission;
- retry attempt and final trigger state.

Health output must distinguish:

- runner unavailable;
- runner request timed out;
- strategy evaluation failed;
- strategy evaluation completed with zero candidates;
- candidates returned to scorer/admission;
- downstream scorer/admission or publisher failure.

### 12. Deployment configuration

- Add a managed `strategy-runner` definition to the runtime configuration.
- The runner receives the same import path, database paths, strategy settings,
  and regime settings needed for read-only evaluation.
- The orchestrator receives only the runner transport address and timeout
  settings.
- No production secrets or executor credentials are added to the runner.
- Do not run the old in-process strategy invocation concurrently with the new
  runner in production.
- Restart only the strategy runner when changing its strategy code or active
  strategy configuration.

### 13. Documentation alignment

Update the current runtime and hybrid-computation documentation to describe the
strategy runner as a process placement of the existing strategy stage. Do not
describe it as a new scorer, admission, publisher, trigger, or database owner.
Document that the shared numerical context is shared across strategies inside
the runner and is rehydrated independently by downstream admission when
needed.

## Testing Decisions

Tests must exercise the external process seam and observable behavior rather
than private implementation details. Existing plugin unit tests remain valid
for strategy formulas; new tests must prove that the process boundary preserves
the existing stage contract.

### Unit tests

- Request serialization preserves exact UTC cutoff and rejects malformed input.
- Response serialization preserves candidate fields, provenance, and nested
  timestamps.
- Non-finite values and non-JSON values are rejected at the seam.
- An active manifest loads the expected plugins and rejects unknown IDs,
  versions, or configuration fingerprints.
- A plugin failure produces a per-strategy failure without suppressing other
  plugin results.
- A plugin returning no candidates produces a completed zero-emission result.
- Direct plugin writes to the outbox or analyst ledger are rejected by the
  purity contract and regression tests.

### Integration tests

- The orchestrator sends one cutoff request and passes the complete returned
  candidate set once to scorer/admission.
- The existing regime scope and effective-universe version arrive unchanged at
  the runner.
- A runner restart before response causes retry of the same cutoff.
- A runner restart after response but before downstream completion causes safe
  replay without duplicate raw signals, alpha events, or intents.
- Two runner instances cannot process one cutoff concurrently.
- A retired strategy produces no new candidate after the lifecycle change, while
  its prior persisted events remain unchanged.
- A promoted strategy begins on the first eligible cutoff after the restart.
- A stale or expired replay remains auditable but does not create a valid
  executable intent.
- Ingestion and regime workers continue while the strategy runner is stopped.
- Scorer/admission output is equivalent between the in-process reference path
  and the decoupled runner path for fixed fixtures.

### Operational tests

- The managed runner starts with the required read-only database access.
- Runner health reports unavailable and recovers after restart.
- Runner timeout and retry metrics are visible in health output.
- The old in-process strategy path is not started concurrently.
- Full repository tests, compilation, and the existing runtime health checks
  pass.

## Out of Scope

- Changing strategy formulas, thresholds, indicators, or target geometry.
- Creating one process per strategy.
- Moving scorer, hard admission, structural admission, clash resolution, or
  publisher into the runner.
- Moving ingestion or regime computation into the runner.
- Adding a new market database, regime database, analyst database, or intent
  bus.
- Changing database writer ownership.
- Changing executor sizing, leverage, order, fill, or protection behavior.
- Changing raw-signal, alpha-event, publisher, or intent-bus schemas except for
  fields strictly required to record the strategy manifest or seam observability.
- Replacing the existing cutoff trigger with a new scheduler.
- Adding strategy hot reload while a cutoff is in progress.
- Persisting transient Polars frames across the process seam.
- Introducing network exposure for the strategy runner.
- Running legacy direct-writing plugins in the decoupled runner.

## Further Notes

The highest-value seam is the existing conceptual boundary where plugin results
are collected before `trade_quality.resolve()` runs. The implementation should
replace that in-process call with an adapter rather than duplicate the
surrounding pipeline.

The success condition is not merely that the runner can restart. The success
condition is that, for a fixed cutoff and manifest, the downstream scorer,
admission, clash, alpha, publisher, and intent behavior is observationally
equivalent to the current path, while the runner can be stopped and restarted
independently.
