# EMA9 ADX StochRSI State v1

## Status

Implementation specification. The strategy is enabled in the agreed portfolio
rollout as a compact Hyro strategy. Runtime activation still requires a
controlled service restart and post-restart verification.

## Identity and Scope

```text
strategy_id: ema9-adx-stochrsi-state-v1
plugin_version: v1
entry_timeframe: 1m
structure_timeframe: 5m
trend_timeframe: 1h
direction: long and short
```

The plugin is symbol-dumb and account-agnostic. It evaluates the symbol scope
provided by the evaluator. Account routing, symbol policy, sizing, leverage,
venue precision, orders, fills, and receipts remain downstream concerns.

The plugin runs only on finalized 1M cutoffs. The 5M structure and 1H ADX are
the latest completed higher-timeframe values available at that cutoff. No
forming bar is allowed into an entry or exit calculation.

## Parameters

| Parameter | Default | Meaning |
|---|---:|---|
| 1H ADX length | 14 | DMI/ADX DI length |
| 1H ADX smoothing | 14 | Wilder ADX smoothing, fixed to the ADX length input |
| 1H minimum ADX | 20.0 | Strict trend gate: ADX must be greater than this value |
| RSI length | 14 | RSI input for 1M and 5M StochRSI and 5M exit RSI |
| StochRSI length | 14 | RSI range window |
| K smoothing | 3 | SMA smoothing of raw StochRSI |
| D smoothing | 3 | SMA smoothing of K |
| EMA length | 9 | 1M and 5M EMA |
| ATR length | 14 | 5M Wilder ATR |
| ATR multiplier | 2.0 | Structural stop buffer |
| 5M structure bars | 15 | Number of preceding completed 5M candles |
| Long extension offset | 5.0% | 5M close must exceed EMA9 by this percentage |
| Short extension offset | 5.0% | 5M close must be below EMA9 by this percentage |
| Long extension K threshold | 80.0 | 5M K threshold |
| Short extension K threshold | 20.0 | 5M K threshold |
| Long momentum RSI threshold | 75.0 | 5M RSI must be greater than this value |
| Short momentum RSI threshold | 25.0 | 5M RSI must be less than this value |
| Long momentum extreme | 80.0 | 1M K memory threshold |
| Short momentum extreme | 20.0 | 1M K memory threshold |
| Entry validity | 5 minutes | Candidate expiry after the completed 1M signal bar |

The executor derives the fixed 2R target from the emitted entry and stop:

```text
long target  = entry + 2 * (entry - stop)
short target = entry - 2 * (stop - entry)
```

The analyst emits no quantity, risk amount, leverage, order type, or account
route.

## Indicator Definitions

All indicator inputs are close-based unless stated otherwise.

```text
RSI       = Wilder RSI(close, 14)
raw_stoch = 0 when highest(RSI, 14) == lowest(RSI, 14)
            100 * (RSI - lowest(RSI, 14)) /
            (highest(RSI, 14) - lowest(RSI, 14)) otherwise
K         = SMA(raw_stoch, 3)
D         = SMA(K, 3)
EMA9      = EMA(close, 9)
ATR14     = Wilder ATR(14)
```

The 1H trend input is the confirmed ADX from DMI(14, 14). DI values are
recorded for evidence but do not select long versus short. Direction comes from
the 1M K/D crossover.

## Entry Flow

```text
Completed 1M cutoff
        |
        v
Load latest completed 1H ADX
        |
        +-- ADX <= 20.0 -> reject symbol
        |
        +-- ADX > 20.0
                |
                v
Calculate latest completed 1M K, D, close, and EMA9
                |
                +-- Long trigger:
                |     K crosses above D
                |     1M close > 1M EMA9
                |
                +-- Short trigger:
                      K crosses below D
                      1M close < 1M EMA9
                |
                v
Load the latest completed 5M bar and its preceding 14 bars
                |
                +-- Long structure:
                |     all 15 closes >= their corresponding 5M EMA9
                |
                +-- Short structure:
                      all 15 closes <= their corresponding 5M EMA9
                |
                v
Require the symbol to be flat for this strategy and direction
                |
                v
Lock structural stop from the same 15-bar 5M window
                |
                +-- Long:  lowest low - 2 * 5M ATR14
                +-- Short: highest high + 2 * 5M ATR14
                |
                v
Emit candidate with 5-minute validity
                |
                v
Admission and clash resolution
                |
                v
Selected candidate reaches executor
                |
                +-- Executor attaches structural SL
                +-- Executor derives and attaches external 2R TP
```

