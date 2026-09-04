# Structural-Stop Admission Patch Readiness v1

## Status

Implementation specification and readiness gate.

This document is a pre-implementation audit and patch plan. It does not assert
that the current runtime satisfies the target contract. No application code is
changed by this document.

The target change adds an independent structural-stop admission gate while
preserving the existing execution admission policy. The gate is not ready for
live rejection until the data, provenance, warmup, and structural-reference
prerequisites in this document are complete.

## 1. Goal

Prevent an otherwise geometrically valid candidate from reaching execution when
its proposed stop does not clear the market structure that the candidate claims
invalidates its setup.

The target pipeline is:

```text
market observations
  -> complete, cutoff-bounded structural context
  -> strategy candidate with declared structural reference
  -> existing execution admission
  -> independent structural-stop admission
  -> scoring and clash resolution
  -> selected alpha event
  -> shared intent bus
```

The structural gate validates a candidate-proposed stop. It does not place,
move, widen, tighten, or otherwise mutate that stop.

## 2. Scope

### In scope

- Per-asset freshness and coverage for every required interval.
- Deep warmup for newly rotated symbols.
- Canonical ATR provenance and complete input coverage.
- Reliable 1h/4h swings, FVGs, and order blocks.
- Complete point-in-time structural snapshots.
- A strategy-declared structural-reference event contract.
- A separate deterministic structural-stop pass/reject gate.
- Revalidation at the alpha-outbox boundary.
- Consistent PM context without PM stop mutation.
- Audit history, dry-run rollout, and rollback.

### Out of scope

- Changing strategy entry logic or signal formulas.
- Changing the existing `ATR14_4h` admission rule.
- Adding `TR_1D` or `ATR14_1D`.
- Native 1D WebSocket subscription.
- A complex exchange session, holiday, or timezone calendar.
- Executor sizing, protective-order behavior, or venue receipts.
- Automatic stop movement by any analyst component.
- Historical multiplier calibration.

## 3. Current Execution Boundaries

The principal implementation seams are:

| Responsibility | Current location | Readiness implication |
| --- | --- | --- |
| Market ingestion and writes | `src/research_analyst/ws_gateway.py` | Sole market writer; must provide complete source bars and backfill. |
| Canonical bars and resampling | `src/research_analyst/strategy_v2_context.py` | Must expose continuity and cutoff-safe provenance. |
| Zone detection | `src/research_analyst/structure_zones.py` | Must produce valid lifecycle states only after creation. |
| Confirmed swings | `src/research_analyst/market_structure.py` | Must retain confirmation and source identity. |
| Zone materialization | `src/research_analyst/orchestrator.py` | Must persist all required fields per asset. |
| Candidate invocation | `src/research_analyst/strategy_plugins.py` | Must attach per-asset freshness, ATR provenance, and structural context. |
| Existing admission | `src/research_analyst/trade_admission.py` | Remains authoritative for current hard gates. |
| Alpha and intent handoff | `src/research_analyst/alpha_outbox.py`, `intent_outbox.py` | Must revalidate and preserve the original stop. |
| PM context | `src/research_analyst/pm_sidecar.py` | Must consume the originating immutable plan. |

The existing admission specification remains authoritative for current geometry,
RR, ATR, expiry, freshness, and identity rules. This patch adds a new policy;
it must not silently turn generic contextual scoring into a hard gate.

## 4. Readiness Verdict

Structural-stop admission is **not ready for live enforcement** in the current
repository state.

### P0 blockers

1. Freshness and bar availability are global rather than candidate-asset scoped.
2. Coverage checks use row existence/counts rather than continuity and expected
   timestamps.
3. Startup backfill is six hours by default and rotation changes do not trigger
   deep backfill or warmup gating.
4. Persisted zone snapshots lose price bounds, state, and stable identity when
   reconstructed by `_build_snapshot()`.
5. Zone materialization is capped and identified non-deterministically.
6. FVG/OB lifecycle evaluation is not reliably restricted to bars after
   creation.
7. ATR implementations have inconsistent methods and under-warm fallbacks.
8. Candidates do not contain a standardized structural reference.
9. Finalized cutoff records do not freeze their source-observation manifest.
10. Existing trigger timestamp normalization can select the wrong 5m cutoff for
    a `59.999ms` source-end representation.

### P1 blockers

1. PM does not retain the complete originating stop/protection context.
2. PM active-intent lookup is not unambiguously tied to the originating intent
   and can use expired plans.
3. Alpha delivery status can conflate file selection with bus publication.
4. Rotation ranking accepts sparse 24-hour spans and has global freshness logic.
5. Gateway and orchestrator health do not show per-asset market coverage.
6. The legacy execution adapter can read events outside the new gate boundary.

## 5. Required Patches

### Patch A: Per-asset coverage service

Introduce one reusable coverage result for `(asset, interval, cutoff)`.

The result must include:

