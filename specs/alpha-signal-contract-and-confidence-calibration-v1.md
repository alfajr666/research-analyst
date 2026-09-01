# Alpha Signal Contract and Confidence Calibration v1

## Status

The alpha signal contract is locked except for the confidence-generation model.
This specification records the current behavior that must not change while
confidence calibration is developed separately.

Confidence is retained in the internal event schema and audit data, but is
temporarily omitted from public Discord and Telegram alpha messages.

## 1. Scope

This specification covers:

- The immutable alpha event and execution boundaries.
- The public alpha message contract while confidence is withheld.
- A cross-producer confidence calibration architecture.
- Isolation requirements for live five-minute evaluations.

It does not change strategy rules, admission policy, scoring, routing, sizing,
execution, or the position-management sidecar.

## 2. Locked Runtime Contract

### 2.1 Producer flow

```text
completed market cutoff
  -> deterministic strategy plugin
  -> candidate capture
  -> hard admission and clash resolution
  -> selected alpha event
  -> alpha outbox
  -> public message publisher
  -> optional executor intent delivery
```

The strategy plugin remains the producer of the market thesis. The publisher
formats and delivers the thesis; it does not create or improve it.

### 2.2 Point-in-time behavior

- Plugins use finalized bars only.
- The evaluation cutoff remains the event's temporal boundary.
- Candidates retain the feature snapshot available at that cutoff.
- Event identity remains deterministic from strategy, asset, direction, and
  observed timestamp.
- Event validity remains controlled by `valid_until`.

### 2.3 Event fields that are locked

The following remain unchanged:

- `schema_version`
- `alpha_id` and `dedupe_key`
- `strategy_id` and plugin version
- `asset` and `direction`
- `setup_class` and `phase`
- `observed_at`, `valid_until`, and `horizon_minutes`
- `entry_condition` and entry price
- `invalidation_price`
- `targets`
- `feature_snapshot`
- `data_purity`, `price_source`, and source evidence metadata

The internal `confidence` and `confidence_status` fields also remain in the
event schema for audit and future calibration. Their generation is the only
unlocked part of this contract.

### 2.4 Admission and selection

Every strategy candidate continues through the existing pipeline:

1. Capture in the raw candidate ledger.
2. Apply symbol/account policy.
3. Apply hard admission checks:
   - finite positive prices;
   - directional stop/target geometry;
   - minimum reward/risk;
   - stop-distance bounds;
   - future expiry;
   - fresh market data;
   - valid candidate identity.
4. Calculate the explainable soft score.
5. Resolve same-symbol candidates and opposite-direction clashes.
6. Write only selected candidates to the alpha outbox.

The score remains a ranking mechanism. It is not a probability, hard gate,
sizing input, or execution authorization.

Confidence must not become an admission gate or a replacement for clash
resolution.

### 2.5 Execution boundary

The executor continues to own:

- Venue and account mapping.
- Order placement and lifecycle.
- Sizing, leverage, and portfolio risk.
- Fill and execution receipts.

Confidence, calibrated or otherwise, must not alter an executor intent's:

- Entry, stop, target, or validity window.
- Direction or strategy identity.
- Venue, account, or symbol mapping.
- Quantity, risk amount, leverage, or order behavior.

The shared intent bus remains an execution handoff only. It is not the
calibration store and must not be changed to become one.

## 3. Public Message Contract

### 3.1 Temporary confidence policy

Public alpha messages must not display:

- `confidence`;
- `confidence_status`;
- a derived confidence percentage;
- an LLM confidence percentage.

The internal fields remain persisted. Removing them from the message does not
remove them from the event schema or audit trail.

### 3.2 Required message content

Public alpha messages must show only meaningful, event-backed information:

```md
**ALPHA SIGNAL · {ASSET} · {DIRECTION}**

**Strategy:** {human-readable strategy}
**Setup:** {human-readable setup} · {phase}

**Trade plan**
- Entry: `{price}` {entry condition}
- Invalidation: `{price}`
- Target(s): `{prices}`

**Validity**
- Observed: `{timestamp}`
- Valid until: `{timestamp}`

_Execution and fills are not confirmed._
```

Future evidence and risk sections may be added only when their values are
actually present in the immutable event snapshot. The formatter must not infer
or fabricate missing evidence.

### 3.3 Human-readable labels

The message must not use a generic fallback that misclassifies a strategy.

At minimum:

- `dual_zone_follower` -> `Dual-zone trend pullback`;
- `dual_zone_short_follower` -> `Dual-zone trend pullback`;
- `continuation_*` -> `Continuation`;
- `accumulation_base` -> `Accumulation base`;
- `liquidity_reversal` -> `Liquidity reversal`;
- `impulse_ignition` or `squeeze_ignition` -> `Impulse ignition`;
- unknown setup classes -> a neutral `Strategy setup` label, not `Impulse ignition`.

The internal `strategy_id` remains visible so the exact producer is always
identifiable.

## 4. Cross-Producer Calibration Architecture

### 4.1 Shared data, not shared intent bus

Calibration must support events from multiple producers, but it must not use
the executor intent bus as its primary data source.

The intent bus is unsuitable as the calibration source because it can omit:

- rejected candidates;
- suppressed candidates;
- the complete point-in-time feature snapshot;
- candidates that never reached execution;
- the distinction between signal outcome and order-fill outcome.

