# ATR-Based Stop Admission

## Status

Accepted and locked by the operator on 2026-08-30.

This specification changes the universal hard stop-distance admission rule. It
does not change strategy-local stop construction, target construction, RR
requirements, scorer semantics, clash resolution, sizing, or executor behavior.

## Decision

Replace the fixed-only minimum stop-distance rule with a symbol-relative,
point-in-time volatility floor based on completed 4h ATR14.

The initial deployment multiplier is configurable and defaults to `0.25`:

```text
effective_min_stop_distance = max(
    INTENT_MIN_STOP_DISTANCE_PCT,
    ATR14_4H / entry_price * INTENT_MIN_STOP_ATR_MULTIPLIER,
)
```

With deployment defaults:

```text
INTENT_MIN_STOP_DISTANCE_PCT=0.001
INTENT_MIN_STOP_ATR_MULTIPLIER=0.25
```

The existing `0.1%` value remains a defensive absolute floor. It is not the
normal volatility policy when 4h ATR implies a larger distance.

The stop is never widened or clamped by admission. A candidate either has a
strategy-produced structural invalidation level far enough from entry or it is
rejected as advisory-only.

## Scope

The rule applies to every candidate entering the universal hard admission path,
including the configured compact and dual-zone admission strategies. It is
evaluated before scoring and clash resolution.

The rule does not apply retroactively to already-created executor intents or
active positions. Existing executor hard stops remain executor-owned.

## Definitions

### Entry price

Use the deterministic candidate entry price:

1. `entry_price`, when present.
2. `entry_condition.price`, otherwise.

Market-entry candidates without a known entry price cannot be evaluated against
an entry-relative ATR floor and remain subject to the existing market-entry
contract. They must not be silently treated as passing the ATR gate.

### 4h ATR14

`ATR14_4H` is the latest ATR14 calculated from completed 4h OHLC bars available
at the candidate's evaluation cutoff.

The calculation must be point-in-time safe:

- Do not use a currently forming 4h bar.
- Do not use bars after the candidate cutoff.
- Use the same ATR14 definition as `strategy_v2_context.atr_last` and the
  existing 4h strategy context.
- Require sufficient completed bars for ATR14 calculation.
- Require a finite, strictly positive ATR value.

ATR is an absolute price distance. The normalized ATR percentage is:

```text
atr14_4h_pct = ATR14_4H / entry_price
```

### Observed stop distance

```text
stop_distance = abs(entry_price - invalidation_price)
stop_distance_pct = stop_distance / entry_price
```

The candidate's `invalidation_price` is the stop used for admission. Admission
must not substitute a target, EMA, market price, or executor-calculated stop.

## Hard Admission Rule

The candidate passes the stop-distance portion of admission only when:

```text
stop_distance_pct >= effective_min_stop_distance
stop_distance_pct <= INTENT_MAX_STOP_DISTANCE_PCT
```

The lower-bound failure reason must identify the volatility inputs:

```text
stop distance below ATR-based minimum
```

The auditable admission result must include at least:

```json
{
  "atr14_4h": 12.5,
  "atr14_4h_pct": 0.0125,
  "stop_distance_pct": 0.004,
  "stop_atr_multiple": 0.32,
  "min_stop_atr_multiplier": 0.25,
  "configured_min_stop_distance_pct": 0.001,
  "effective_min_stop_distance_pct": 0.003125
}
```

The existing geometry, RR, expiry, freshness, positive-price, identity, and
maximum-stop checks remain unchanged. ATR admission cannot rescue any other
hard-gate failure.

## Missing or Invalid ATR

For a candidate with a known entry and stop, missing or invalid 4h ATR is a
hard admission failure, not a pass and not a fabricated fallback.

The reason must distinguish the condition, for example:

- `4h ATR14 is unavailable`
- `4h ATR14 is stale`
- `4h ATR14 is invalid`

The candidate remains retained in raw/advisory persistence. It must not reach
the executor or scorer-based selection as an eligible candidate.

The ATR freshness cutoff is the candidate evaluation cutoff. The general market
freshness gate remains separate and continues to use its configured maximum
age.

## Interaction With Strategy-Local ATR Rules

Strategy-local ATR constraints remain authoritative and continue to run where
they already exist. The universal rule is an additional lower bound:

```text
strategy structural stop
    -> strategy-local ATR/risk checks
    -> universal 4h ATR minimum stop floor
    -> universal RR and remaining admission gates
```

The universal gate must not alter strategy invalidation formulas. In particular,
existing local rules such as `risk <= r_max * ATR_15m` or `risk <= r_max *
ATR_1h` remain rejection rules and must not be replaced by the 4h rule.

The universal floor is intentionally asymmetric with the existing maximum:
the floor protects against micro-stops while the maximum prevents structurally
unbounded stops.

## RR and Target Behavior

RR is calculated using the original candidate stop and the deterministic
selected target:

```text
RR = abs(target - entry_price) / abs(entry_price - invalidation_price)
```

Admission must not move the stop or target to make RR pass. A candidate that
passes the ATR floor but fails `INTENT_MIN_RR` remains rejected.

