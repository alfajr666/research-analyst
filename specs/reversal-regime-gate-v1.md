# Reversal Regime Gate v1

## Status

Locked design specification, agreed during the operator discussion on
2026-09-04 and assessed against `reversal-regime-proposal.md`.

This is a regime-family activation gate. It is not a trade trigger, a position
manager, a confidence score, or a replacement for candidate admission.

## 1. Assessment Verdict

The proposal is suitable with these decisions:

- regular RSI divergence only is correct for reversal activation;
- hidden divergence is explicitly excluded because it is continuation evidence;
- recent trend plus negative ADX slope is the correct context filter;
- an AND gate is appropriate because activation is a safety/context unlock;
- the reversal family must coexist with trend and mean-reversion families;
- the reversal gate must carry direction and confirmed pivot evidence;
- all pivots and indicators must use completed 1h data to prevent lookahead.

The proposal's open choices are locked below as v1 research defaults. RSI 9/11,
3-bar/7-bar fractals, and alternate lookbacks remain offline research variants;
they are not mixed into the live gate without a versioned comparison.

## 2. Role in the Regime Model

The regime model has three independently activatable strategy families:

```text
trend            <- continuous trend weight + hysteresis
mean_reversion   <- continuous range weight + hysteresis
reversal         <- regular divergence AND ADX rollover gate
```

These are scopes, not mutually exclusive labels. One asset may activate trend
and reversal simultaneously during a transition. It may also activate
mean-reversion and reversal if both independent conditions are present.

This overlap is intentional. A rolling-over trend can still be trend-like while
a reversal setup is forming. Trend and reversal strategies must make their own
entry decisions, and opposing candidates continue through deterministic clash
resolution.

The reversal gate must not deactivate trend or mean-reversion. It only controls
whether reversal-family plugins receive the asset.

## 3. Locked Defaults

### 3.1 Working timeframe

The working timeframe is completed `1h` bars.

The direct 4h regime cache supplies the long-history context and readiness. The
direct 1h regime cache supplies reversal timing. Recent volatility continues
to use the canonical completed 5m series. This regime-only 1h source does not
change strategy-facing 1h data.

### 3.2 RSI

```text
RSI period:             14
RSI implementation:     repository in-house RSI
```

RSI 9 and RSI 11 are research variants only. They require separate replay
results and a new gate or score version before consideration for live use.

### 3.3 Swing points

Use a confirmed 5-bar fractal on the 1h series:

```text
swing high at i: high[i] > high[i-2], high[i-1], high[i+1], high[i+2]
swing low at i:  low[i]  < low[i-2],  low[i-1],  low[i+1],  low[i+2]
```

A pivot is not usable until the two bars to its right have closed. The most
recent usable pivot therefore has an intentional two-hour confirmation delay.

3-bar and 7-bar fractals are offline variants only.

### 3.4 Divergence lookback

The default lookback is the most recent 48 completed 1h bars. The two compared
pivots must both be confirmed and lie within this comparison window.

20, 32, and 50 bars are research variants only. A variant must retain its own
configuration and result version.

### 3.5 ADX rollover

ADX uses the repository's in-house 1h ADX with length 14 and smoothing 14.

```text
recent_trend:
    max(ADX_1h over the previous 20 completed 1h readings) >= 25

decay:
    ordinary-least-squares slope(ADX_1h over the latest 5 completed readings)
    < 0
```

The recent-trend window is measured in completed 1h readings, not wall-clock
minutes. The `25` threshold and 20/5 windows are research defaults and are
versioned gate parameters.

The gate does not require 4h ADX itself to be decaying. A 4h trend may remain
strong while a confirmed 1h reversal is forming. The direct 4h score remains a
readiness and broader-context input.

## 4. Divergence Contract

Only regular divergence activates the gate.

### 4.1 Bearish regular divergence

For two confirmed 1h swing highs, older `A` and newer `B`:

```text
price_high[B] > price_high[A]
AND
RSI_high[B] < RSI_high[A]
```

This produces a `short` reversal direction.

### 4.2 Bullish regular divergence

For two confirmed 1h swing lows, older `A` and newer `B`:

```text
price_low[B] < price_low[A]
AND
RSI_low[B] > RSI_low[A]
```

This produces a `long` reversal direction.

### 4.3 Exclusions

The following do not activate reversal:

- hidden bullish divergence;
- hidden bearish divergence;
- unconfirmed pivots;
- price and RSI pivots from different pivot centers;
- pivots outside the 48-bar window;
- equal-price or equal-RSI comparisons that do not satisfy strict divergence;
- a divergence without the recent-trend and ADX-decay conditions.

The gate may record excluded divergence as diagnostic evidence, but excluded
signals must not appear in `active_families`.

## 5. Gate Logic

The public pure-function contract is:

```text
reversal_gate(asset, cutoff, bars_1h, adx_1h, rsi_1h) -> {
    active: bool,
    direction: "long" | "short" | "none",
    divergence_type: "regular_bullish" | "regular_bearish" | "none",
    divergence_detected: bool,
    recent_trend_detected: bool,
    adx_decay_detected: bool,
    pivot_ids: [...],
    reasons: [...],
    gate_version: "reversal-gate-v1"
}
```

