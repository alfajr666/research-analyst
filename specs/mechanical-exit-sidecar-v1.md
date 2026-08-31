# Mechanical Exit and LLM Management Sidecar v1

## Status

Superseded by the locked LLM-only PM contract in
`specs/llm-position-sidecar.md`. Retained as historical design context; its
mechanical-veto behavior is not part of the current target architecture.

Implementation specification for a universal position-management sidecar shared
by all research strategies and routed positions. The sidecar runs in
`research-analyst`; the executor remains authoritative for account state,
quantity, order placement, protection, and venue confirmation.

## Goal

Evaluate deterministic, strategy-declared exit policies on completed market bars
and combine them with optional LLM management. The LLM may veto a vetoable
mechanical exit so a reduced-size position can be held longer. It may never
veto hard safety protections.

```text
executor snapshots
        -> mechanical policy evaluation
        -> optional LLM review
        -> durable decision outbox
        -> bybit executor
```

Default deployment scope:

```text
exchange: bybit
account: fundamo
universe: 97 static symbols
management cadence: completed 1m cutoff
```

## Responsibilities

### Research analyst owns

- Mechanical indicator calculations
- Strategy exit-policy selection
- Mechanical trigger events
- LLM review requests and responses
- Veto, reduce, and exit recommendations
- Decision provenance and observability
- Durable decision outbox delivery

### Executor owns

- Snapshot truth and live position state
- Position/protection revisions
- Quantity and venue precision
- Reduce-only execution
- Stop protection
- Entry and order policy
- Venue confirmation
- Duplicate rejection
- Emergency close

## Exit Policy Contract

Every managed strategy declares an exit policy:

```text
policy_id
strategy_id
version
evaluation_timeframe
required_features
mechanical_exit(position, context) -> ExitTrigger | None
veto_allowed
veto_window_minutes
```

Policies must be deterministic and side-aware. They must not call the LLM,
inspect executor internals, or place orders.

## Mechanical Trigger

When a policy fires, persist a trigger containing:

```text
trigger_event_id
policy_id
policy_version
strategy_id
exchange_id
account_id
position_id
position_revision
protection_revision
symbol
side
quantity_at_evaluation
entry_price
mark_price
triggered_at
evaluation_cutoff
rule_name
rule_inputs_json
veto_allowed
status
```

Allowed trigger states:

```text
TRIGGERED
AWAITING_LLM
VETOED
APPROVED
REDUCED
EXIT_SUBMITTED
COMPLETED
STALE
FAILED
```

Persist the trigger before requesting or acting on LLM output.

## Decision Precedence

The sidecar evaluates in this order:

1. Hard safety conditions remain non-vetoable.
2. Mechanical policy evaluates the current completed cutoff.
3. If no mechanical trigger exists, the LLM may issue discretionary `HOLD`,
   `REDUCE`, or `EXIT` according to the normal policy.
4. If a mechanical trigger exists and veto is allowed, the LLM may return
   `VETO_MECHANICAL_EXIT`, `REDUCE`, or `EXIT`.
5. If the LLM is unavailable, malformed, stale, or times out, the mechanical
   exit is approved; it must not be held because of LLM failure.
6. A valid veto creates a reduced-size management state and expires at the
   configured veto deadline.

The LLM cannot veto:

- Stop-loss protection
- Liquidation or margin protection
- Emergency close
- Account risk-limit actions
- Protection-loss responses

## Reduced-Size Management

When the LLM vetoes a mechanical exit or performs an early reduction:

```text
management_mode: llm_managed
remaining_quantity: executor-confirmed live quantity
mechanical_trigger_id: preserved
veto_deadline: bounded timestamp
```

The executor must enforce reduced-size policy from live quantity. The analyst
must never calculate final quantity or override venue minimums. A reduction
that cannot leave a valid remainder may fall back to full close according to the
executor contract.

## PM Decision Contract

Extend the existing PMDecision envelope with:

```json
{
  "schema_version": 1,
  "decision_id": "deterministic-id",
  "exchange_id": "bybit",
  "account_id": "fundamo",
  "position_id": "executor-position-id",
  "symbol": "BTC/USDT:USDT",
  "action": "VETO_MECHANICAL_EXIT",
  "decision_scope": "MECHANICAL_VETO",
  "trigger_event_id": "mechanical-trigger-id",
  "position_revision": 12,
  "protection_revision": 4,
  "reduce_fraction": 0.5,
  "issued_at": "2026-08-29T12:00:00Z",
  "valid_until": "2026-08-29T12:05:00Z",
  "reason": "TA exit triggered; holding reduced remainder"
}
```

Supported actions:

```text
HOLD
REDUCE
EXIT
VETO_MECHANICAL_EXIT
```

`VETO_MECHANICAL_EXIT` is valid only when the referenced trigger exists,
`veto_allowed` is true, and position/protection revisions match. A normal
`HOLD` must not silently veto a mechanical trigger.

## Decision Identity and Delivery

Decision identity must include:

```text
account_id + position_id + position_revision + protection_revision
+ trigger_event_id + action + evaluation_cutoff
```

The analyst persists a decision outbox record with:

```text
decision_id
trigger_event_id
status: pending | written | acknowledged | skipped | failed
attempts
next_retry_at
written_at
acknowledged_at
executor_receipt
```

Database persistence and file delivery must be recoverable. A persisted advice
without a decision file must be retried or reconciled.

## Stale-State Rules

Do not evaluate a position when:

- Snapshot is stale beyond the configured threshold.
- Position is not `OPEN`.
- Position revision is missing.
- The referenced strategy or policy is unknown.
- Required 5m or higher-timeframe data is unavailable.

Do not fall back silently from a stale/unreadable executor snapshot to an old
local position row. Record the reason and skip the cycle.

## Logging and Observability

Every cycle must produce structured logs for each position:

```text
mechanical_exit_evaluation
  decision_id/cycle_id
  policy_id/version
  strategy_id
  exchange_id/account_id
  position_id/revision
  symbol/side
  cutoff/evaluated_at
  required_features_status
  rule_result: triggered | not_triggered | skipped | failed
  rule_name
  rule_inputs
  trigger_event_id
```

When the LLM is called, log:

```text
llm_exit_review
  request_id
  trigger_event_id
  position_id/revision
  prompt_version
  model
  started_at/completed_at
  latency_ms
  result: hold | reduce | exit | veto_mechanical_exit | timeout | error | invalid
  reason
```

When a decision is produced, log:

```text
position_management_decision
  decision_id
  trigger_event_id
  action
  decision_scope
  source: mechanical | llm | combined
  veto_applied
  reduced_size_mode
  valid_until
  delivery_status
```

Never log API keys, secrets, full prompts, or sensitive account credentials.

Required aggregate metrics:

```text
mechanical_evaluations_total{strategy,policy,result}
mechanical_triggers_total{strategy,policy,side}
mechanical_trigger_skips_total{reason}
llm_reviews_total{result}
llm_vetoes_total{strategy,policy}
llm_timeouts_total
management_decisions_total{action,source,status}
decision_delivery_total{status}
stale_position_skips_total{reason}
```

The health snapshot must expose the latest cycle, evaluated position count,
mechanical trigger count, LLM review count, veto count, decision delivery
counts, oldest pending decision age, and last error.

## Failure Behavior

- Mechanical evaluator failure: log and fail closed for that position.
- LLM failure before a mechanical trigger: discretionary action defaults to
  `HOLD`.
- LLM failure after a mechanical trigger: approve the mechanical exit.
- Decision file write failure: retain retryable outbox state.
- Executor rejection: record receipt and do not retry terminal rejection.
- Stale revision: reject the decision and re-evaluate on the next cutoff.

## Required Tests

1. Mechanical policy evaluates once per completed cutoff.
2. Trigger persistence is idempotent.
3. LLM veto is accepted only for vetoable triggers.
4. LLM veto cannot override a hard stop or emergency action.
5. LLM failure after a trigger approves mechanical exit.
6. A veto expires and re-enters review on the next cutoff.
7. Reduced quantity is based on current executor quantity.
8. Position/protection revision mismatch rejects stale decisions.
9. DB advice and decision-file delivery recover independently.
10. Logs and metrics include mechanical, LLM, combined, and delivery outcomes.
