# Admission-Owned Structural SL Admission v3

## Status

Locked revision of `structural-sl-admission-v2.md` for directional zone
containment. This revision changes entry location semantics only. The 4h > 1h
priority, proposed stop authority, and structural stop buffer policy remain
unchanged.

## 1. Decision

Every candidate selected for execution must pass generic execution admission and
admission-owned structural SL admission before scoring or delivery.

The strategy supplies its entry, direction, and proposed `invalidation_price`.
Admission selects the newest eligible zone at the strongest available timeframe
and validates the proposed stop without mutating it.

## 2. Zone Priority

The selector checks timeframes in this order:

1. Eligible 4h zones.
2. Eligible 1h zones when no eligible 4h zone exists.

An eligible zone must be directional, asset-matched, complete before the exact
cutoff, covered by valid source evidence, active or partial, non-stale, and have
finite valid bounds. The newest eligible zone wins within a timeframe.

The selector does not choose a different zone because the proposed stop would
pass against it.

## 3. Directional Entry Containment

Directional containment is valid:

```text
long candidate + bullish support zone:
    entry >= zone.low

short candidate + bearish resistance zone:
    entry <= zone.high
```

An entry between the zone bounds, including either boundary, is `inside` and is
accepted. An entry on the continuation side of the zone is:

```text
long:  entry > zone.high  -> above
short: entry < zone.low    -> below
```

For an outside entry, the existing proximity rule remains inclusive:

```text
long:  0.5 * ATR <= entry - zone.high <= 3.0 * ATR
short: 0.5 * ATR <= zone.low - entry <= 3.0 * ATR
```

Contained entries do not receive a negative entry-buffer rejection. For audit,
an inside entry records `entry_zone_location=inside`, `entry_zone_buffer=0.0`,
and `entry_zone_buffer_atr=0.0`. Outside entries record `above` or `below` and
their positive distance from the relevant outer boundary.

Opposing-direction zones remain ineligible. Long candidates cannot use bearish
zones, and short candidates cannot use bullish zones.

## 4. Structural Stop Buffer

The proposed strategy stop remains authoritative.

For a long candidate:

```text
stop_buffer = zone.low - stop
0.5 * ATR <= stop_buffer <= 3.0 * ATR
```

For a short candidate:

```text
stop_buffer = stop - zone.high
0.5 * ATR <= stop_buffer <= 3.0 * ATR
```

The stop must therefore be beyond the directional zone invalidation boundary.
An entry may be inside the zone, but the stop may not be inside the zone.

## 5. Provenance

The admission result must include:

```json
{
  "structural_admission_contract_version": "structural-sl-admission-v3",
  "entry_zone_location": "inside | above | below",
  "entry_zone_buffer": 0.0,
  "entry_zone_buffer_atr": 0.0
}
```

Handoff validation must recompute and verify the location, buffers, selected
zone, exact cutoff, source evidence, ATR method and period, and unchanged stop.
Proofs without the v3 contract version or with inconsistent entry location are
invalid.

## 6. Required Tests

- Long entry inside a bullish support zone passes with a valid stop below the
  zone low.
- Short entry inside a bearish resistance zone passes with a valid stop above
  the zone high.
- Boundary entries are treated as inside entries.
- Long entries below support and short entries above resistance are rejected.
- Outside entry proximity retains the 0.5-3.0 ATR inclusive bounds.
- A contained entry does not produce a negative entry-buffer rejection.
- Stops inside or too close to the zone still fail.
- 4h priority and newest-within-timeframe selection remain unchanged.
- Intent handoff accepts a valid v3 contained-entry proof.
- Intent handoff rejects a missing or inconsistent entry-location proof.
