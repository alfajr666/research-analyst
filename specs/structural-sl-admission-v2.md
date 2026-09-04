# Admission-Owned Structural SL Admission v2

## Status

Locked design, agreed during the operator discussion on 2026-09-04.

This specification supersedes the producer-declared structural-reference rule
in `structural-stop-admission-locked-decisions-v1.md`. Strategies remain blind
to HTF zones. Admission discovers and validates the structural context after
strategy evaluation and before scoring, selection, or delivery.

## 1. Core Decision

Every candidate selected for execution must pass both independent hard gates:

```text
strategy candidate
  -> generic execution admission
  -> admission-owned structural SL admission
  -> scoring and clash resolution
  -> alpha persistence and intent delivery
```

The strategy supplies only its normal entry, direction, and proposed
`invalidation_price`. It does not select, name, or receive HTF zones and must
not change its entry or stop formula for this policy.

Structural SL admission answers:

> Are both the entry and proposed SL near the most recent eligible HTF zone, and
> are their respective distances between 0.5 and 3.0 ATR of the selected zone
> timeframe?

Admission does not mutate the candidate stop, select a convenient zone after
seeing whether the stop passes, or manage an open position.

## 2. Ownership and Data Flow

```text
regime worker
  -> owns direct completed Bybit 1h/4h history and readiness

orchestrator
  -> runs strategies
  -> passes only emitted candidates to admission

admission
  -> reads regime-owned bars read-only for candidate assets
  -> calculates ATR and HTF zones once per asset/cutoff/timeframe
  -> selects the structural zone deterministically
  -> validates the proposed SL
```

The regime worker remains a filter and readiness service. It does not calculate
or persist admission ATR for every subscribed asset. Admission calculates ATR
only for assets that emitted at least one candidate. Multiple candidates for
the same asset and cutoff reuse the same calculated context.

Reading `regime.sqlite3` from admission is read-only and does not create a
second writer. The exact cutoff and canonical asset are mandatory context keys.

## 3. Zone Selection

Selection occurs before SL validation and is independent of the candidate's
pass/fail outcome.

### 3.1 Timeframe priority

1. Select from eligible 4h zones.
2. If no eligible 4h zone exists, select from eligible 1h zones.
3. If neither timeframe has an eligible zone, reject the candidate.

### 3.2 Direction and location

- Long candidates use bullish support zones below or containing the entry.
- Short candidates use bearish resistance zones above or containing the entry.
- Opposing-direction zones are not eligible.
- A zone must belong to the candidate's canonical asset.
- For a long, the entry is measured above the zone high and the SL below the
  zone low. For a short, the entry is measured below the zone low and the SL
  above the zone high.

### 3.3 Most recent zone

Among eligible zones at the selected timeframe, choose the greatest
`created_at` that is not after the candidate cutoff. Break an exact timestamp
tie by stable `zone_id` lexical order.

The selector must never iterate over zones and choose the first one whose
buffer makes the proposed SL pass.

### 3.4 Eligibility

A zone is eligible only when all of the following hold:

- state is `active` or `partial`;
- creation/confirmation is complete before the candidate cutoff;
- source coverage is complete and point-in-time valid;
- OHLC bounds are finite and positive with `low <= high`;
- source lineage and stable identity are present;
- the zone is not stale, filled, invalidated, forming, or superseded;
- required timeframe ATR is available from sufficient completed bars.

## 4. ATR Context

Admission uses the in-house Wilder ATR14 implementation on direct regime-owned
bars:

```text
4h zone -> ATR14 calculated from direct regime 4h bars
1h zone -> ATR14 calculated from direct regime 1h bars
```

ATR is calculated once per unique `(asset, cutoff, timeframe)` during the
admission pass. It is not calculated by the regime worker for assets that do
not produce candidates.

The ATR input must be:

- completed at or before the candidate cutoff;
- from the same canonical asset and selected timeframe;
- complete and gap-free for the required window;
- finite and positive;
- calculated with the tested in-house Wilder method;
- retained in the admission result with method, period, timeframe, cutoff,
  and source bar IDs for auditability.

Insufficient or invalid ATR fails structural admission closed.

## 5. Structural Buffer Rule

The proposed strategy stop remains authoritative. Admission only checks its
relationship to the selected zone boundary. It also checks that the entry is
near the same zone; an entry inside the zone or on the wrong side has a
non-positive entry buffer and fails closed.

