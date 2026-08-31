# LLM-Only Position Management v1

## Status

Locked design specification. This document is the implementation blueprint for
the PM contract recorded in `specs/llm-position-sidecar.md`.

This is a design lock, not an assertion that the current runtime already
implements every requirement. The implementation must land in small, verified
steps and must not enable new execution authority implicitly.

## 1. Decision Summary

The system has one position-management decision authority: the independent LLM
PM sidecar. There is no independent mechanical PM sidecar and no mechanical
LLM-veto path.

The sidecar emits one semantic decision per open position and five-minute
cutoff:

```text
HOLD       no management action
REDUCE     reduce current exposure now
EXIT       close current exposure now
NEAR_TP    hold the runner, then reduce once near original TP
```

The executor remains the only component that can verify venue state, calculate
actual quantities, submit orders, update protection, and record execution.

The producer owns target selection:

```text
explicit strategy target -> preserve it
missing strategy target  -> derive a 2R target from entry reference and stop
```

Every PM decision is valid for five minutes. A decision that is expired,
duplicated, stale, or aimed at a non-open position has no execution effect.

## 2. Non-Negotiable Invariants

1. One PM decision stream exists for each executor profile.
2. The LLM never places an order or selects a venue, account, symbol, size, or
   price.
3. The executor remains authoritative for venue state and protection.
4. The producer supplies a concrete TP before delivery to the executor.
5. Explicit strategy targets take precedence over the producer 2R fallback.
6. A missing entry reference or invalid stop never produces a guessed TP.
7. `HOLD` is a no-op and never implicitly means `NEAR_TP`.
8. `REDUCE`, `EXIT`, and `NEAR_TP` require a valid confidence threshold result.
9. Stop-loss, take-profit, liquidation, protection failure, and emergency
   containment never depend on LLM confidence or availability.
10. PM decisions are scoped to the exact exchange, account, symbol, side, and
    position identity supplied by the executor.
11. A PM decision cannot be retried as a new action after a terminal executor
    result.
12. A reduction is based on executor-confirmed current quantity, never on the
    sidecar's old snapshot quantity.
13. A `NEAR_TP` reduction is confirmed at most once per position lifecycle.
14. No stale executor snapshot may be replaced silently with a local position
    row.
15. Advisory, selected, delivered, accepted, executed, and filled states remain
    distinct in the audit trail.

## 3. Runtime Topology

```text
ws_gateway
  -> market.sqlite3
  -> completed market observations and evaluation triggers

orchestrator
  -> finalized strategy events
  -> producer admission and target construction
  -> shared intent bus

bybit-executor
  -> venue reconciliation
  -> protected entries
  -> 1m position snapshots
  -> SL/TP, emergency containment, and PM decision execution

pm_sidecar
  -> fresh executor snapshots
  -> originating intent and strategy context
  -> one LLM request per open position and 5m cutoff
  -> confidence/action gate
  -> shared PM decision inbox
```

The PM sidecar is independent from the orchestrator. It must not share the
market database writer, rerun strategy evaluation, or block market ingestion.

Shared handoff paths remain executor-owned and are resolved through
`BYBIT_EXECUTOR_DIR`:

```text
data/intents
data/position-snapshots
data/position-decisions
```

The shared SQLite intent bus is authoritative for entry delivery. JSON intent
inbox delivery is compatibility-only and must not be treated as a second
authority.

## 4. Producer-Owned Take-Profit

### 4.1 Target precedence

The producer constructs the executor intent in this order:

1. Use the first explicit strategy target when present and valid.
2. Otherwise derive a 2R target when direction, entry reference, and stop are
   all valid.
3. Otherwise reject delivery as incomplete. Do not emit a null or guessed TP.

For a known entry reference:

```text
LONG:  take_profit = entry + 2 * abs(entry - stop)
SHORT: take_profit = entry - 2 * abs(entry - stop)
```

