# Structural-Stop Admission Locked Decisions v1

## Status

Locked design register, agreed during the operator discussion on 2026-09-01.

This document records the decisions that must not be changed implicitly during
implementation. It is compatible with the existing runtime ownership contract
and adds one explicit structural-stop policy exception to the generic advisory
context rule.

## 1. Core Decision

Structural-stop admission is an **independent deterministic hard gate**.

```text
candidate
  -> existing execution admission
  -> structural-stop admission
  -> scoring and clash resolution
  -> selection and delivery
```

Both gates must pass before a candidate can become an executor intent.

Structural-stop admission answers:

> Does the candidate's proposed stop clear the structural reference that the
> candidate declares as its setup invalidation level?

It does not answer which candidate is better. That remains the responsibility of
scoring and clash resolution.

## 2. Decision Register

### LSA-001: Existing admission remains intact

**Decision:** Do not replace, weaken, or silently modify the current admission
rules.

The existing gate continues to own:

- symbol/account policy;
- finite positive prices;
- directional SL/TP geometry;
- minimum RR;
- minimum and maximum stop distance;
- `ATR14_4h` availability and floor;
- future expiry;
- candidate identity;
- market freshness.

The current default remains:

```text
minimum stop = max(0.1% of entry, 0.25 * ATR14_4h / entry)
maximum stop = 5% of entry
```

Structural-stop admission is additive. It is not another score component and it
does not replace `ATR14_4h`.

### LSA-002: The strategy owns the proposed stop

**Decision:** The strategy candidate's `invalidation_price` is the canonical
proposed stop for analyst-side admission and delivery.

The structural gate may pass or reject it. No analyst component may:

- move it above or below a zone;
- widen or tighten it;
- clamp it to a maximum;
- recompute it from a different structure;
- replace it during PM reasoning.

If a downstream executor performs favorable stop maintenance, that remains an
executor-owned behavior outside this analyst admission decision.

### LSA-003: Structural-stop admission is pass/reject only

**Decision:** The new gate produces an independent status and reasons.

It must not mutate the candidate or turn structural evidence into a score.

The conceptual result is:

```json
{
  "structural_stop_gate": "pass | fail | unavailable",
  "structural_stop_reasons": [],
  "reference_id": "...",
  "reference_kind": "swing | fvg | order_block | strategy_boundary",
  "reference_boundary": 0.0,
  "stop_buffer": 0.0
}
```

An opted-in strategy with no valid structural reference fails closed. A strategy
that is not yet migrated is explicitly not subject to this gate; the system must
not infer a reference silently.

### LSA-004: The producer declares the reference

**Decision:** Admission validates a strategy-declared structural reference. It
does not select the nearest swing, FVG, order block, EMA, or arbitrary feature.

The reference must identify:

- kind;
- timeframe;
- asset;
- stable reference ID;
- invalidation boundary;
- formation and confirmation timestamps;
- cutoff;
- coverage state;
- source evidence IDs.

The strategy may choose a swing, FVG, order block, or another explicitly
approved strategy boundary. That choice is strategy metadata, not a generic
admission inference.

### LSA-005: Directional stop geometry

**Decision:** A short stop must clear the upper invalidation boundary. A long
stop must clear the lower invalidation boundary.

Conceptually:

```text
SHORT: stop > upper structural boundary + valid buffer
LONG:  stop < lower structural boundary - valid buffer
```

The exact buffer and maximum reference gap are intentionally not locked to a
numeric value in this register. They are implementation configuration and must
not be confused with the existing ATR stop-distance multiplier.

### LSA-006: FVGs are not generic stop anchors

**Decision:** An FVG is not automatically a thesis-invalidation level.

An FVG may be used only when the producer explicitly declares it as the
structural reference. The nearest FVG is never selected automatically.

Confirmed swings, FVGs, order blocks, and strategy-specific boundaries have
different semantics. A generic gate must validate the declared semantics rather
than treating them as interchangeable.

### LSA-007: Confirmed, point-in-time, covered structure only

**Decision:** A structural reference is valid only when its evidence is:

- confirmed by the detector's required closed bars;
- bounded by the candidate cutoff;
- formed and confirmed before the candidate observation;
- associated with the same asset;
- supported by complete required source coverage;
- not filled, invalidated, stale, or superseded at the cutoff.

Examples:

- A 2-left/2-right swing requires both right bars to close.
- An FVG requires its third candle to close.
- An order block requires its displacement candle to close.

Missing or insufficient context is not fabricated into support. For an opted-in
structural candidate, however, missing reference data is a hard failure.

### LSA-008: Per-asset freshness and coverage

**Decision:** Freshness and completeness are evaluated per asset and per
required interval.

A fresh BTC observation cannot satisfy SOL. A fresh 5m bar cannot prove that the
required 1h/4h structural context is fresh or complete.

Coverage includes expected timestamps, internal gaps, warmup, source lineage,
and purity. Global maximum timestamps and global row counts are insufficient.

### LSA-009: Deep warmup precedes rotated-symbol eligibility

**Decision:** A newly rotated symbol may be subscribed before it is eligible for
evaluation, but it must remain `warming_up` until deep backfill and all required
coverage checks pass.

