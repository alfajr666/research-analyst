# Reaction Scorer and Decayed Anchored Volume Profile v1

**Status:** Proposed

## 1. Decision

Replace the current broad weighted scorer with a smaller reaction-oriented
scorer, behind an explicit `off | shadow | enforce` rollout mode. Build one
cutoff-bound, decayed anchored volume-at-price profile per candidate asset and
use it to measure a confirmed directional reaction at POC or a prominent HVN.

The change does not alter Research Analyst's terminal contract. The final
output remains a validated TradeIntent published only to the shared intent bus.
The executor remains the sole owner of position size, leverage, venue rules,
orders, fills, and protection.

## 2. Goals

1. Remove safety checks and duplicated structural evidence from the sizing
   score while preserving every deterministic admission condition.
2. Make the score answer one interpretable question: did the candidate show a
   directionally valid reaction at an established value node, with sufficient
   participation and without adverse crowding?
3. Use three complete days of 15m history without lookahead.
4. Preserve a high-volume anchor until a newer completed hour exceeds the
   decayed strength of the existing anchor.
5. Make shadow-to-enforce promotion a configuration-only operational switch.
6. Persist enough provenance to replay every result exactly.

## 3. Non-goals

- No venue adapter, execution, sizing instruction, position management, or LLM.
- No CVD, raw trades, order-book imbalance, liquidation feed, or VPIN.
- No new market-data writer and no direct exchange data path inside the scorer.
- No strategy trigger changes in v1.
- No assumption that POC/HVN proximity alone is directional support.
- No production score-weight promotion without locked outcome validation.

## 4. Rollout contract

Add one setting:

```text
REACTION_SCORER_MODE=off|shadow|enforce
```

Default: `shadow` after implementation is deployed and replay tests pass.

| Mode | Legacy v2 score | Reaction v3, including OI | Operational score and verdict |
| --- | --- | --- | --- |
| `off` | computed | not computed; no candidate-scoped OI fetch | v2 |
| `shadow` | computed | fully weighted, computed, and persisted | v2 |
| `enforce` | computed and persisted for comparison | same fully weighted result | v3 |

Shadow and enforce must call the same v3 implementation. The only allowed
difference is this selector:

```text
operational_result = v3_result if mode == "enforce" else v2_result
```

In enforce mode, `quality_score`, thresholding, same-direction ranking,
opposite-direction clash resolution, the alpha event, and the shared-bus source
payload must all use v3. Persist both versions in every mode except `off`.

Changing `REACTION_SCORER_MODE` requires only a managed orchestrator restart.
It must not require a schema migration, database rewrite, or code edit. Startup
must reject any other value.

`REACTION_SCORER_MODE` is the sole rollout setting. Remove
`OI_SHADOW_ENABLED`; do not retain it as an alias or add per-module mode flags,
secondary booleans, or direct `os.environ` reads. The settings loader parses
the mode once into a typed enum and passes that value to the collection plan and
operational selector. Startup and cycle health must expose both the configured
mode and selected operational scorer version so an operator can verify that
`enforce` took effect.

Emergency rollback is `REACTION_SCORER_MODE=shadow`; existing anchor/profile
state remains available and collection continues.

## 5. Deep-module interfaces

### 5.1 Profile module

The external seam is one pure interface:

```text
build_value_profile(
  asset,
  evaluation_cutoff,
  completed_15m_bars,
  previous_anchor
) -> ValueProfileResult
```

The caller does not select bins, identify nodes, apply decay, or interpret
readiness. Those details remain inside the module and are versioned together.

### 5.2 Scorer module

The external seam is:

```text
score_candidate_v3(candidate, admission_proof, reaction_context)
  -> ReactionScoreResult
```

The scorer performs no network or database I/O. It receives immutable,
cutoff-bound inputs and returns data without side effects.

### 5.3 Operational selector

One selector receives v2, v3, and the rollout mode. Callers must not branch on
the mode independently. This keeps alpha-outbox revalidation, clash resolution,
and intent construction on the same operational result.

## 6. Timeframe and cutoff contract

Three days means exactly 72 completed hourly buckets or 288 completed 15m bars:

```text
72 hours * 4 completed 15m bars/hour = 288 completed 15m bars
```

