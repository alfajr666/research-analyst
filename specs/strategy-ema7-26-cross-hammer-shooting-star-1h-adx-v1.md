# EMA7/26 Cross Hammer/Shooting Star 1H ADX v1

## Status

Locked implementation specification. The strategy is enabled in the agreed
portfolio rollout as a Fundamo strategy. Runtime activation still requires a
controlled service restart and post-restart verification.

The implementation follows the Pine strategy's 5M execution model while making
the higher-timeframe data point-in-time safe and correcting the EMA26 proximity
condition to mean actual proximity rather than a one-sided price filter.

## Identity and Scope

```text
strategy_id: ema7-26-cross-hammer-shooting-star-1h-adx-v1
plugin_version: v1
entry_timeframe: 5m
setup_timeframe: 5m
trend_timeframe: 1h
direction: long and short
```

The plugin is symbol-dumb and account-agnostic. It does not own symbol
eligibility, account routing, sizing, leverage, order type, venue precision,
orders, fills, position state, or execution receipts. Those remain downstream
responsibilities.

The strategy owns:

- 5M cross and setup detection
- confirmed 1H ADX filtering
- the 5M setup-candle extreme used for invalidation
- the 5M ATR stop buffer
- the deterministic TA exit policy

The strategy does not own the external 2R target. The executor derives that
target from the entry and analyst-provided stop.

## Locked Interpretation

- Evaluate only at completed 5M cutoffs.
- Use only completed 5M bars for EMA, RSI, ATR, cross, candle pattern, setup
  scan, stop, and exit calculations.
- Use only the latest completed 1H bar for ADX.
- Derive 1H bars from canonical completed 5M observations using the repository's
  point-in-time resampling path.
- A cross on the current 5M bar may use a setup candle only from bars 1 through
  10 bars before the cross; the current cross bar is never a setup candle.
- Search setup candles from nearest to farthest. The first qualifying setup is
  the one whose extreme is saved.
- ADX is a hard gate before setup evaluation and entry construction.
- Direction is selected by the current EMA cross. DI values are evidence only;
  they do not select or veto direction.
- Do not reproduce any realtime higher-timeframe repainting from Pine.

## Parameters

| Parameter | Default | Meaning |
|---|---:|---|
| Fast EMA length | 7 | 5M fast EMA |
| Slow EMA length | 26 | 5M slow EMA and exit reference |
| Setup lookback bars | 10 | Prior completed 5M bars scanned before a cross |
| EMA26 proximity | 0.25% | Symmetric maximum close-to-EMA26 distance |
| Minimum candle body | greater than 0 | Rejects degenerate zero-body candles |
| Hammer lower-wick ratio | 2.0 | Lower wick must be at least body times this value |
| Hammer upper-wick ratio | 0.5 | Upper wick must be at most body times this value |
| Shooting-star upper-wick ratio | 2.0 | Upper wick must be at least body times this value |
| Shooting-star lower-wick ratio | 0.5 | Lower wick must be at most body times this value |
| RSI length | 14 | 5M Wilder RSI used at entry and exit |
| Entry RSI minimum | 40 | Inclusive lower entry boundary |
| Entry RSI maximum | 60 | Inclusive upper entry boundary |
| ATR length | 14 | 5M Wilder ATR used for the stop buffer |
| ATR stop multiplier | 1.0 | Multiplier applied to current ATR at the cross |
| 1H ADX length | 14 | DMI directional length |
| 1H ADX smoothing | 14 | ADX smoothing length |
| 1H ADX minimum | 20 | Inclusive trend-strength threshold |
| Long exit RSI maximum | 28 | Strict long exit threshold |
| Short exit RSI minimum | 72 | Strict short exit threshold |
| Exit EMA spread | 50 bps | Strict minimum EMA7/EMA26 separation |
| Entry validity | 5 minutes | Candidate expiry after the cross cutoff |

All parameters must be configuration-backed. The defaults above are the locked
v1 values and must not be silently changed by the implementation.

## Indicator Definitions

```text
EMA7       = EMA(5M close, 7)
EMA26      = EMA(5M close, 26)
RSI5       = Wilder RSI(5M close, 14)
ATR5       = Wilder ATR(5M OHLC, 14)
ADX1H      = DMI(1H, length=14, smoothing=14).ADX
```

ADX is strength-only. `plusDI_1h` and `minusDI_1h` are recorded in the feature
snapshot but do not select the long or short branch.

The EMA spread used by exits is:

```text
emaSpreadBps = abs(EMA7 - EMA26) / EMA26 * 10000
```