The 5M structure is evaluated from the preceding 15 completed bars, explicitly
excluding a forming 5M candle. Equality is valid for both directions. A long
and short signal cannot both be emitted from one crossover because the latest
K/D crossover has one direction.

## Position Exit Flow

The fixed 2R target and structural stop remain active independently in the
executor. The strategy exit policy is evaluated from completed bars and returns
an exit signal for the position-management layer.

```text
Open position
        |
        +-- Executor hard SL hit -> close; never vetoable
        |
        +-- Executor external 2R TP hit -> close
        |
        +-- Otherwise evaluate strategy exits
                |
                +-- Long extension TP
                |     5M close >= 5M EMA9 * 1.05
                |     5M K >= 80
                |     5M K crosses above 5M D
                |
                +-- Short extension TP
                |     5M close <= 5M EMA9 * 0.95
                |     5M K <= 20
                |     5M K crosses below 5M D
                |
                +-- Long momentum exit
                |     1M K has reached >= 80 since entry
                |     1M K crosses below 1M D
                |     5M RSI > 75
                |
                +-- Short momentum exit
                      1M K has reached <= 20 since entry
                      1M K crosses above 1M D
                      5M RSI < 25
```

If both strategy exit rules fire on the same cutoff, the extension rule is
reported first because it is evaluated first in the Pine strategy. A strategy
exit must never cancel or move the executor's hard stop or external 2R target.

The implementation exposes this policy as a pure, deterministic evaluator. It
does not create a second order writer or bypass the locked PM sidecar contract.
The current PM sidecar remains the authority for advisory management decisions;
true 1M mechanical decision delivery requires a separate PM cadence/integration
rollout.

## Candidate Contract

Entry candidates include:

```text
strategy_id
plugin_version
input_snapshot_id
asset
direction
observed_at
valid_until
entry_price = completed 1M close
invalidation_price = locked structural stop
feature_snapshot = confirmed ADX/DI, EMA, RSI, StochRSI, ATR, structure
metadata = target and exit policy descriptions
```

`targets` is intentionally omitted. The producer/executor contract derives a
2R target from the valid entry and stop. The candidate still passes the normal
admission geometry and RR gates after that derived target is applied.

## Data and Warmup

The plugin requires:

- Fresh completed 1M data through the evaluation cutoff.
- At least enough 1M bars for RSI14, StochRSI14, K3, and D3, plus one prior bar
  for crossover detection.
- At least 15 completed 5M structure bars, plus indicator warmup history for
  EMA9 and ATR14.
- Enough completed 1H bars for DMI/ADX14 with smoothing14.

Missing or stale data produces no candidate. It must not be replaced with a
forming bar or an alternate locally fetched higher-timeframe series.

## Recommendation

1. Keep this as a separate strategy ID rather than altering
   `ema9-continuation-stochrsi-v1`; the entry order and exit contract differ.
2. Use completed-bar Python semantics rather than reproducing Pine's potential
   realtime higher-timeframe repaint behavior.
3. Validate the plugin against TradingView-exported OHLCV and signal vectors
   before considering activation.
4. Verify the Hyro route, external 2R target, and structural stop after the
   controlled rollout restart.
5. Keep the strategy in the configured enabled and active portfolio allowlists.

## Required Tests

1. Non-1M evaluation is rejected or produces no candidate.
2. ADX equal to or below the minimum rejects; ADX above it permits evaluation.
3. Long and short 1M K/D crossovers require the correct EMA9 side.
4. The 5M structure uses exactly the preceding 15 completed bars.
5. Equality at the 5M EMA9 boundary is accepted.
6. Structural long and short stops use the correct extreme and ATR buffer.
7. Candidate expiry is five minutes, `targets=[]` is explicit, and quantity is
   not emitted.
8. The external 2R target is derived correctly by the intent contract.
9. Long and short extension exits match the completed 5M conditions.
10. Momentum exits require the post-entry 1M extreme memory and 5M RSI filter.
11. Exit evaluation does not mutate executor protection or place orders.
12. Registry presence, enabled allowlisting, and Hyro routing agree.
13. End-to-end selected candidates pass through admission and produce the
    expected executor envelope without analyst-owned sizing.