```json
{
  "asset": "BTC",
  "interval": "4h",
  "cutoff": "2026-09-01T12:05:00Z",
  "latest_end": "2026-09-01T12:00:00Z",
  "freshness_seconds": 300,
  "expected_bars": 48,
  "observed_bars": 48,
  "missing_ends": [],
  "max_gap_seconds": 14400,
  "source": "bybit_ws",
  "purity": "pure_ws",
  "status": "covered"
}
```

Required behavior:

- Query by candidate asset, never only by interval.
- Exclude `source_end > cutoff`.
- Require confirmed, valid, finite OHLC data.
- Validate expected interval spacing and internal gaps.
- Detect duplicate normalized timestamps.
- Preserve source and purity lineage.
- Return explicit `missing`, `stale`, `incomplete`, `mixed_source`, or
  `covered` states.

Replace the global behavior in:

- `strategy_plugins._data_freshness_seconds()`;
- `strategy_plugins._bars_available()`;
- orchestrator health queries;
- rotation source validation;
- PM context admission.

One asset must never inherit freshness, availability, or coverage from another.

### Patch B: Canonical cutoff and source manifest

Normalize source timestamps before trigger key creation and before resampling.
The `59.999ms` representation must map to the intended completed boundary.

Every finalized cutoff must persist:

- cutoff ID and interval;
- exact source observation IDs or immutable revisions;
- selected source per timestamp;
- feature snapshot IDs;
- structural zone IDs;
- coverage result per required asset/timeframe;
- policy versions.

Once finalized, a cutoff manifest must not change because of late data or source
corrections. Corrections require a new revision and a new explicit replay.

The same completed-bar convention must be used by:

- `evaluation_trigger.py`;
- `strategy_v2_context.py`;
- `ws_gateway.py`;
- orchestrator feature materialization;
- plugin invocation;
- structural admission.

### Patch C: Deep rotation warmup

When a symbol enters the subscription feed:

```text
feed addition
  -> deep backfill
  -> per-asset coverage check
  -> indicator and structure warmup
  -> ready
```

Until `ready`, the symbol may remain subscribed but must be marked
`warming_up` and cannot produce an executor-eligible candidate.

The deep history requirement is the maximum of all enabled consumer needs,
including:

- direct 1m/5m data;
- complete 15m/1h/4h resamples;
- ATR and EMA warmups;
- swing confirmation windows;
- FVG and OB lifecycle windows;
- any active PM context requirement.

The current six-hour startup backfill is not sufficient. Rotation-time backfill
and reconnect gap fill must be durable, retryable, and observable through the
existing `deep_backfill_jobs` model or an explicitly superseding model.

### Patch D: Reliable structural zone engine

#### D.1 FVG lifecycle

Use the existing normative zone definitions in
`specs/data-platform-strategy-plugins.md`, with explicit state transitions:

```text
active -> partial -> filled
active/partial -> invalidated
```

Rules:

- Create only after the third candle closes.
- Evaluate mitigation only on candles after creation.
- Record creation, first mitigation, fill, and invalidation timestamps.
- Define precedence when one candle touches multiple boundaries.
- Use the correct far boundary for fill.
- Preserve the source bars that created and changed the zone.

#### D.2 Order-block lifecycle

- Create only after the displacement candle closes.
- Evaluate mitigation only after creation.
- Invalidate bullish blocks only on the defined completed close below the low.
- Invalidate bearish blocks only on the defined completed close above the high.
- Retain the displacement and opposing-candle evidence.

#### D.3 ATR and warmup

Use a canonical point-in-time ATR method for structural thresholds. Under-warmed
ATR must be unavailable, never the `1.0` fallback currently returned by
`structure_zones.compute_atr()`.

Zone generation must require enough bars for the actual detector and ATR period,
not merely `df.height >= 5`.

### Patch E: Complete deterministic zone snapshots

Persist and expose, per zone:

- stable zone ID;
- asset;
- timeframe;
- type and direction;
- low and high bounds;
- state;
- creation and lifecycle timestamps;
- source evidence IDs;
- cutoff ID;
- coverage status;
- source and purity provenance.

Do not cap with a global `(fvgs + obs)[:6]`. Apply the documented bound after
grouping by:

```text
asset + timeframe + direction + zone type
```

IDs must not use `time.time()`. They must be deterministic from cutoff, asset,
zone type, direction, creation timestamp, and boundaries.

Write feature snapshots for every asset, not only `assets[0]`.

### Patch F: Strategy-declared structural reference

The strategy producer must eventually declare the structural reference used by
its proposed stop. This is metadata, not a change to entry logic.

Minimum contract:

```json
{
  "structural_reference": {
    "kind": "swing | fvg | order_block | strategy_boundary",
    "timeframe": "1h | 4h",
    "asset": "BTC",
    "reference_id": "zone-or-pivot-id",
    "boundary_price": 60000.0,
    "formed_at": "2026-09-01T10:00:00Z",
    "confirmed_at": "2026-09-01T10:30:00Z",
    "cutoff_at": "2026-09-01T11:00:00Z",
    "coverage_status": "covered",
    "source_evidence_ids": ["..."]
  }
}
```

The candidate's `invalidation_price` remains the proposed stop. Admission does
not select a nearby reference and does not rewrite the candidate.

