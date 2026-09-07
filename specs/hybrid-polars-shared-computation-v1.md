# Hybrid Polars Shared Computation v1

## 1. Status

This document locks the design for the next computation architecture. It is a
design specification only. It does not authorize implementation, configuration
changes, database migrations, service restarts, or strategy-version changes.

Status: locked for implementation planning.

This specification composes and makes normative the existing contracts in:

- `specs/hybrid-htf-engine-v1.md`
- `specs/polars-computation-migration-v1.md`
- `specs/regime-history-bootstrap-v2.md`
- `specs/structural-sl-admission-v2.md`
- `specs/reversal-regime-gate-v1.md`
- `SPEC_DATABASE_RETENTION.md`

Where this document is more specific, this document governs the shared
computation and strategy-evaluation design.

## 2. Decision Summary

The system will use one shared, cutoff-bound computation context for each
evaluation cutoff. The context loads each required asset/timeframe frame once,
computes each requested numerical feature once, and shares immutable Polars
frames with every strategy, regime calculation, structural calculation, and
admission stage that can reuse them.

The market data path is:

```text
Bybit REST completed 1h/4h history
  -> regime worker direct-history cache
  -> immutable hybrid handoff

Bybit WS completed 5m observations
  -> market.sqlite3
  -> canonical completed 5m tail
  -> Polars 1h/4h resampling after the handoff

shared hybrid computation context
  -> strategy feature frames
  -> regime numerical inputs
  -> structural numerical inputs
  -> candidate evaluation
  -> admission and clash resolution
```

Numerical market computation will use Polars. Python will remain responsible
for orchestration, source and cutoff policy, state machines, candidate policy,
admission proofs, provenance, persistence, and delivery.

The design explicitly prohibits each strategy from independently loading bars,
resampling HTF data, or recomputing the same indicators from scratch during one
evaluation.

## 3. Goals

- Use direct REST 1h/4h history as a reliable warmup seed.
- Use completed 5m WS data as the canonical live tail for 1h/4h frames.
- Reuse frames and computations across all strategies at one cutoff.
- Reuse valid incremental computation state across sequential cutoffs.
- Move numerical strategy work to shared Polars kernels and feature frames.
- Preserve exact cutoff, warmup, lookahead, source, and provenance semantics.
- Reduce SQLite reads, Polars allocations, Python object churn, and repeated
  indicator calculations.
- Keep durable SQLite state limited to required audit and operational records.
- Make every optimization observable and parity-testable.

## 4. Non-Goals

- This design does not change strategy logic, thresholds, strategy IDs, or
  plugin versions by itself.
- This design does not replace SQLite with Polars, DuckDB, or another database.
- This design does not persist every transient feature column.
- This design does not make REST calls from strategies or the orchestrator.
- This design does not make the analyst place orders or own executor state.
- This design does not force sequential state machines into opaque vectorized
  expressions.
- This design does not make `VACUUM` run from a live worker.

## 5. Ownership And Write Boundaries

The existing database ownership contract remains mandatory:

| Store | Writer | Readers |
| --- | --- | --- |
| `market.sqlite3` | WS gateway | orchestrator, regime worker |
| `regime.sqlite3` | regime-session worker | orchestrator, hybrid computation, admission |
| `analyst.sqlite3` | orchestrator-owned ledger | publisher, research readers |
| shared intent bus | shared-bus publisher | executor |

The computation layer is read-only with respect to all databases. It must not
open external market APIs, write market observations, write regime history, or
write analyst ledgers.

Position management is owned by the separate standalone-llm-pm repository. It
does not share this computation module or the analyst databases.

## 6. Cutoff Model

Every computation context has two explicit cutoffs:

- `evaluation_cutoff`: the trigger cutoff authoritative for candidate time,
  freshness, expiry, and replay.
- `htf_cutoff`: the latest completed canonical 5m boundary at or before the
  evaluation cutoff, used only for hybrid 1h/4h frames.

Examples:

```text
evaluation at 00:04 -> htf_cutoff 00:00
evaluation at 00:05 -> htf_cutoff 00:05
```

No calculation may silently substitute `now` for either cutoff.