The existing six-hour startup backfill is not considered sufficient structural
warmup. Reconnect gaps and rotation additions require durable backfill and
explicit readiness state.

### LSA-010: ATR provenance is mandatory

**Decision:** Any ATR used by admission or structural detection must have
point-in-time provenance and valid underlying coverage.

The provenance must identify:

- asset;
- timeframe and period;
- calculation method;
- source interval;
- source kind;
- last included bar;
- cutoff;
- coverage status;
- source evidence IDs.

Under-warmed ATR is unavailable. The structural-zone fallback value `1.0` is
not valid evidence for a hard gate.

### LSA-011: PM consumes the immutable plan

**Decision:** PM uses the originating intent's direction, entry, stop, target,
structural reference, and provenance.

PM may issue only its defined management advice. It cannot:

- move or replace the entry stop;
- select a different structural reference;
- weaken protection;
- treat an expired plan as the current plan;
- make the analyst structural gate pass or fail after the fact.

Executor state remains authoritative for live position, protection, quantity,
and lifecycle facts.

### LSA-012: Raw and delivery states remain distinct

**Decision:** A structural failure remains auditable but cannot proceed.

The ledger must distinguish:

```text
raw candidate
structural gate failed
existing admission failed
eligible
selected
alpha persisted
bus published
executor accepted
executed/filled
```

An alpha file write is not a bus receipt, and a bus publication is not an
execution or fill receipt.

### LSA-013: Structural failure precedes scoring

**Decision:** A candidate that fails the structural gate is not scored, selected,
clash-resolved, or delivered.

It remains in raw candidate/audit storage with explicit reasons. Contextual
scoring remains advisory for candidates that pass hard admission.

### LSA-014: UTC full-day complexity is deferred

**Decision:** Do not add `TR_1D`, `ATR14_1D`, native 1D subscription, or a
complex session-calendar subsystem in this patch.

The current market model remains continuous Bybit perpetual data with local
lower-timeframe resampling. If a daily metric is introduced later, its scope
will be decided separately.

### LSA-015: No strategy formula changes in this patch

**Decision:** The implementation must not change strategy entries, exits,
indicator formulas, or signal cadence.

Strategies will later be updated to emit structural-reference metadata. That is
an event-contract migration, not permission to redesign strategy behavior in the
readiness patch.

## 3. Explicitly Deferred Decisions

The following are not locked:

- exact structural buffer formula;
- maximum stop-to-reference gap;
- whether the buffer uses ticks, spread/slippage, local ATR, or a combination;
- per-strategy reference-kind mapping;
- exact structural gate rollout date;
- historical calibration of the existing `0.25` ATR multiplier;
- `TR_1D` or `ATR14_1D`;
- native daily bars or session calendars;
- whether downstream executor stop maintenance should change.

These decisions must not be smuggled into generic helper behavior or hidden
configuration defaults without a separate review.

## 4. Compatibility Clarifications

### Existing advisory context rule

The accepted trade-admission specification says generic HTF bias, swings, FVGs,
and order blocks are advisory. That remains true.

The new exception is narrow:

```text
generic contextual zone -> advisory
strategy-declared stop reference -> independently validated hard gate
```

### Existing strategy diversity

Current strategies derive stops from different concepts, including ATR offsets,
rolling extremes, EMA anchors, sweep extremes, bases, and flags. They cannot all
be assigned a swing/FVG reference by inference.

Each strategy must explicitly opt into the structural contract and declare its
reference semantics before the gate is required for that strategy.

### Executor ownership

The analyst owns candidate admission and intent construction. The executor owns
quantity, venue precision, protective-order attachment, live position state,
and execution receipts. Structural-stop admission does not cross that boundary.

## 5. Implementation Order

1. Repair cutoff normalization and point-in-time source manifests.
2. Add per-asset freshness, continuity, and coverage results.
3. Add deep rotation/reconnect warmup and readiness state.
4. Repair FVG/OB lifecycle and canonical ATR behavior.
5. Preserve complete deterministic zone snapshots and evidence IDs.
6. Add the structural-reference event contract.
7. Implement the independent structural-stop admission result.
8. Revalidate at alpha-outbox and compatibility handoff boundaries.
9. Update PM to consume the immutable originating plan.
10. Run audit-only, then enable per strategy.

No step may enable live structural rejection before the preceding data and
provenance steps are complete.

## 6. Source Authority

Normative current runtime sources:

- `AGENTS.md`
- `README.md`
- `CONTEXT.md`
- `specs/trade-admission-and-clash-resolution.md`
- `specs/strategy-fidelity-repair-and-expansion-v1.md`
- `specs/llm-position-sidecar.md`
- `specs/pm-sidecar-llm-only-v1.md`

Historical or superseded documents remain useful for background but cannot
override this register where they conflict:

- `specs/data-platform-strategy-plugins.md` for historical zone details;
- `specs/strategy-v2-shared-library.md` for older advisory semantics;
- `specs/mechanical-exit-sidecar-v1.md` for superseded mechanical PM behavior;
- `docs/DESIGN.md` where its older universe, timer, or ownership descriptions
  differ from current runtime notes.