Instead, each producer publishes or exposes the same neutral calibration event
contract to a dedicated calibration ledger. The ledger is shared analytically,
not operationally.

```text
Producer A event ledger ----\
                            -> calibration ledger -> calibrator -> model registry
Producer B event ledger ----/

Intent bus ----------------------> optional fill/receipt correlation only
Market bars ---------------------> outcome labeling only
```

### 4.2 Ownership and process isolation

The calibrator is a separate scheduled process or command. It must:

- Read producer ledgers through read-only connections.
- Read completed market bars through a read-only connection.
- Never write to the live market database.
- Never write to the orchestrator-owned analyst database.
- Own its separate calibration database or artifact directory.
- Never run inside the five-minute orchestrator call stack.
- Never make a network or LLM call on the evaluation path.

The recommended storage is:

```text
calibration.sqlite3
  candidate_outcomes
  calibration_runs
  calibration_metrics
  model_registry
```

The calibrator may also write versioned model files for low-latency runtime
lookup. Model publication must use an atomic temporary-file rename, preserving
the last known-good model if a run fails.

### 4.3 Producer identity

Every calibration record must preserve:

- `producer_id`;
- `strategy_id`;
- strategy/plugin version;
- `alpha_id` or source event ID;
- setup class;
- direction;
- phase/channel;
- event timestamp;
- source data snapshot.

Events from different producers or different strategy hypotheses must not be
pooled into one calibration model unless an explicit research decision proves
that they represent the same population.

The initial models should be segmented at least by:

```text
strategy_id + direction + setup/channel
```

### 4.4 Outcome labeling

The outcome builder processes only matured events, after the event validity
window and evaluation horizon have elapsed.

For each event, it records:

- entry-fill status;
- target-first, invalidation-first, or timeout result;
- elapsed time;
- maximum favorable excursion;
- maximum adverse excursion;
- estimated fees, spread, slippage, and funding;
- market regime and liquidity tier.

The default trade outcome is:

- `win`: target reached before invalidation;
- `loss`: invalidation reached before target;
- `timeout`: neither barrier reached before the defined horizon;
- `unfilled`: limit entry was not reached under the fill policy.

If both barriers occur within one candle and ordering cannot be observed, the
label must use the conservative invalidation-first rule.

Calibration must state whether it estimates:

1. trade success conditional on entry fill; or
2. end-to-end success including fill probability.

These must not be mixed. The recommended primary confidence is trade success
conditional on a fill, with fill likelihood reported separately if needed.

### 4.5 Training and validation

The calibrator must use chronological walk-forward evaluation:

```text
older events -> fit
next events   -> calibrate
newer events  -> out-of-sample test
```

It must not use future features, future outcomes, or post-event execution state
as model inputs.

The initial calibration model should be deliberately simple:

- empirical smoothed rates by strategy/channel; or
- logistic or isotonic calibration of a deterministic raw quality score.

Required evaluation outputs:

- reliability diagram;
- Brier score;
- log loss;
- calibration error;
- sample count per bucket;
- realized win rate per confidence bucket;
- results after conservative costs.

### 4.6 Promotion policy

A model may be promoted only when:

- its test period is strictly out of sample;
- sample counts meet the configured minimum;
- confidence buckets have acceptable calibration error;
- results are stable across more than one market regime;
- the model beats or meaningfully improves the neutral baseline;
- the model version and training window are recorded.

Before promotion, public messages show no confidence. Internally, the existing
neutral numeric placeholder may remain for schema compatibility, but it must be
marked as pending and must not be described as a probability.

After promotion, the event may use:

```text
confidence_status = calibrated_v1
```

Only then may a numeric confidence be shown publicly, together with the model
version and enough sample context to prevent false precision.

## 5. LLM Boundary

An LLM is not a confidence calibrator.

LLM review may later provide separate advisory fields such as:

- `support`;
- `caution`;
- `oppose`;
- rationale;
- counter-evidence;
- data gaps;
- delivery priority.

LLM output must not modify deterministic confidence, promote a calibration
version, change an event, suppress admission, or alter an executor intent.

## 6. Failure Behavior

- A calibrator failure must not fail or delay live evaluation.
- A stale or missing calibration model must not change admission or execution.
- A corrupt model artifact must be rejected and the last known-good artifact
  retained.
- No public confidence is shown while confidence is pending calibration.
- Intent-bus and executor failures remain separate from calibration failures.

## 7. Acceptance Criteria

- The live five-minute evaluation path has no dependency on the calibrator
  process, calibration database, or LLM.
- Both producers can contribute events without sharing executor-bus ownership.
- Candidate outcomes are reproducible from point-in-time event and market data.
- Strategy, channel, direction, and producer identity remain queryable.
- Public alpha messages contain no confidence field or confidence percentage.
- Internal confidence remains available for audit and future calibration.
- No calibrated value can change entry, invalidation, target, expiry, admission,
  routing, sizing, or intent receipt.
- A promoted model has a version, training window, test window, sample count,
  and calibration metrics.

## 8. Explicitly Unlocked Decision

The following remains intentionally undecided:

- the exact definition of confidence;
- whether it is conditional on fill or end-to-end;
- the minimum sample size;
- the raw score inputs;
- the calibration algorithm;
- the promotion thresholds;
- whether and how calibrated confidence is displayed publicly.

No LLM-generated confidence is authorized by this specification.