The producer records the source:

```text
target_source: strategy_target | producer_derived_2r
```

The executor receives a concrete TP and remains strategy-dumb. It validates
the intent and supplied geometry, attaches the supplied SL/TP atomically, and
does not derive a strategy target.

### 4.2 Preconditions

The producer may derive the fallback only when:

- direction is `LONG` or `SHORT`;
- stop is finite and positive;
- entry reference is finite and positive;
- stop is on the correct side of entry;
- derived target is finite, positive, and on the correct side of entry;
- stop distance passes configured minimum and maximum bounds;
- resulting reward:risk is at least the configured minimum, normally `2.0`.

For a market entry with no reliable entry reference, the producer must reject
the intent or use a separately specified execution reference. It must not
derive from zero, the last mark, or an unrecorded estimate.

### 4.3 Executor boundary

The executor must remain safety-authoritative even though it does not derive
the target:

- reject missing or invalid TP;
- validate direction and geometry;
- attach SL and TP in the protected entry request;
- verify the venue reports an active position with both protections;
- contain an active unprotected position;
- journal the supplied target as `original_take_profit`;
- never accept a PM decision that removes or weakens protection.

## 5. PM Input Contract

The sidecar evaluates only executor-confirmed `OPEN` positions from a fresh
snapshot. It must receive the full `position_manager_contexts` entry rather
than reconstructing a reduced position from symbol and PnL alone.

Required position facts, where available:

```json
{
  "position": {
    "position_id": "executor-position-id",
    "exchange_id": "bybit",
    "account_id": "fundamo",
    "symbol": "BTC/USDT:USDT",
    "side": "long",
    "status": "OPEN",
    "quantity": 1.0,
    "entry_price": 100.0,
    "stop_loss": 90.0,
    "original_take_profit": 120.0,
    "current_take_profit": 120.0,
    "mark_price": 114.0,
    "venue_tick_size": 0.1,
    "protection": {
      "sl_active": true,
      "tp_active": true
    }
  }
}
```

Required lifecycle facts:

```json
{
  "lifecycle": {
    "confirmed_1_5r": true,
    "near_tp": false,
    "near_tp_management": {
      "status": "NOT_EXECUTED"
    },
    "management_events": []
  }
}
```

Required strategy facts:

```json
{
  "position_thesis": {
    "strategy": "ema9-continuation-stochrsi-v1",
    "entry_reason": "...",
    "invalidation_conditions": ["..."],
    "target_source": "producer_derived_2r",
    "target_policy": "fixed_full_close"
  }
}
```

The model may also receive bounded market context, strategy parameters,
completed 5m TA, HTF bias, swings, RR, and recent PM decision history. Missing
facts are represented as unknown or omitted. The prompt must explicitly forbid
inference of missing venue or lifecycle facts.

## 6. Semantic Decision Contract

### 6.1 `HOLD`

`HOLD` means no management action. It does not require confidence. It must not:

- reduce quantity;
- modify SL or TP;
- arm near-TP behavior;
- veto another decision;
- reset a prior management event.

The existing executor behavior that overloads `HOLD` to trigger near-TP
reduction must be replaced by the explicit `NEAR_TP` semantic action.

### 6.2 `REDUCE`

`REDUCE` requests an immediate reduce-only reduction. It requires:

- confidence at or above `PM_ACTION_CONFIDENCE`;
- a valid current open position;
- a valid reduction fraction;
- executor-side minimum-size and venue checks.

The executor calculates the requested quantity from current venue quantity. If
the requested reduction or remainder is below venue minimum, the executor may
fall back to a full reduce-only close according to its safety contract.

### 6.3 `EXIT`

`EXIT` requests an immediate reduce-only full close. It requires:

- confidence at or above `PM_ACTION_CONFIDENCE`;
- a valid current open position;
- a current position identity match;
- no executor safety rejection.

