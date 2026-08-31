# Fundamo and Propr Intent Fan-Out

## Status

Implementation specification. No implementation is included in this document.

## Goal

Deliver every admitted intent from the four active Fundamo strategy families to
both independent execution accounts:

```text
one admitted analyst event
          |
          +--> shared bus target=bybit -> bybit-executor -> Fundamo account
          |
          +--> shared bus target=propr -> propr-executor -> Propr account
```

The analyst remains the single producer of the strategy thesis. Each executor
owns its account-specific sizing, eligibility, position checks, risk controls,
placement, protection, exits, and execution receipts.

## Scope

In scope:

- Research Analyst fan-out to `target=bybit` and `target=propr`.
- Four strategy IDs currently assigned to Fundamo:
  - `dual-zone-follower-v2`
  - `dual-zone-short-follower-v2`
  - `ema20-pullback-h4-trend-v1`
  - `ema-stack-15m-adx-stochrsi-5m-v1`
- Shared SQLite intent-bus delivery records and receipts.
- Propr consumer compatibility and end-to-end verification.
- Per-target/account observability and idempotency.

Out of scope:

- Changing strategy rules or admission policy.
- Analyst-side sizing or leverage.
- Direct writes to Propr filesystem inboxes.
- Making Fundamo and Propr share position state.
- Changing Propr SDK behavior or editing `propr_sdk`.
- Routing unrelated Hyro strategies to Propr.

## Current repository facts

### Research Analyst

- `intent_outbox.py` builds the executor-facing schema-v1 envelope.
- `intent_bus_publisher.py` currently publishes one `target=bybit` delivery.
- The authoritative bus is `/home/ubuntu/shared/intent-bus/intent_bus.sqlite3`.
- `INTENT_BUS_DB` must remain an explicit absolute path.
- Intent publication is gated by `INTENT_BUS_BYBIT_ENABLED`.
- `execution_adapter.py` has legacy target-aware filesystem delivery, but this
  feature must use the shared bus, not that compatibility path.

### Propr Executor

- `src/propr_executor/intent_bus_consumer.py` already claims only
  `target=propr` deliveries.
- It writes one terminal or retry receipt for every claimed delivery.
- It preserves source allowlists, freshness/drift gates, sizing, protection,
  paper interlock, and venue/account risk behavior.
- It coalesces stale ordinary intents by target, asset, direction, source, and
  strategy/thesis identity.
- Propr’s account identity is executor configuration, not analyst sizing data.

## Delivery contract

### Fan-out identity

One analyst event must produce two independent bus delivery IDs. A single global
delivery ID must not be reused for both targets if the bus schema treats delivery
identity as target-specific.

Recommended identity:

```text
producer event identity = analyst alpha/dedupe identity
delivery identity       = producer identity + ":" + target
```

The event identity remains strategy, asset, direction, and observation based.
Target is added only to delivery identity and delivery records.

### Bybit delivery

The existing schema-v1 envelope is preserved:

- `target=bybit`
- `exchange_id=bybit`
- `account_id=fundamo`
- `asset`, canonical perpetual `symbol`, direction, entry, stop, target
- `entry_valid_until`, observed timestamp, strategy metadata

The four strategy IDs must remain forcibly routed to Fundamo and must not be
overridden by global defaults or caller arguments.

### Propr delivery

The Propr delivery must use the exact payload contract accepted by
`propr_executor.intent_bus_consumer` and its existing processor. The adapter
must explicitly map, not merely relabel, fields:

- `target=propr`
- `source=research-analyst`
- canonical `asset` and executor-compatible `symbol`
- `direction`
- entry price and validity
- stop loss and take profit
- strategy identity in the field consumed by Propr coalescing/journaling
- original analyst delivery/alpha identity for audit correlation

Do not include quantity, risk amount, or leverage. Propr calculates these.

The implementation must first confirm whether Propr expects the analyst
schema-v1 top-level fields or its existing `hints`/schema-v2 shape. If an
adapter is required, define it in the shared producer adapter boundary or the
Research Analyst bus publisher; do not modify the vendored SDK.

## Failure isolation

Fan-out is independently durable:

- Bybit acceptance/rejection must not suppress Propr publication.
- Propr acceptance/rejection must not change Bybit status.
- A transient failure publishing one target must leave only that target
  retryable.
- The analyst must never claim execution success from a bus publication.
- A bus `available` or `claimed` state is not an execution receipt.
- Executor receipts remain the source of truth for accepted, rejected, expired,
  skipped, paper, and retry outcomes.

If one target cannot be built due to a contract error, record a target-specific
delivery failure and preserve the other target’s valid delivery.

## Deduplication and replay

- Reprocessing one alpha event must not create duplicate delivery for either
  target.