If EMA26 is unavailable or non-positive, the candidate or exit evaluation must
return no signal.

## Candle Pattern Definitions

For each completed 5M candle:

```text
body      = abs(close - open)
upperWick = high - max(open, close)
lowerWick = min(open, close) - low
```

Degenerate zero-body candles are not eligible. No candle-color requirement is
added: both bullish and bearish candles may qualify, matching the Pine wick
logic without allowing the zero-body edge case.

```text
shootingStar = body > 0
               and upperWick >= body * 2.0
               and lowerWick <= body * 0.5

hammer       = body > 0
               and lowerWick >= body * 2.0
               and upperWick <= body * 0.5
```

Invalid OHLC geometry, missing values, or negative wick values make the candle
ineligible rather than being normalized by the strategy.

## EMA26 Setup Proximity

The setup candle's close must be within a symmetric 25-basis-point distance of
its own completed-candle EMA26:

```text
nearEMA26 = abs(close - EMA26) / EMA26 <= 0.0025
```

This intentionally replaces the Pine expressions:

```text
close <= EMA26 * 1.0025
close >= EMA26 * 0.9975
```

Those expressions accept any price on one side of EMA26 and therefore do not
represent proximity.

## EMA Cross Definitions

At the current completed 5M bar `t`:

```text
goldenCross = EMA7[t - 1] <= EMA26[t - 1]
              and EMA7[t] > EMA26[t]

deathCross  = EMA7[t - 1] >= EMA26[t - 1]
              and EMA7[t] < EMA26[t]
```

The equality cases are inclusive on the prior bar, matching crossover and
crossunder behavior. A candidate is never emitted without a cross on the
current 5M cutoff.

## Setup Candle Selection

### Short setup

On a current `deathCross`, inspect completed bars `t-1` through `t-10` in that
order. The first bar satisfying both conditions is selected:

```text
shootingStar[i] and nearEMA26[i]
```

Save its high as `shortStarHigh`.

### Long setup

On a current `goldenCross`, inspect completed bars `t-1` through `t-10` in that
order. The first bar satisfying both conditions is selected:

```text
hammer[i] and nearEMA26[i]
```

Save its low as `longHammerLow`.

If no qualifying setup exists, reject the cross. Setup state is local to the
current evaluation and must not leak from an earlier cross.

## Entry Flow

```text
Completed 5M cutoff
        |
        v
Filter all input bars to source_end <= cutoff
        |
        +-- missing, future, or stale data -> reject
        |
        v
Read latest completed 1H ADX(14,14)
        |
        +-- ADX < 20 -> reject before setup evaluation
        |
        +-- ADX >= 20
                |
                v
Calculate current completed 5M EMA7/EMA26 cross
                |
                +-- no current golden/death cross -> reject
                |
                v
Scan prior completed 5M bars 1..10
for the direction's candle setup near EMA26
                |
                +-- no setup -> reject
                |
                v
Require completed 5M RSI in [40, 60]
                |
                +-- outside inclusive range -> reject
                |
                v
Lock current 5M close as entry
and current 5M ATR14 stop buffer
                |
                v
Emit candidate with five-minute validity
                |
                v
Admission, clash resolution, and downstream flat-position policy
                |
                v
Executor attaches the stop and derives external 2R
```

ADX minimum and entry RSI boundaries are inclusive. The EMA cross, setup
candle, RSI, ATR, and entry price must all come from the same completed 5M
cutoff.

## Entry Conditions

### Short

All conditions must be true:

```text
ADX1H >= 20
deathCross on the current 5M candle
qualifying shooting star exists in prior bars 1..10
RSI5 in [40, 60]
```

### Long

All conditions must be true:

```text
ADX1H >= 20
goldenCross on the current 5M candle
qualifying hammer exists in prior bars 1..10
RSI5 in [40, 60]
```

## Stop and Candidate Contract

The entry price is the current completed 5M close.

```text
short invalidation = shortStarHigh + ATR5 * 1.0
long invalidation  = longHammerLow - ATR5 * 1.0
```

Reject non-positive prices and directionally invalid geometry:

```text
long:  0 < invalidation_price < entry_price
short: 0 < entry_price < invalidation_price
```

Admission remains authoritative for freshness, stop-distance, 4H ATR floor,
symbol policy, conflict handling, and flat-position requirements.

The emitted event must include the repository's required fields, including an
empty target list:

```text
schema_version, strategy_id, plugin_version, alpha_id/dedupe identity,
asset, direction, setup_class, phase, observed_at, valid_until,
horizon_minutes, confidence, entry_condition, entry_price,
invalidation_price, targets=[], feature_snapshot, metadata
```