`EXIT` does not cancel or weaken the venue's existing SL/TP before the executor
confirms the close.

### 6.4 `NEAR_TP`

`NEAR_TP` is an action-bearing management instruction, not a synonym for
`HOLD`. It means: preserve the runner, and authorize one executor-owned
reduction when the position is within five venue ticks of immutable original
TP.

The decision requires confidence at or above `PM_ACTION_CONFIDENCE`. The
executor may accept the decision only when:

- original TP is known;
- venue tick size is known and positive;
- current venue position is `OPEN`;
- current venue quantity is positive;
- SL and TP protection state is known and valid;
- the universal `1.5R` partial is confirmed when that phase is enabled;
- the near-TP reduction has not already been confirmed.

The executor checks the current venue mark, not the sidecar's old mark:

```text
LONG:  0 <= original_tp - mark <= 5 * tick_size
SHORT: 0 <= mark - original_tp <= 5 * tick_size
```

The decision is valid for five minutes. The executor may arm the near-TP
operation until `valid_until`; if the condition is already true it may execute
immediately. After expiry, the decision cannot arm or execute the operation.

The executor persists the arm and confirmed reduction state. A later PM
decision cannot repeat a confirmed near-TP reduction for the same position
lifecycle.

## 7. LLM Response and Confidence

The strict response shape is:

```json
{
  "action": "hold | reduce | exit | near_tp",
  "confidence": 0.82,
  "reason": "bounded factual reason"
}
```

Conditional validation applies:

| Action | Confidence | Result |
| --- | --- | --- |
| `hold` | optional/ignored | accept as no-op |
| `reduce` | required, finite, threshold met | accept as action |
| `exit` | required, finite, threshold met | accept as action |
| `near_tp` | required, finite, threshold met | accept as action |

The default threshold is `PM_ACTION_CONFIDENCE=0.70`. A response below the
threshold is normalized to `HOLD`, while preserving the original proposed
action, confidence, and rejection reason in audit data. Invalid JSON, unknown
actions, non-finite confidence, and schema violations also normalize to
`HOLD`.

Confidence is an uncalibrated model claim, not a probability. It gates model
authority; it does not replace venue protections, producer admission, or
executor validation.

## 8. Decision Envelope

The executor-facing envelope is:

```json
{
  "schema_version": 1,
  "decision_id": "deterministic-id",
  "exchange_id": "bybit",
  "account_id": "fundamo",
  "position_id": "executor-position-id",
  "symbol": "BTC/USDT:USDT",
  "action": "NEAR_TP",
  "decision_scope": "NEAR_TP",
  "confidence": 0.82,
  "confidence_threshold": 0.70,
  "reduce_fraction": 0.75,
  "issued_at": "2026-08-29T12:00:00Z",
  "valid_until": "2026-08-29T12:05:00Z",
  "reason": "protect profit near original TP",
  "controller": "llm_sidecar"
}
```

`reduce_fraction` is required for `REDUCE` and `NEAR_TP`. It is ignored for
`HOLD` and `EXIT`. `NEAR_TP` is the canonical action spelling in the executor
envelope; the LLM's lower-case semantic output is normalized before delivery.

Decision identity must include:

```text
exchange_id + account_id + position_id + symbol + side
+ action + decision_scope + evaluation_cutoff
```

If the executor exposes position/protection revisions, those revisions must be
included as additional identity and stale-state guards.

## 9. Validity, Deduplication, and Supersession

- PM cadence is five minutes.
- `valid_until` is exactly five minutes after `issued_at`.
- The PM must emit at most one decision per position per evaluation cutoff.
- Decision files are atomically written.
- A persisted decision must be retryable if file delivery fails.
- A terminal executor result must not be retried as a new decision.
- A `REDUCE` decision must not be replayed after confirmed quantity reduction.
- An `EXIT` decision for a flat position is a terminal no-op/rejection, not a
  new order.