Research Analyst continues reading canonical completed 5m bars from
`market.sqlite3` and resampling them locally into exact completed 15m groups.
There is no new 15m source.

For a candidate evaluated at cutoff `T`:

- profile input bars must have `source_end < reaction_bar_start`;
- the reaction bar may end at `T`;
- the reaction bar's volume must not influence the profile against which that
  reaction is scored;
- partial 15m groups, gaps, duplicate logical bars, and future bars are
  unavailable;
- a 5m-cadence candidate uses the latest completed 5m bar as its reaction bar
  against a profile built only from earlier completed 15m bars;
- a 15m-or-higher strategy may use its latest completed execution/setup bar as
  the reaction bar, but the same exclusion rule applies.

This separation is mandatory to prevent the signal candle from creating or
moving the node that it appears to react to.

## 7. Anchor algorithm

### 7.1 Hourly buckets

Aggregate each exact group of four 15m bars:

```text
hour_volume = sum(volume)
hour_start  = first.source_start
hour_end    = fourth.source_end
```

An incomplete or gapped hour is ineligible for anchor replacement.

### 7.2 Initial anchor

When no prior valid anchor exists, choose the eligible completed hour with the
largest volume in the preceding 72 hours. Ties resolve by:

1. most recent `hour_end`;
2. stable source-observation ID order.

### 7.3 Decay and replacement

Default half-life:

```text
VOLUME_PROFILE_ANCHOR_HALF_LIFE_HOURS=72
```

At completed hour `t`:

```text
decayed_anchor_strength =
  anchor_original_volume * 2 ** (-age_hours / half_life_hours)
```

Replace the anchor only when:

```text
new_hour_volume > decayed_anchor_strength
```

Equality keeps the existing anchor. A candidate hour with invalid or zero
volume cannot replace it. An anchor older than seven days is invalidated and
reseeded from the latest complete 72-hour window so low-activity assets cannot
retain an anchor indefinitely.

Every replacement persists the old and new anchor IDs, strengths, cutoff, and
reason. Replays reconstruct state from persisted observations only and perform
no exchange requests.

## 8. Decayed volume-at-price construction

Use completed 15m bars beginning at the retained anchor and ending before the
reaction bar, capped at seven days. At least 72 valid 15m bars are required;
288 are required for normal readiness.

### 8.1 Temporal weight

For every contributing bar:

```text
bar_weight = 2 ** (-bar_age_hours / VOLUME_PROFILE_HALF_LIFE_HOURS)
weighted_volume = bar.volume * bar_weight
```

Default `VOLUME_PROFILE_HALF_LIFE_HOURS=72`.

### 8.2 Price bins

Use a stable ATR-normalized bin width:

```text
bin_width = max(0.10 * ATR14_15m, 0.0005 * latest_close)
bin_index = floor(price / bin_width)
```

ATR must be computed only from bars eligible for the profile. Invalid ATR or
fewer than 72 bars returns `unavailable`.

Allocate each bar's weighted volume across every bin intersecting `[low, high]`
with triangular weights centered on typical price `(high + low + close) / 3`.
Normalize per-bar weights so their allocated sum equals `weighted_volume`.
Putting an entire 15m bar's volume into one typical-price bin is forbidden.

### 8.3 POC, value area, and HVNs

- POC is the bin with maximum decayed allocated volume.
- Value area expands from POC until it contains at least 70% of total decayed
  volume; expansion chooses the larger adjacent bin and resolves ties lower
  first.
- An HVN is a strict local maximum whose volume is at or above the 75th
  percentile and whose prominence is at least 10% of POC volume.
- Merge adjacent qualifying HVN bins into one node and use their
  volume-weighted center.
- Persist no more than eight HVNs, ordered by volume descending then price.

POC is always included as a node, even when it is also an HVN.

## 9. Reaction observation

Location alone is neutral. A reaction requires a node touch plus a directional
close/reclaim.

For each POC/HVN, calculate:

```text
distance_atr = abs(entry_price - node_price) / ATR14_15m
proximity = exp(-0.5 * (distance_atr / 0.35) ** 2)
```

Use the nearest node; ties prefer POC, then higher node volume, then lower
price.

### 9.1 Long support

Support requires all of:

- reaction-bar low reaches the node's `0.15 ATR` touch band;
- reaction-bar close is above the node;
- close location within the bar is at least 0.60;
- close is above open or above the preceding close.

