# Trade Quality Scoring and Regime Profiles v1

## Status

Accepted and locked for implementation on 2026-09-10.

This specification replaces the live execution role of hard trade admission
with one auditable, bounded trade-quality score. The legacy admission module may
remain as a compatibility surface during migration, but it is not on the
production decision path after this change.

## 1. Scope

The scorer owns:

- candidate validity and execution-safety floors;
- explainable market-quality components;
- RVOL and funding-overheating evidence;
- regime-aware component weights;
- score thresholding;
- same-direction ranking and opposite-direction clash resolution;
- immutable score provenance for alpha events and trade intents.

The scorer does not own:

- venue quantity, leverage, precision, or order type;
- exchange credentials, orders, fills, or receipts;
- position caps or venue protection;
- executor position management;
- regime calculation or regime database writes.

## 2. Runtime Flow

```text
completed 5m cutoff
  -> immutable regime observation lookup
  -> full-universe feature materialization
  -> strategy candidates
  -> modular quality components
  -> regime-aware score aggregation [0, 1]
  -> score threshold and clash resolution
  -> alpha event with score proof
  -> schema-v2 trade intent
  -> venue-local risk sizing using score multiplier
```

The scorer reads the regime worker's immutable observation for the exact
candidate cutoff. It never calculates or writes regime state.

## 3. Score Semantics

`quality_score` is a bounded execution-quality multiplier, not a probability.

```text
0.00 = no execution quality
0.30 = minimum analyst execution threshold
0.50 = 50% of venue-configured base risk
1.00 = 100% of venue-configured base risk
```

The default threshold is `TRADE_QUALITY_MIN_SCORE=0.30`.

The score is the weighted mean of active quality components after component
normalization. Component weights are normalized to a common scale for every
profile. A component marked unavailable follows its configured missing-data
policy and is never silently treated as support.

## 4. Component Interface

Every component is an independent module with one public evaluation seam:

```text
evaluate(ScoreContext) -> ComponentObservation
```

The observation contains:

- `component_id`;
- `component_version`;
- `role`: `floor`, `quality`, or `penalty`;
- `value` in `[0, 1]`;
- `status`: `support`, `neutral`, `contradict`, `unavailable`, or `invalid`;
- bounded raw inputs;
- source bar/evidence IDs;
- a deterministic reason;
- missing-data policy used.

Components do not call other components, mutate candidates, access databases,
or persist results. The aggregator is the only module that combines them.

## 5. Locked Components

Floor components:

- `identity_validity`: candidate identity, direction, timestamps;
- `price_geometry`: finite positive prices and directional SL/TP geometry;
- `reward_risk`: configured minimum RR;
- `stop_distance`: configured ATR-relative stop floor;
- `freshness_readiness`: cutoff-safe data and required strategy data;
- `structural_stop`: candidate stop is beyond the selected structural reference;
- `symbol_account_policy`: route and effective-universe policy.

Quality components:

- `htf_bias`;
- `zone_context`;
- `alignment`;
- `freshness_quality`;
- `same_direction_agreement`;
- `rvol`;
- `funding_overheating`;
- `contradiction`.

Any invalid floor observation forces the final score to `0.0`. The final
decision remains represented as `score < threshold`; there is no separate
production admission decision.

## 6. RVOL

The canonical RVOL input is the completed 5m execution frame at the candidate
cutoff.

```text
RVOL = latest completed 5m volume /
       median volume of the preceding 96 completed 5m bars
```

At least 32 valid prior bars are required. The component must preserve the
actual reference count and source observation IDs.

Initial interpretation:

- trend candidates: RVOL above the configured participation band supports;
- trend candidates with weak RVOL contradict;
- mean-reversion candidates: extreme RVOL is exhaustion evidence and is
  interpreted by the mean-reversion profile;
- reversal candidates: extreme RVOL may support exhaustion, while ordinary RVOL
  remains neutral;
- missing or stale volume is unavailable and does not create support.

The component owns RVOL normalization. Strategy plugins must not add an
independent global RVOL contribution.

## 7. Funding Overheating

Funding uses the latest completed, cutoff-safe funding observation and a bounded
history of the preceding 288 completed 5m observations. At least 32 valid
funding observations are required.

The component computes the percentile rank of the absolute current funding rate
within the historical absolute-rate distribution. A rate at or below the
configured neutral percentile is neutral. A rate above the configured heat
percentile is overheated.

Direction-aware interpretation:

- positive overheated funding is crowded-long evidence;
- negative overheated funding is crowded-short evidence;
- crowded in the candidate direction contradicts continuation;
- crowded opposite the candidate direction supports continuation;
- for reversal-family candidates, crowded same-side funding can support a
  contrarian reversal;
- missing funding remains unavailable, never zero.

Existing strategy-local `funding_neutral` fields are evidence only and must not
be added as a second global score component.

## 8. Regime-Aware Weight Profiles

The regime worker remains authoritative for regime observations. Profiles are:

- `neutral-v1`;
- `trend-v1`;
- `mean-reversion-v1`;
- `reversal-v1`.

The candidate's canonical `market_family` selects the family profile. The
regime worker's family weight controls interpolation with the neutral profile:

```text
effective_weights = family_weight * family_profile
                    + (1 - family_weight) * neutral_profile
```

Reversal uses its dedicated boolean activation gate as the family weight.

`REGIME_SESSION_MODE` behavior:

- `off`: use `neutral-v1` operationally;
- `shadow`: calculate and persist baseline and regime-weighted scores, but use
  baseline score for thresholding, delivery, and sizing;
- `enforce`: use the exact-cutoff regime-weighted score operationally.

In enforce mode, missing or insufficient regime data forces a score floor of
`0.0`. The scorer must not invent a profile.

The regime weight is not also a separate score component. This prevents regime
evidence from being double-counted.

## 9. Clash Resolution

Only candidates with `quality_score >= threshold` enter clash resolution.

- Same asset, account, and direction: highest score wins deterministically.
- Opposite directions: the higher score wins only when the normalized margin
  is at least `TRADE_QUALITY_CLASH_MIN_MARGIN=0.10`.
- Otherwise both remain advisory and no intent is emitted.

Suppression is not rejection. Every candidate retains its score and reason.

## 10. Persistence

Every candidate status record and selected alpha event must retain:

- `quality_score`;
- baseline score when regime weighting is shadowed;
- component observations and statuses;
- score policy version;
- weight profile and profile version;
- regime observation ID and regime score version;
- family activation version;
- exact evaluation cutoff;
- selected/suppressed decision and clash margin.

## 11. Acceptance Criteria

1. Every production score is finite and in `[0, 1]`.
2. Invalid floor inputs produce score `0.0`.
3. Missing RVOL or funding never becomes fabricated support.
4. RVOL and funding are independently testable components.
5. Regime profile selection uses the exact asset and cutoff observation.
6. Shadow mode cannot alter live thresholding, delivery, or sizing.
7. Enforce mode fails closed on missing regime readiness.
8. Same-direction ranking remains deterministic.
9. Opposing directions require the normalized score margin.
10. The score is preserved unchanged into the trade intent.
11. Historical replay proves score meaning is comparable across profiles.