- Replaying after a producer crash must be safe before and after bus insertion.
- Propr’s existing coalescing must remain target-local and must not expire the
  corresponding Bybit delivery.
- Fundamo and Propr may independently reject the same thesis because their
  positions differ. That is expected.

## Account and risk boundaries

```text
Analyst: thesis, entry, stop, target, validity, strategy metadata
Admission: freshness, geometry, RR, clash, selection
Bus: durable target-specific handoff
Fundamo executor: Fundamo sizing, caps, existing positions, execution
Propr executor: Propr sizing, caps, existing positions, execution
```

No cross-account position cap is allowed. A Fundamo `position already exists`
rejection must not cause Propr to reject the same intent, and vice versa.

## Configuration

Document and validate separate controls:

- `INTENT_BUS_DB`: required absolute shared-bus path.
- Existing Bybit enable flag, retained for Bybit delivery.
- A separate explicit Propr target enable flag, default off until verified.
- Optional per-target producer delivery retry settings.
- No account credentials or sizing settings in Research Analyst.

The four strategy IDs remain hard-routed to Fundamo for `target=bybit`. Propr
target delivery must identify the Propr executor/account through its own
consumer configuration unless the shared-bus schema requires an explicit
account field.

## Propr-side requirements

Before enabling live Propr fan-out:

1. Confirm Propr’s managed service consumes the same shared bus database.
2. Confirm its consumer is configured for `target=propr`.
3. Confirm the source allowlist accepts `research-analyst`.
4. Confirm the payload adapter passes Propr’s required validation and freshness
   gates.
5. Confirm paper/live mode and account identity explicitly.
6. Confirm receipt rows are written for accepted and rejected deliveries.
7. Confirm journal strategy identity is persisted after venue-confirmed opens.

No Propr code change is required if its existing consumer already accepts the
adapted payload and configuration. If a code change is required, it must be
limited to the consumer contract adapter or tests; do not alter venue risk
logic.

## Observability

Every cycle must make it possible to answer separately:

- How many admitted events were fanned out to each target?
- How many bus deliveries are available, claimed, completed, or retrying per
  target?
- What account/exchange was recorded for each delivery?
- What was the executor receipt status and reason per target?
- Which strategy, asset, direction, and alpha identity produced it?

Required log/metric distinction:

```text
published_bybit != accepted_bybit
published_propr != accepted_propr
```

Never label a bus publication as execution, fill, or acceptance.

## Tests

### Research Analyst unit tests

- One admitted event builds exactly two target deliveries.
- Four strategy IDs remain Fundamo-routed for the Bybit delivery even when
  global routing or caller overrides request another account.
- Propr payload contains all fields required by its consumer.
- No sizing/leverage fields are emitted.
- Target-specific delivery IDs are distinct and deterministic.
- Repeated publication is idempotent per target.
- Failure to build/publish Propr does not suppress Bybit.
- Failure to build/publish Bybit does not suppress Propr.

### Propr unit/contract tests

- Propr claims `target=propr` and never claims `target=bybit`.
- A research-analyst payload passes source and schema validation.
- Malformed, stale, unsupported, capped, and existing-position cases produce
  terminal receipts.
- Retry outcomes remain retryable and do not duplicate venue submission.
- Strategy identity survives into the journal after a confirmed open.

### End-to-end tests

Use a temporary shared SQLite bus and hermetic executor processors:

1. Create one admitted four-strategy event.
2. Publish both target deliveries.
3. Run a fake Bybit consumer and fake Propr consumer independently.
4. Assert each claims only its target delivery.
5. Assert each receives the same thesis identity but separate delivery IDs.
6. Make one consumer reject for position cap and the other accept.
7. Assert both receipts are independent and correctly attributed.
8. Replay the producer and assert no duplicate deliveries.
9. Assert no filesystem legacy inbox was written.
10. Run the real Propr consumer contract test against the adapted payload.

## Rollout

1. Implement and test adapter/fan-out with Propr target disabled.
2. Run shared-bus contract tests against both repositories.
3. Enable Propr target in paper mode only.
4. Verify target-specific receipts and no cross-target duplication.
5. Enable live Propr delivery under its managed service.
6. Monitor accepted/rejected/expired/retry counts by target.
7. Keep the existing Fundamo path unchanged throughout rollout.

## Acceptance criteria

- Every admitted event produces at most one durable delivery per target.
- The four strategies continue routing to Fundamo on the Bybit target.
- Propr receives only `target=propr` deliveries.
- Fundamo and Propr outcomes are independently recorded.
- A rejection on either account does not suppress or mutate the other delivery.
- No direct analyst write occurs to Propr inboxes.
- No executor reports a fill solely because the analyst published to the bus.
- Full repository test suites and cross-repository end-to-end tests pass before
  live enablement.