`targets=[]` records that the strategy intentionally has no target. It is not a
strategy target. The downstream intent builder derives the external 2R target
from entry and stop:

```text
long target  = entry + 2 * (entry - invalidation_price)
short target = entry - 2 * (invalidation_price - entry)
```

The strategy must not emit quantity, amount, risk amount, leverage, account,
route, or venue-specific order fields.

Suggested metadata and evidence fields include:

```text
execution_timeframe: 5m
setup_timeframe: 5m
trend_timeframe: 1h
target_policy: executor_derived_2r
stop_policy: setup_extreme_plus_atr
setup_timestamp
setup_high/setup_low
ema7_5m
ema26_5m
rsi14_5m
atr14_5m
adx14_1h
plus_di14_1h
minus_di14_1h
ema_spread_bps
```

## TA Exit Policy

The Pine TA exit conditions remain intact. Evaluate them only against the latest
completed 5M bar for an open position of this strategy.

```text
long exit  = RSI5 < 28 and emaSpreadBps > 50
short exit = RSI5 > 72 and emaSpreadBps > 50
```

The RSI thresholds are strict. The EMA spread threshold is strict. The spread
uses the absolute EMA difference and is independent of position direction.

The exit evaluator returns a deterministic policy signal such as:

```text
{
  "action": "exit",
  "side": "long" | "short",
  "rule_name": "long_rsi_ema_spread_exit" | "short_rsi_ema_spread_exit",
  "cutoff": "...",
  "inputs": {"rsi5m": ..., "ema7_5m": ..., "ema26_5m": ..., "spread_bps": ...}
}
```

It does not place orders, cancel protection, move stops, or create a second
order writer. The executor-owned stop and external 2R target remain active
independently. Until a compatible mechanical policy integration is separately
approved, the current LLM-only PM sidecar remains the live position-management
authority; the TA exit can be recorded and tested without silently changing
that ownership.

## Data, Freshness, and Warmup

The plugin requires:

- fresh completed 5M bars through the cutoff
- enough 5M history for EMA26, ATR14, and the ten-bar setup scan
- at least one latest completed 1H bar with valid ADX14/14
- sufficient 1H history for DMI/ADX warmup, approximately 43 completed 1H bars
  under the repository's current implementation

Missing, future, stale, or invalid bars produce no candidate. A forming 5M or
1H bar must never affect a cross, setup pattern, ATR stop, RSI filter, or exit.

## Registration and Activation

The implementation must:

- add the strategy to `KNOWN_STRATEGIES`
- add it to the admission strategy set
- register the builtin plugin at `5m` cadence
- classify it consistently for price-structure handling
- add configuration-backed parameters
 - keep it present in the configured enabled and active portfolio allowlists

Account routing is intentionally unspecified. If a later deployment assigns it
to a Fundamo or other account route, update the corresponding routing and
allowlist contracts in the same deployment change rather than embedding route
logic in the plugin.

## Required Tests

1. The strategy is registered at `5m` cadence and present in the configured
   enabled and active allowlists.
2. ADX below 20 rejects before setup and entry evaluation.
3. ADX equal to 20 passes the hard gate.
4. Golden and death crosses use completed current and previous EMA values.
5. Setup scanning excludes the current cross candle.
6. Setup scanning covers exactly bars 1 through 10 and chooses the nearest
   qualifying setup.
7. Hammer and shooting-star wick inequalities match the locked definitions.
8. Zero-body candles are rejected.
9. EMA26 proximity is symmetric and rejects prices beyond 25 bps on either side.
10. RSI boundaries 40 and 60 are inclusive for entry.
11. Long and short stops use the saved setup extreme plus/minus current 5M ATR14.
12. Invalid directional stop geometry rejects the candidate.
13. Candidate validity is five minutes after the completed cross cutoff.
14. Candidate emits `targets=[]` and no sizing, routing, or target fields owned
    by the strategy.
15. Downstream intent construction derives external 2R from the analyst stop.
16. Long TA exit uses RSI `<28` and spread `>50` bps.
17. Short TA exit uses RSI `>72` and spread `>50` bps.
18. Exit thresholds are strict and price/EMA data is completed and point-in-time.
19. Future and stale 5M/1H bars are excluded.
20. The full regression suite passes with the strategy enabled but before any
    production service restart.

## Operational Recommendation

Keep the plugin in the configured portfolio and validate candidates and stops
against TradingView exports using completed 5M bars and confirmed 1H ADX. The
controlled restart must verify Fundamo routing before live candidate delivery.
TA exit consumption remains subject to the existing PM ownership contract.