For a long candidate:

```text
entry_buffer = entry - zone.high
0.5 * ATR <= entry_buffer <= 3.0 * ATR
```

For a short candidate:

```text
entry_buffer = zone.low - entry
0.5 * ATR <= entry_buffer <= 3.0 * ATR
```

For a long candidate, where `boundary = zone.low`:

```text
buffer = boundary - stop
0.5 * ATR <= buffer <= 3.0 * ATR
```

For a short candidate, where `boundary = zone.high`:

```text
buffer = stop - boundary
0.5 * ATR <= buffer <= 3.0 * ATR
```

The lower bound prevents a stop from sitting inside or directly on a zone.
The upper bound prevents a stop from being detached from the selected
structural invalidation level.

There is no global entry-to-SL maximum. The old `5%` global stop-distance
rule is removed. Total entry-to-SL distance may therefore exceed 3 ATR when
the selected HTF zone itself is far from entry; this is intentional and must
remain visible in audit output.

## 6. Result Contract

The admission result must include:

```json
{
  "structural_stop_gate": "pass | fail | unavailable",
  "structural_stop_reasons": [],
  "selected_zone_id": "...",
  "selected_zone_kind": "fvg | order_block",
  "selected_zone_timeframe": "4h",
  "selected_zone_state": "active",
  "selected_zone_boundary": 60000.0,
  "entry_zone_buffer": 200.0,
  "entry_zone_buffer_atr": 2.0,
  "structural_stop_buffer": 120.0,
  "structural_stop_buffer_atr": 1.2,
  "structural_atr": 100.0,
  "structural_atr_period": 14,
  "structural_atr_method": "wilder",
  "structural_atr_source_bar_ids": ["..."],
  "structural_context_cutoff": "2026-09-04T12:00:00Z"
}
```

Failure is auditable and cannot be scored, selected, persisted as an admitted
alpha event, or delivered to the executor.

## 7. Generic Admission Changes

The existing generic gate remains responsible for:

- symbol/account policy;
- finite positive prices;
- directional geometry;
- minimum RR;
- minimum ATR floor where retained by the existing policy;
- expiry, freshness, and identity.

The global `INTENT_MAX_STOP_DISTANCE_PCT` rule is removed. Structural admission
becomes the only maximum-distance rule for the SL.

The existing generic minimum stop rule is not silently changed by this spec.
Its retention or later removal requires a separate measured decision because
it controls a different relationship: entry-to-SL distance rather than
zone-to-SL distance.

## 8. Handoff and Audit Requirements

Admission context and result must be recomputed or verified at the alpha and
compatibility handoff boundary. A caller-supplied admission result must not be
trusted if it is stale, incomplete, or inconsistent with the candidate,
selected zone, cutoff, or proposed stop.

The exact proposed stop must survive unchanged through:

```text
candidate -> raw ledger -> selected event -> alpha outbox -> intent bus
```

The system must distinguish structural rejection from scoring suppression,
alpha persistence, bus publication, executor acceptance, and fill state.
Every intent handoff must also revalidate generic admission, structural proof
consistency, symbol/account identity, freshness, expiry, exact cutoff, and the
unchanged proposed stop. Strategy snapshots must not expose HTF zone records.

## 9. Required Tests

- 4h eligible zone is preferred over a newer 1h zone.
- 1h zone is used when no eligible 4h zone exists.
- Most recent eligible zone is selected deterministically.
- Selection does not depend on whether the stop would pass.
- Missing eligible zone rejects.
- Opposing-direction zone rejects.
- Long and short boundary directions are correct.
- Buffers below `0.5 ATR` reject.
- Buffers at `0.5 ATR`, `3 ATR`, and above `3 ATR` behave correctly.
- Entry-to-zone buffers below `0.5 ATR` and above `3 ATR` reject.
- Filled, invalidated, stale, forming, future, incomplete, and cross-asset
  zones reject.
- ATR is calculated only for candidate assets and is reused for same-asset
  candidates.
- The global entry-to-SL `5%` maximum no longer rejects a structurally valid
  candidate.
- Structural failure occurs before scoring and delivery.
- Alpha and intent handoff cannot bypass structural admission.
- The proposed stop remains unchanged in every result and handoff.