- If multiple unprocessed decisions exist for one position, the executor must
  process them in deterministic order and prevent stale actions from causing
  repeated reductions.

Five-minute expiry limits stale exposure but is not a substitute for
supersession and idempotency.

## 10. Failure Behavior

| Failure | PM result | Executor result |
| --- | --- | --- |
| missing LLM credential | `HOLD` | no PM action; SL/TP remain active |
| timeout or provider error | `HOLD` | no PM action; SL/TP remain active |
| malformed response | `HOLD` | no PM action; SL/TP remain active |
| low action confidence | `HOLD` | no PM action |
| stale snapshot | skip cycle | no PM decision |
| unknown position identity | skip/reject | no order |
| missing original TP for `NEAR_TP` | reject action | no reduction |
| missing tick size for `NEAR_TP` | reject action | no reduction |
| invalid protection state | reject action | executor containment policy applies |
| executor decision rejection | durable terminal/retry state | no blind retry |
| PM file write failure | retryable outbox state | no claim of delivery |

LLM failure must never cancel, delay, or suppress executor-native SL/TP,
liquidation containment, or emergency close behavior.

## 11. Persistence Model

The analyst database remains the PM advice observation ledger. It must record at
least:

```text
advice_id
position_id
strategy_id
exchange_id
account_id
symbol
decision_scope
confidence
confidence_threshold
confidence_status
reason
observed_at
evaluation_cutoff
created_at
```

The executor-facing delivery record must separately track:

```text
decision_id
advice_id
status: pending | written | acknowledged | skipped | failed
attempts
next_retry_at
written_at
acknowledged_at
executor_result
```

An advice row without a delivered decision must remain recoverable. A decision
file without an advice row must be quarantined or recorded as an external
decision anomaly.

## 12. Observability

Every PM cycle records:

```text
cycle_id
cutoff
snapshot_age
positions_seen
positions_evaluated
holds
reductions
exits
near_tp_decisions
low_confidence_rejections
stale_skips
delivery_pending
delivery_written
delivery_failed
last_error
```

Every LLM request records sanitized metadata only:

```text
request_id
decision_id
position_id
snapshot_id or snapshot_timestamp
prompt_version
model
started_at
completed_at
latency_ms
raw_action
confidence
normalized_action
confidence_threshold
confidence_status
reason
```

Never log credentials, full prompts, raw sensitive account data, or provider
secrets.

Required counters:

```text
pm_cycles_total{status}
pm_positions_seen_total{profile}
pm_positions_skipped_total{reason}
pm_llm_requests_total{result}
pm_actions_total{action,status}
pm_confidence_rejections_total{action}
pm_near_tp_arms_total{status}
pm_near_tp_reductions_total{status}
pm_decision_delivery_total{status}
pm_decision_expired_total{action}
```

The health snapshot exposes the latest completed cycle, snapshot age, oldest
pending decision age, counts by action, last LLM result, last delivery result,
and last error.

## 13. Producer and Execution Integration Work

Implementation must be split into these seams:

1. Add producer target fallback and `target_source` metadata.
2. Run target construction before geometry/admission validation.
3. Preserve explicit targets unchanged.
4. Add confidence and `NEAR_TP` to PM parsing and persistence.
5. Remove mechanical PM evaluation and `VETO_MECHANICAL_EXIT` emission.
6. Replace overloaded executor `HOLD` near-TP behavior with explicit
   `NEAR_TP` handling.
7. Pass complete executor PM context to the sidecar.
8. Add fresh-snapshot and `OPEN`-only admission.
9. Add durable PM delivery state and atomic file writes.
10. Change PM cadence and decision validity defaults to five minutes.
11. Add executor-side action, expiry, quantity, protection, and one-time-event
    validation.
12. Keep shared intent-bus delivery authoritative and quarantine legacy paths.

No step may enable LLM PM order authority before its executor contract and
paper-path tests pass.