The activation rule is:

```text
reversal_active(asset) =
    regular_divergence_detected
    AND recent_trend_detected
    AND adx_decay_detected
```

The gate does not use a weighted score for activation. A continuous
`reversal_weight` may remain in the broader regime observation as diagnostic
research output, but it must not override or bypass this boolean gate.

The gate is recomputed at every completed 5m cutoff using the newest preceding
completed 1h bars. It has no wall-clock TTL independent of its input bars.
Repeated observations with the same pivot IDs are expected and must be handled
by candidate identity/deduplication downstream.

## 6. Family Coexistence

Family activation is evaluated independently:

```text
asset ETH
  trend active:          yes
  mean_reversion active: no
  reversal active:       yes, direction=short
```

The evaluator behavior is:

- trend plugins may receive ETH;
- mean-reversion plugins do not receive ETH;
- reversal plugins receive ETH and the reversal evidence;
- the reversal gate does not choose the entry, stop, target, or size;
- any opposing trend/reversal candidates use existing deterministic clash
  resolution;
- an unresolved opposing clash produces no intent;
- candidate-level admission remains mandatory for every family.

An asset-level data or session block still blocks all new strategy evaluation
for that asset. A missing divergence or missing previous ADX value blocks only
the reversal family when the trend/range score itself is otherwise ready.

## 7. Evidence Contract

The persisted gate decision must include:

```text
reversal_gate_version
working_timeframe
rsi_period
fractal_width
lookback_bars
adx_length
adx_smoothing
adx_recent_threshold
adx_recent_lookback
adx_decay_lookback
divergence_type
direction
price_pivot_ids
rsi_pivot_ids
price_pivot_values
rsi_pivot_values
adx_recent_max
adx_decay_slope
source_observation_ids
reasons
```

The evidence is immutable and point-in-time. It must never include the two
right-hand bars of an unconfirmed pivot before their cutoff.

## 8. Failure and Unknown Behavior

- Missing 1h bars: reversal inactive for the asset; record `missing_1h_data`.
- Missing RSI or ADX inputs: reversal inactive; record the exact missing input.
- Insufficient direct 1h or 4h regime history: asset-level regime block remains
  in force under `enforce`.
- No regular divergence: reversal inactive, with trend/range unaffected.
- ADX never recently exceeded 25: reversal inactive.
- ADX slope is zero or positive: reversal inactive.
- Both bullish and bearish regular divergences are simultaneously detected:
  reversal is inactive with `ambiguous_divergence`; no direction is selected.
- Malformed or future data: fail closed for the reversal family or affected
  asset, depending on the scope of the malformed input.

## 9. Strategy Boundary

Reversal activation is not a trade trigger. A reversal plugin must still define
and validate:

- entry confirmation after the divergence;
- direction-specific setup rules;
- structural reference and stop placement;
- target and expiry geometry;
- reward/risk and ATR constraints;
- symbol-account policy;
- candidate evidence and deduplication.

The gate cannot move a stop, produce an intent, override admission, or change
executor protections.

## 10. Validation Matrix

Required deterministic tests include:

- regular bullish divergence activates only with recent trend and ADX decay;
- regular bearish divergence activates only with recent trend and ADX decay;
- hidden bullish divergence does not activate;
- hidden bearish divergence does not activate;
- 3-bar and 7-bar alternatives are not accidentally used by v1;
- unconfirmed 5-bar pivots do not activate;
- pivot lookback boundaries are exact;
- RSI and price pivot centers are paired correctly;
- ADX threshold history is required;
- positive, flat, and negative ADX slopes behave as specified;
- ambiguous opposing divergences fail closed;
- trend and reversal can both be active for one asset;
- reversal inactivity does not disable trend or mean-reversion;
- no future source observation IDs enter the gate evidence;
- repeated 5m cutoffs reuse the same confirmed evidence deterministically;
- reversal candidates still pass ordinary admission and clash resolution.

Replay validation must compare RSI 14 against offline RSI 9 and RSI 11, and
5-bar against 3-bar and 7-bar fractals. Those comparisons must be reported by
rotation cohort, asset, session, direction, strategy family, and normalized R
outcome before any parameter promotion.

## 11. Rollout

The gate is exposed in `shadow` mode first. Shadow output must include:

- whether reversal would activate;
- direction and pivot evidence;
- which AND component failed;
- whether trend and/or mean-reversion were also active;
- downstream candidate and clash outcomes.

`enforce` may activate reversal-family routing only after replay, lookahead,
coexistence, and candidate-admission validation pass. Rollback disables regime
enforcement without changing existing positions or executor behavior.

## 12. Explicit Non-Goals

- No hidden divergence activation.
- No reversal trade trigger in the regime worker.
- No reversal-specific sizing or confidence override.
- No automatic suppression of trend or mean-reversion.
- No session-to-reversal mapping.
- No direct 1h/4h WebSocket subscription.
- No TA-library replacement for in-house RSI or ADX.