The computation context is immutable after construction. A new cutoff receives
a new logical context, even when it can reuse validated computation state from
the preceding cutoff.

## 7. Hybrid HTF Data Contract

### 7.1 Direct seed

The regime worker fetches completed Bybit linear-perpetual 1h/4h history and
stores it in `regime.sqlite3`. It remains the only writer of:

- `regime_1h_bars`
- `regime_4h_bars`

The direct seed must satisfy the existing readiness contract, including the
ADX warmup requirement and configured seed depth. Missing, malformed, stale,
forming, conflicting, or gapped direct history is not silently repaired by
fabricating bars.

### 7.2 Handoff

For each asset and timeframe, the computation layer determines a handoff point
from the validated direct seed. The merged source ranges are:

```text
direct seed:      source_end <= handoff_at
canonical tail:   source_end >  handoff_at
                  and source_end <= htf_cutoff
```

The handoff must be represented in computation provenance, including the direct
bar IDs and canonical source observation IDs used on each side.

### 7.3 Canonical tail

The canonical tail is built only from completed 5m observations committed by
the WS gateway. The computation layer resamples the tail into 1h and 4h bars
using Polars, then applies Python completeness and cutoff validation.

The resampler must preserve:

- end-stamped candle semantics;
- exact boundary normalization;
- open-first, high-max, low-min, close-last aggregation;
- volume sum and final auxiliary fields;
- source provenance and data purity;
- required bucket completeness;
- no future or forming bars.

An 1h bar requires 12 completed 5m bars. A 4h bar requires 48 completed 5m
bars. Partial buckets are not emitted as completed HTF bars.

### 7.4 Failure modes

The affected frame fails closed on:

- missing direct seed when hybrid enforce mode requires it;
- missing canonical tail bars;
- duplicate bars that cannot be normalized deterministically;
- conflicting direct and canonical rows;
- malformed OHLCV values;
- gaps or incomplete buckets;
- future or forming bars;
- handoff or cutoff mismatch;
- indicator state that cannot be continued with a validated prefix.

In hybrid shadow mode, a missing direct seed may expose an observable
`canonical_only` fallback according to the existing rollout contract. In hybrid
enforce mode, the affected frame is unavailable rather than silently falling
back.

## 8. Shared Computation Module

The implementation will provide one deep shared computation module at the seam
between database loading and strategy policy. Its external interface should be
small; callers request a validated frame or feature set rather than managing
database queries, resampling, indicator warmup, or cache keys themselves.

Conceptually, the interface provides:

```text
EvaluationContext(cutoff, universe, source_contract)

context.frame(asset, interval, purpose, lookback)
context.features(asset, interval, feature_spec)
context.htf(asset, interval)
context.regime_inputs(asset)
context.structural_inputs(asset)
context.provenance(asset, interval)
```

The exact Python names are an implementation decision. The following behavior
is not optional:

- all returned market and feature frames are typed Polars frames;
- all frames are cutoff-bound;
- all frames are read-only by convention after construction;
- all frames carry enough provenance for the caller's audit contract;
- a request with the same valid key returns the same logical computation;
- invalidation is explicit rather than implicit or time-based guesswork;
- callers do not call `load_bars_for_interval` independently once inside the
  shared evaluation seam.

## 9. Computation Reuse

Reuse has two levels.

### 9.1 Level 1: per-cutoff reuse

All strategies in one evaluation share one context. The following are computed
once per asset, interval, cutoff, and feature contract:

- source frame loading;
- hybrid HTF construction;
- completeness and freshness assessment;
- resampling;
- EMA, RSI, ATR, ADX/DMI, StochRSI, Bollinger, VWMA, and volatility;
- rolling extrema, candle geometry, and pivot candidates;
- vectorizable FVG/order-block candidates;
- regime numerical inputs;
- structural ATR and bar-validity columns.

Strategies declare their required frames and feature specifications before
execution. The context unions compatible requirements and computes shared
features once. A strategy must not recompute a feature under a different local
name when the numerical contract is identical.

### 9.2 Level 2: sequential-cutoff reuse

The orchestrator maintains a bounded rolling computation cache across adjacent
completed cutoffs. It may reuse a prior frame or indicator prefix only when all
of the following hold:

- the new cutoff is later than the cached cutoff;
- the source feed identity and source contract are unchanged;
- the existing prefix is immutable;
- new observations extend the prefix without gaps or conflicting duplicates;
- the hybrid handoff is unchanged;
- the feature contract and indicator parameters are unchanged;
- the cached state includes enough warmup and recursive indicator state;
- the new result is equivalent to recomputing from the validated prefix.

If any condition fails, the module invalidates the affected cache entry and
rebuilds it from the bounded validated source window. Cache reuse is an
optimization, never a correctness dependency.

The cache must be bounded by asset, interval, and maximum lookback. It must not
retain unbounded historical Polars frames or provenance lists.

### 9.3 Cache identity

Every computation cache key includes at least:

```text
feed_id
asset
interval
evaluation_cutoff
htf_cutoff, when applicable
handoff_at, when applicable
source high-water mark
feature contract version
indicator parameters
lookback contract
```

No cache entry may be reused across different source versions, handoffs,
indicator contracts, or cutoffs without an explicit parity-safe equivalence.

## 10. Polars Numerical Contract

Polars is the implementation standard for numerical market computation. The
shared kernels retain the repository's explicit contracts for:

- SMA-seeded EMA;
- Wilder RSI and zero-loss behavior;
- Wilder ATR and true-range definition;
- StochRSI null warmup and zero-denominator behavior;
- Bollinger population standard deviation where required;
- each ADX/DMI warmup and smoothing contract;
- rolling extrema and confirmed pivot alignment;
- realized volatility and log-return windows;
- source and cutoff alignment.

Strategy-local numerical implementations may not be replaced by a generalized
kernel merely because the formula appears equivalent. The implementation must
first establish fixture and randomized parity for null positions, warmup,
finite values, and tolerance.

The public strategy-facing result may contain scalar values or a final row, but
the numerical work must occur in Polars before that boundary. Repeated
conversion of the same full column to Python lists is prohibited unless the
algorithm is a deliberately sequential state machine or the conversion is
limited to a small final slice.

## 11. Strategy Interface

Every active strategy will be migrated to consume the shared computation
context rather than opening its own market connection or loading its own bars.

A strategy declares:

- strategy ID and version;
- evaluation cadence;
- required asset universe;
- required intervals;
- required feature specifications;
- required warmup and lookback;
- whether it has sequential state behavior;
- output candidate contract.

A strategy receives:

- its cutoff-bound shared frames;
- its cutoff-bound feature frames;
- the relevant regime/family scope;
- the strategy's immutable configuration;
- a small state-machine input where required.

A strategy returns zero or more candidate records. It does not:

- open a database connection;
- call Bybit REST or WS APIs;
- resample the same source bars independently;
- write candidates directly;
- perform admission or clash resolution;
- mutate shared frames;
- use future rows beyond its declared confirmation point.

### 11.1 Numerical strategy logic

All vectorizable strategy calculations move into Polars expressions or shared
Polars kernels. This includes indicator columns, rolling windows, candle
geometry, current-row masks, and candidate setup columns.

The final candidate construction may be Python because it creates a small,
auditable record and attaches strategy metadata. It must consume the final
validated Polars row rather than recompute values from raw lists.

### 11.2 Stateful strategies

Stateful strategies use Polars for their numerical inputs and candidate
features. Python remains allowed for explicitly sequential behavior:

- setup arming and invalidation;
- re-arm rules;
- expiry windows;
- event ordering;
- confirmation-point enforcement;
- mutable state transitions.

Any vectorization of a state machine requires replay parity proving identical
event ordering, reset behavior, expiry, and lookahead behavior. No state-machine
rewrite is accepted solely because it appears shorter or more vectorized.

## 12. Regime, Scoring, And Admission

### 12.1 Regime scoring

Regime numerical inputs use shared Polars frames and kernels:

- 1h/4h ADX/DMI;
- 5m realized volatility;
- reversal-gate RSI, pivots, and OLS inputs;
- source and readiness columns.

Python remains responsible for:

- insufficient-data fail-closed decisions;
- family weighting;
- hysteresis;
- reversal ambiguity rules;
- gate persistence;
- routing decisions.