## 14. Rollout

### Phase A: shadow

- PM reads fresh snapshots and emits no executor decisions.
- Record proposed action, confidence, threshold result, and reason.
- Compare proposals with venue TP/SL outcomes and strategy outcomes.
- Measure repeated reductions, false exits, stale contexts, and provider errors.

### Phase B: protected paper execution

- Producer-derived 2R targets are enabled in paper mode.
- Executor validates and attaches supplied targets.
- PM decisions are delivered to paper executor only.
- `NEAR_TP` is tested against venue mark/tick and one-time lifecycle state.

### Phase C: bounded live management

- Enable `REDUCE`, `EXIT`, and `NEAR_TP` only after acceptance criteria pass.
- Keep SL/TP and emergency containment independent.
- Monitor action counts, confidence rejection rate, decision age, and executor
  rejection rate.
- Disable PM delivery without disabling venue protections if anomalies occur.

## 15. Acceptance Criteria

### Producer

1. Explicit strategy target is preserved.
2. Missing target derives exact 2R for long and short with known entry/stop.
3. Missing entry/stop rejects rather than guessing.
4. Derived target is included in the executor intent before validation.
5. `target_source` is persisted and visible in delivery audit.
6. Producer and executor agree on target geometry.

### PM

7. Parser accepts only `hold`, `reduce`, `exit`, and `near_tp`.
8. `HOLD` works without confidence and creates no execution effect.
9. Action-bearing decisions require finite confidence at or above threshold.
10. Low-confidence actions become auditable `HOLD` results.
11. LLM failures become `HOLD` without affecting SL/TP.
12. Mechanical PM evaluation is absent from the live decision path.
13. `NEAR_TP` requires original TP, tick size, open state, and lifecycle gates.
14. `NEAR_TP` uses current venue mark and quantity.
15. `NEAR_TP` cannot repeat after confirmed reduction.
16. Every decision expires exactly five minutes after issuance.
17. One decision is emitted per position and cutoff.

### Executor

18. Executor rejects expired, malformed, unknown-profile, and unknown-position
    decisions.
19. Executor remains strategy-dumb and does not derive TP policy.
20. Executor validates and attaches producer-supplied SL/TP.
21. Ordinary `HOLD` is a no-op.
22. `NEAR_TP` is a distinct action and cannot be encoded as `HOLD`.
23. `REDUCE` and `NEAR_TP` use current venue quantity and reduce-only orders.
24. SL/TP and emergency containment remain independent of PM availability.
25. Venue-flat positions cannot receive a new PM order.

### Delivery and operations

26. PM DB advice and decision-file delivery recover independently.
27. Decision files are atomically written and safely archived.
28. Health reports stale snapshots, pending decision age, and delivery failures.
29. PM failure cannot block orchestrator, gateway, or intent delivery.
30. Shared paths are the executor's paths, not analyst-local copies.
31. Legacy intent delivery is disabled or explicitly labeled compatibility-only.
32. Replayed triggers and decisions are idempotent.

## 16. Current-Code Gaps

The following are known implementation gaps to close before the design is
considered live:

- `src/research_analyst/pm_sidecar.py` currently parses only three actions and
  does not parse confidence.
- The current PM path still evaluates `_mechanical_exit` and emits
  `VETO_MECHANICAL_EXIT`.
- PM validity currently defaults to 30 minutes in runtime configuration.
- PM advice/file persistence is not independently retryable.
- `src/research_analyst/intent_outbox.py` currently passes through missing TP,
  while geometry validation rejects it; producer 2R fallback is absent.
- `bybit-executor` currently requires a concrete TP and does not derive one.
- The executor's existing near-TP behavior is overloaded onto `HOLD` and must
  become explicit `NEAR_TP` behavior.
- Several older documents describe the superseded mechanical-veto contract;
  this specification and `specs/llm-position-sidecar.md` are authoritative for
  the target design.