Because the new floor can reject candidates before scoring, expected trade
frequency may decrease. This is intentional; the policy prefers no trade over a
micro-stop whose risk is not meaningful relative to symbol volatility.

## Scoring and Clash Resolution

ATR stop admission remains a hard gate, not a score component. The scorer must
not reward or penalize ATR distance, and a high score cannot rescue an ATR gate
failure.

Only candidates passing the ATR floor and all other hard gates enter same-direction
ranking or opposite-direction clash resolution. Suppression statuses remain
unchanged.

ATR measurements may be included in feature snapshots and notifications as
explanation metadata, but these annotations cannot modify entry, stop, target,
direction, RR, or hard-gate status.

## Persistence and Audit

For every complete candidate, retain:

- `atr14_4h`
- `atr14_4h_pct`
- `stop_distance_pct`
- `stop_atr_multiple`
- `min_stop_atr_multiplier`
- configured percentage floor
- effective percentage floor
- ATR source cutoff/bar timestamp
- ATR calculation/version identifier
- hard-gate result and rejection reason

Raw capture must occur before admission as it does today. Failed candidates
remain advisory evidence and must never be written as executor intents.

## Configuration

Add one new configuration value:

| Name | Default | Meaning |
| --- | ---: | --- |
| `INTENT_MIN_STOP_ATR_MULTIPLIER` | `0.25` | Minimum observed stop distance measured in completed 4h ATR14 units |

The value must be finite and non-negative at configuration load. A negative
value is invalid configuration and must fail closed rather than weaken the
floor. A value of zero disables the ATR contribution while retaining the
absolute percentage floor, which is useful for controlled rollback only.

`INTENT_MIN_STOP_DISTANCE_PCT` remains configurable as the absolute defensive
floor. `INTENT_MAX_STOP_DISTANCE_PCT` remains the universal upper bound.

## Rollout and Monitoring

Before enabling delivery after implementation:

1. Run the existing test suite and new admission tests.
2. Replay recent raw candidates without delivery.
3. Measure rejection rates by strategy, asset, direction, and evaluation
   interval.
4. Report distributions for `stop_atr_multiple`, `atr14_4h_pct`, RR, and the
   effective floor.
5. Verify zero executor intents have `stop_atr_multiple < 0.25`, except for
   explicitly documented market-entry cases.
6. Verify no stale or future 4h bar contributes to ATR.

The first review should compare at least:

- candidate count before and after the floor
- hard-gate rejection rate
- selected-intent count
- RR distribution
- stop-out rate and outcome expectancy by strategy
- rejection concentration in high-volatility assets

No multiplier increase should be made from intuition alone. It requires a
replay or outcome analysis by strategy timeframe.

## Required Implementation Touchpoints

The implementation must inspect and update consistently:

- `trade_admission.py`: calculate and enforce the universal ATR floor
- `strategy_plugins.py`: provide the point-in-time 4h ATR context to admission
- `strategy_v2_context.py`: reuse the existing ATR14 calculation and cutoff-safe
  4h bars rather than creating a second formula
- `config.py` and `.env.example`: add and document the multiplier
- `intent_outbox.py`: mirror the same hard-stop contract before executor writing
- `raw_signal_batch.py` and analyst persistence: retain ATR audit fields/reasons
- `signal_publisher.py` and formatting: expose explanation metadata without
  changing lifecycle fields
- compact and dual-zone strategy payloads: preserve or add ATR evidence as
  needed; do not duplicate admission decisions inside strategies
- admission, outbox, publisher, and plugin integration tests
- `README.md`, `agent.md`, and related admission specs

There must be one shared definition of the effective minimum and one shared
definition of the ATR source cutoff. The analyst and executor-facing validation
paths must not diverge.

## Required Tests

Tests must prove:

1. A stop at exactly `0.25 × ATR14_4H` passes the ATR floor.
2. A stop below `0.25 × ATR14_4H` fails even when it exceeds `0.1%`.
3. A low-volatility symbol still respects the absolute `0.1%` floor.
4. A configurable multiplier changes the effective floor deterministically.
5. A stop passing the ATR floor can still fail RR.
6. A stop passing the ATR floor can still fail the maximum stop bound.
7. Missing, stale, non-finite, and non-positive ATR fail closed.
8. ATR uses only completed bars at or before the evaluation cutoff.
9. Long and short candidates use identical distance math.
10. Admission never widens or rewrites the supplied stop.
11. ATR-gate failures remain raw/advisory-only and cannot create executor intents.
12. Same-direction scoring excludes ATR-gate failures.
13. Opposite-direction clash resolution sees only ATR-admitted candidates.
14. The executor intent validation path rejects any candidate below the same
    effective ATR floor when sufficient ATR metadata is available.
15. Audit persistence records all ATR values, thresholds, source cutoff, and
    rejection reasons.

## Non-Goals

This change does not:

- set every stop to `0.25 × ATR14_4H`
- replace structural invalidation with an ATR stop
- use a generic fixed percentage such as `1%`
- make ATR a soft scorer input
- change target selection or RR minimum
- change quantity, leverage, sizing, or account routing
- alter active-position stops or PM-sidecar authority
- guarantee a particular trade frequency or win rate