The 1h ADX series used by the regime score and reversal gate must be computed
once and reused, not recalculated independently.

### 12.2 Candidate scoring

Candidate numerical components may be materialized into a small Polars frame:

- finite-price checks;
- reward/risk;
- stop distance;
- ATR multiples;
- freshness values;
- bounded score components;
- score totals.

The final score result must preserve the existing missing-context semantics and
rounding contract. Python remains responsible for candidate identity, audit
reasons, priority tie-breaks, and deterministic clash resolution.

### 12.3 Admission

Admission uses a two-stage design:

1. Polars computes reusable numerical structural inputs, including ATR,
   bar-validity columns, distance/buffer values, and candidate numeric checks.
2. Python applies policy and emits the authoritative admission proof, including
   selected zone, timeframe priority, cutoff checks, source bar IDs, failure
   reasons, fingerprints, and deterministic status.

The complete admission proof remains Python-owned because its semantics are
policy-heavy and audit-facing. Polars must not hide a fail-closed decision in a
generic expression or silently coerce missing values.

Structural context is built only for assets that emitted candidates. Full
universe feature computation remains separate from candidate-owned structural
admission context.

## 13. Persistence Rules

Polars frames and computation cache entries are transient. They are not a new
durable database layer.

Persist only what is required for replay and audit:

- source observations in the owning market database;
- direct regime bars in the regime database;
- cutoff and pipeline state;
- raw candidates and admission status;
- alpha events and delivery state;
- bounded feature summaries;
- bounded provenance references;
- required research and PM audit records.

Per-zone analyst rows remain disabled. Recomputable zone context is retained in
the shared computation context and admission proof rather than written once per
zone per asset per cutoff.

Provenance lists must be bounded at write time. The existing regime provenance
limit remains authoritative.

## 14. Resource And Process Requirements

The implementation must address computation reuse and process duplication as
one design, not as independent optimizations.

### 14.1 Gateway

- Maintain one market database writer connection.
- Serialize backfill, warmup refresh, streaming writes, resampling, and
  retention through that writer or an explicit writer-owned queue.
- Flush backfill rows in bounded batches.
- Resample only newly completed 5m boundaries after the first validated warmup,
  while retaining a bounded repair window for late or corrected observations.
- Do not rebuild and upsert a full 24-hour 5m window every minute unless the
  repair path is explicitly active.

### 14.2 Orchestrator

- Build one shared computation context per cutoff.
- Remove duplicate full-universe HTF loading and zone detection.
- Cache coverage checks per asset/interval/cutoff.
- Batch raw-signal and status writes per cutoff where transactional semantics
  permit.
- Initialize the analyst schema outside the hot evaluation path where safe.
- Continue to separate pipeline success from publisher failure.

### 14.3 Regime worker

- Reuse direct-history frames and ADX results within a cycle.
- Recompute reversal HTF inputs only when a new completed 1h bar changes them.
- Keep 5m volatility updates on the completed 5m cadence.
- Persist immutable score and gate decisions through the regime writer only.

### 14.4 Process inventory

The four managed analyst processes are:

- symbol rotation;
- WS gateway;
- regime session;
- orchestrator;
- The standalone-llm-pm process is managed from its own repository.

## 15. Retention And Compaction

The computation redesign does not change retention ownership.

Recomputable feature data may be aggressively bounded. Operational intent,
delivery, executor handoff, active position, queue, and retry state must be
preserved until their lifecycle completes.

Before changing TTLs:

- measure row counts and payload widths by table;
- correct retention predicates for every active state;
- resolve analyst/PM writer ownership;
- confirm replay and audit horizon;
- run offline compaction only through the existing stop-and-verify workflow.

Online pruning remains batched deletion with passive checkpointing. `VACUUM`
remains offline-only and must never run concurrently with a database writer.

## 16. Observability Requirements

Each evaluation records timing and size metrics for:

- source frame load;
- hybrid seed/tail merge;
- resampling;
- cache hit/miss and invalidation;
- shared feature construction;
- per-strategy policy execution;
- regime computation;
- structural context;
- numerical admission preflight;
- Python admission/proof;
- persistence;
- publishing.