Strategies that do not declare a reference are not silently inferred. They are
either explicitly opted out or fail the structural gate once their policy says
the gate is required.

### Patch G: Independent structural-stop admission

Add a distinct result, separate from the existing `hard_gate` fields:

```json
{
  "structural_stop_gate": "pass",
  "structural_stop_reasons": [],
  "reference_id": "...",
  "reference_kind": "swing",
  "reference_boundary": 60000.0,
  "stop_buffer": 120.0
}
```

For a long, the stop must clear the lower invalidation boundary in the correct
direction. For a short, it must clear the upper invalidation boundary. The gate
must reject:

- missing or malformed reference;
- wrong asset or timeframe;
- unconfirmed or future reference;
- incomplete or stale source coverage;
- wrong-side stop;
- unreasonable stop-to-reference buffer;
- non-finite values.

The exact buffer and maximum-distance values remain configuration decisions and
must not be hidden inside the current ATR admission.

The gate runs before scoring and clash resolution. Failed candidates remain in
the raw ledger with durable reasons and cannot reach alpha or executor delivery.

### Patch H: Handoff and PM consistency

At `alpha_outbox.write_event()` and any compatibility adapter boundary:

- recompute or verify structural admission;
- preserve the exact proposed stop;
- reject stale or bypassed admission results;
- distinguish selection, alpha persistence, bus publication, executor acceptance,
  and execution.

The PM sidecar must receive the originating immutable plan, including:

- exact intent identity;
- original direction and entry;
- original stop and target;
- structural reference and provenance;
- current executor mark/protection/lifecycle state.

PM may advise `HOLD`, `REDUCE`, `EXIT`, or `NEAR_TP`, but may not modify the
structural stop or substitute an expired plan.

## 6. Acceptance Test Matrix

### Coverage and source integrity

- Fresh BTC plus stale SOL: SOL fails independently.
- Missing asset data cannot be satisfied by another asset.
- One missing internal 5m bar invalidates the affected derived bucket.
- Duplicate normalized timestamps fail or resolve deterministically with lineage.
- Open, future, malformed, or non-finite bars are excluded.
- Mixed-source derived bars cannot be labeled pure without parent proof.
- Late data cannot alter a finalized cutoff manifest.
- Trigger input `59.999ms` produces the intended completed 5m cutoff.

### Warmup and rotation

- Newly rotated asset is `warming_up` until deep backfill and coverage pass.
- Backfill failure is durable and retryable.
- Reconnect gaps are recovered without duplicate observations.
- A warm asset can become not-ready after a coverage failure.
- Health reports per-asset stale, missing, incomplete, and ready states.

### Zones and references

- FVG cannot be mitigated by pre-creation bars.
- OB cannot be mitigated or invalidated before creation.
- FVG fill uses the correct far boundary.
- Zone IDs are deterministic and reruns are idempotent.
- Zone bounds/state survive snapshot construction.
- Insufficient ATR produces unavailable, not fabricated, zones.
- Reference timestamps and evidence are cutoff-valid.
- A candidate cannot refer to another asset's zone.

### Structural admission

- Valid long reference and stop pass.
- Valid short reference and stop pass.
- Wrong-side stops fail.
- Missing, stale, uncovered, forming, filled, invalidated, and future references
  fail.
- Excessive reference buffer fails.
- Gate failure is recorded before scoring and prevents delivery.
- Admission never mutates `invalidation_price`.
- Alpha and intent handoff preserve the exact stop and reference ID.

### PM and delivery

- PM receives the same immutable stop/reference as the originating event.
- Expired event lookup cannot replace the originating intent.
- PM failures produce safe HOLD behavior without stop mutation.
- A failed bus publication is not reported as a successful write.
- Legacy compatibility delivery cannot bypass structural admission.

## 7. Rollout

1. Land coverage, cutoff, warmup, zone lifecycle, snapshot, and provenance
   patches with no structural rejection enabled.
2. Add candidate metadata for one strategy family at a time.
3. Run the structural gate in audit-only mode and record pass/fail/unavailable
   counts without suppressing delivery.
4. Replay historical cutoffs and inspect false rejection and missing-reference
   rates.
5. Enable hard rejection behind a per-strategy configuration flag.
6. Verify raw ledger, alpha ledger, bus publication, and executor receipts
   independently after each enablement.

Rollback is the per-strategy structural-gate flag, not removal of structural
metadata or deletion of audit records. Existing ATR/RR/freshness admission stays
enabled during rollback.

## 8. Evidence Required Before Live Enablement

- Full test suite passes.
- Per-asset coverage report is available for the production subscription.
- Newly rotated assets remain warming until ready.
- No unresolved cutoff timestamp mismatch.
- Zone lifecycle tests pass for both directions.
- Structural reference survives candidate, alpha, intent, and PM boundaries.
- Audit-only run has reviewed rejection and unavailable rates.
- No structural-gate bypass exists in shared-bus or compatibility paths.
- Services are restarted only through `oxmgr`, followed by live health and
  receipt verification.