### 9.2 Short support

Mirror the long rules:

- high reaches the touch band;
- close is below the node;
- inverse close location is at least 0.60;
- close is below open or below the preceding close.

### 9.3 Contradiction

A bar that touches the node and closes through it against candidate direction
with a close-location strength of at least 0.60 is contradictory.

### 9.4 Score mapping

```text
confirmed supporting reaction = 0.50 + 0.50 * proximity * confirmation_strength
confirmed contradiction       = 0.50 - 0.50 * proximity * confirmation_strength
near node without confirmation = 0.55
far from every node            = 0.50
unavailable profile/reaction   = 0.50, status=unavailable
```

Clamp to `[0, 1]`. Persist node ID/type, node price, distance ATR, touch band,
bar OHLC, confirmation terms, and profile version.

## 10. Scorer v3

### 10.1 Admission is not score

The following remain deterministic admission proof and receive no score
weight:

- identity and temporal validity;
- finite directional entry/SL/TP geometry;
- minimum reward/risk;
- freshness/readiness;
- symbol/effective-universe policy;
- selected structural-stop proof and distance policy.

Any failing admission proof emits no intent regardless of v2 or v3 score.

### 10.2 Weighted observations

The v3 score has four operational observations:

| Observation | Weight | Meaning |
| --- | ---: | --- |
| `value_reaction-v1` | 0.50 | confirmed direction-aware POC/HVN reaction |
| `participation-v1` | 0.20 | normalized RVOL participation |
| `oi-participation-v1` | 0.15 | price-confirmed derivatives participation |
| `crowding-v1` | 0.15 | direction/family-aware funding crowding |

Weights are fixed by scorer version and sum to `1.00`. They are not independent
runtime settings. `shadow` computes this exact weighted v3 score, while
`enforce` selects the already-tested result. There is no zero-weight OI mode.

Remove from the weighted score:

- identity, geometry, reward/risk, stop distance, symbol policy, and structural
  stop;
- duplicate `htf_bias`, `alignment`, `zone_context`, and `contradiction` legs;
- same-direction agreement, which becomes a deterministic tie-breaker after
  score comparison;
- regime family weight interpolation.

Regime-session continues controlling family activation and plugin scope. It is
not also a score multiplier. Family-aware interpretation remains internal to
the reaction and crowding observations.

### 10.3 OI confirmation

OI is directional only after comparison with candidate-aligned price movement.
For 15m and 60m horizons, calculate cutoff-bound price return, OI change, and
the absolute OI-change percentile within that asset's trailing history. Fixed
cross-asset OI thresholds are forbidden.

For each horizon:

| Candidate-aligned price | OI change | Observation |
| --- | --- | --- |
| positive | positive | support scaled from `0.50` toward `1.00` by OI-change percentile |
| positive | negative | closing-led move, `0.55` |
| negative | positive | opposing position expansion scaled from `0.50` toward `0.00` |
| negative | negative | adverse liquidation/covering, `0.35` |
| flat or unavailable | any | neutral `0.50` |

Combine horizons with fixed weights:

```text
oi_confirmation = 0.60 * oi_15m + 0.40 * oi_60m
```

An unavailable horizon contributes neutral `0.50`; weights are never
renormalized per candidate. Funding is not re-scored inside this observation:
same-side crowding remains the separate `crowding-v1` leg. OI cannot create a
direction, rescue failed admission, or alter TradeIntent geometry.

### 10.4 Threshold

Add:

```text
REACTION_SCORER_MIN_SCORE=0.50
```

The threshold is used only in `enforce`. Changing the mode must not silently
change the threshold. Score is an advisory quality multiplier, never a claimed
probability or direct producer-owned size instruction.

## 11. Persistence and ownership

`data/analyst.sqlite3` remains the only writer-owned store for scorer/profile
state. Add:

```sql
CREATE TABLE volume_profile_anchors (
  asset TEXT NOT NULL,
  anchor_version TEXT NOT NULL,
  anchor_hour_start TEXT NOT NULL,
  anchor_hour_end TEXT NOT NULL,
  original_volume REAL NOT NULL,
  current_strength REAL NOT NULL,
  evaluated_at TEXT NOT NULL,
  source_observation_ids_json TEXT NOT NULL,
  replacement_reason TEXT NOT NULL,
  PRIMARY KEY (asset, anchor_version, evaluated_at)
);

CREATE TABLE reaction_profiles (
  asset TEXT NOT NULL,
  evaluation_cutoff TEXT NOT NULL,
  profile_version TEXT NOT NULL,
  anchor_hour_start TEXT NOT NULL,
  profile_json TEXT NOT NULL,
  readiness TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (asset, evaluation_cutoff, profile_version)
);

CREATE TABLE reaction_score_comparisons (
  candidate_id TEXT PRIMARY KEY,
  rollout_mode TEXT NOT NULL,
  legacy_score REAL,
  reaction_score REAL,
  operational_score REAL NOT NULL,
  legacy_decision TEXT,
  reaction_decision TEXT,
  operational_version TEXT NOT NULL,
  observations_json TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
```

Retention: profiles and comparisons 30 days; anchor history 90 days, always
preserving the latest valid anchor per asset. Online retention is bounded and
never runs `VACUUM`.

## 12. Runtime placement and caching

Build profiles only for assets that emitted candidates. Within one cutoff,
compute one profile per `(asset, profile_version, reaction_frame)` and reuse it
across every candidate and strategy. The scorer never rebuilds a profile.

The orchestrator owns persistence and passes immutable results into the scorer.
The strategy runner remains source-blind and no plugin reads profile state.

## 13. Observability

Per cutoff record:

- rollout mode and operational scorer version;
- candidate assets, ready/unavailable profiles, and readiness reasons;
- anchors retained/replaced/reseeded and anchor ages;
- profile build p50/p95 latency and cache hits;
- POC/HVN reaction status counts;
- OI ready/unavailable status, 15m/60m states, and weighted contribution;
- v2/v3 score delta distribution;
- v2/v3 decision disagreement counts;
- selected/suppressed candidates under each version;
- profile and comparison rows written/pruned.

Logs must never describe a score as a fill, position, or execution result.

## 14. Tests

Required seam-level tests:

1. 288 15m bars equal three days; 72 15m bars equal 18 hours and are not
   accepted as full readiness.
2. Forming/future/gapped bars never enter an hour or profile.
3. The reaction bar is excluded from profile construction.
4. Anchor seeding and deterministic ties.
5. Exponential decay at 24h/48h/72h and exact replacement threshold.
6. Seven-day forced reseed.
7. Triangular allocation preserves each bar's weighted total volume.
8. Stable ATR bins, POC, value area, HVN prominence, and HVN merge rules.
9. Mirrored long/short support and contradiction reactions.
10. Location without confirmation does not score as a full reaction.
11. Missing profile produces neutral evidence and never fabricated support.
12. Admission failure blocks publication at every rollout mode.
13. Shadow mode preserves byte-equivalent v2 operational score/decision and
    intent quality score while computing the fully weighted OI-bearing v3.
14. Enforce mode uses v3 for threshold, clash resolution, alpha event, and bus
    source payload.
15. Switching shadow to enforce requires no state rewrite.
16. Replay performs no network I/O and reproduces the same profile and score.
17. One asset/cutoff profile is reused across multiple candidates.
18. The four price/OI states mirror correctly for long and short candidates.
19. With other observations fixed, OI support versus contradiction changes v3
    by exactly `0.15 * (support - contradiction)` and can change the verdict.
20. `off` performs no candidate-scoped OI fetch; `shadow` and `enforce` compute
    byte-equivalent v3 results from identical inputs.

## 15. Promotion gate

Remain in shadow for at least eight complete weeks. Compare v2 and v3 on the
same candidates using anchored/walk-forward splits and block-bootstrap
confidence intervals by cutoff/day.

Promotion to `enforce` requires:

- profile readiness of at least 95% for otherwise-admissible candidates;
- no lookahead or replay mismatch;
- positive lower confidence bound on the locked rank-discrimination metric;
- no material degradation by strategy, family, direction, or liquidity tier;
- improved score calibration or a demonstrably monotonic outcome relationship;
- no material orchestrator-latency or shared-bus regression; and
- a recorded operator decision naming the tested profile/scorer versions.

If outcome evidence is insufficient, keep `shadow`. Technical ability to toggle
`enforce` does not waive the promotion gate.