Metrics must include asset count, frame row counts, feature counts, cache reuse,
and peak or estimated transient memory where available.

The implementation must make it possible to distinguish:

- slow database access;
- repeated computation;
- Polars computation;
- Python policy/state work;
- SQLite commit contention;
- external REST or LLM latency.

## 17. Parity And Safety Gates

No migrated strategy or computation family changes production behavior until
shadow replay proves zero unexplained differences in:

- candidate IDs;
- candidate direction and timestamps;
- entries, stops, and targets;
- feature values and null positions;
- regime scores and family gates;
- selected zones and ATR values;
- admission reasons and proofs;
- clash selections;
- alpha event IDs and delivery targets.

Required test layers:

- shared indicator fixture parity;
- randomized numerical parity;
- hybrid seed/tail parity;
- cutoff and lookahead tests;
- gap, duplicate, and malformed-bar tests;
- strategy replay tests;
- structural admission E2E tests;
- regime-session E2E tests;
- PM context parity tests;
- full repository tests and compile checks.

Every cache optimization must have a cache-disabled reference path for tests.
Every incremental result must match a bounded full recomputation on the same
validated source data.

## 18. Rollout Phases

### Phase 0: baseline

Measure current per-cutoff work, database reads, frame sizes, indicator calls,
Python conversions, cache opportunities, CPU, RAM, and SQLite lock waits.

No behavior change.

### Phase 1: shared context seam

Introduce the shared computation interface and route one evaluation through it
without changing numerical implementations or strategy outputs.

No strategy version changes.

### Phase 2: hybrid reuse

Centralize REST-seed plus 5m-tail frame construction, provenance, completeness,
and per-cutoff reuse. Add sequential-cutoff cache reuse with invalidation.

### Phase 3: shared Polars features

Move all compatible shared indicators and numerical columns behind the shared
feature registry. Preserve adapters for strategies not yet migrated.

### Phase 4: strategy migration

Migrate simple strategies first, then stateful strategies. Keep state-machine
policy explicit and independently parity-tested.

### Phase 5: scorer and admission numerics

Move reusable numeric prechecks and score components to Polars. Keep final
admission proofs and clash policy in Python.

### Phase 6: resource cleanup

Make gateway resampling incremental, remove duplicate HTF work, batch writes,
resolve writer ownership, and classify duplicate processes.

### Phase 7: rollout

Enable one computation family at a time after replay, lookahead, resource, and
operational validation. Retain rollback adapters through one validated release
cycle.

## 19. Acceptance Criteria

The design is implemented successfully only when:

- each active strategy receives shared cutoff-bound Polars frames;
- no active strategy independently loads market bars during evaluation;
- identical features are computed once per compatible cache key;
- sequential cutoffs reuse valid state or explicitly invalidate it;
- REST seed and 5m tail provenance is complete and auditable;
- full recomputation and incremental computation produce identical results;
- all active strategy outputs pass replay parity;
- admission and clash outputs are unchanged unless explicitly versioned;
- gateway writes have one owner and no avoidable concurrent writers;
- analyst retention preserves all operational states;
- offline compaction passes integrity checks;
- CPU, RAM, SQLite reads, and repeated feature counts show measured improvement.

Performance improvement alone cannot waive a parity or fail-closed requirement.

## 20. Locked Design Decisions

The following decisions are closed for implementation planning:

1. Direct REST 1h/4h history is the seed; completed 5m WS data is the live
   canonical tail.
2. The regime worker remains the sole direct-history writer.
3. HTF frames are hybrid and cutoff-bound, not independently fetched by each
   strategy.
4. One shared computation context serves all strategies for an evaluation.
5. Valid sequential computation state may be reused across evaluations.
6. Polars is mandatory for shared numerical market computation.
7. Strategies migrate to Polars feature frames and do not calculate shared
   indicators independently.
8. Python remains responsible for orchestration, policy, state machines,
   admission proofs, provenance, persistence, and delivery.
9. Candidate scoring and admission numeric prechecks may use Polars, while
   final proof and deterministic policy remain Python-owned.
10. Transient computation is not persisted as a second feature database.
11. Parity, lookahead, fail-closed, ownership, and resource gates are required
    before production rollout.
