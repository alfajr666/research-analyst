# EMA99 Double Touch StochRSI State v1

## Status

Implementation specification. The strategy is enabled in the agreed portfolio
rollout as a Fundamo strategy. Runtime activation still requires a controlled
service restart and post-restart verification.

## Locked Interpretation

The Pine source is locked to a 5M execution chart, but its labels describe a
true 1M touch and oscillator engine. This implementation follows the intended
semantics rather than the literal chart-local calculations:

- EMA99, RSI, and StochRSI touch/trigger data use completed 1M bars.
- Evaluation and execution occur at each completed 5M cutoff.
- 5M EMA7/EMA26 crosses, RSI, ATR, and exits use completed 5M bars.
- 1H ADX uses the latest completed 1H bar.
- Higher-timeframe data is point-in-time and derived from canonical completed
  5M observations.
- A second touch must occur on a later 1M candle than the first touch.
- ADX is a hard gate before touch state is replayed or an entry is considered.

## Identity and Scope

```text
strategy_id: ema99-double-touch-stochrsi-state-v1
plugin_version: v1
entry_timeframe: 5m
trigger_timeframe: 1m
structure_timeframe: 5m
trend_timeframe: 1h
direction: long and short
```

The plugin is symbol-dumb and account-agnostic. Routing, symbol policy,
sizing, leverage, venue precision, orders, fills, and receipts remain owned by
downstream admission and execution services.

## Parameters

| Parameter | Default | Meaning |
|---|---:|---|
| 1M EMA touch length | 99 | EMA used for touch detection |
| EMA proximity | 0.5% | Distance accepted as an EMA touch |
| 1M RSI length | 14 | Entry RSI |
| 1M RSI minimum | 40 | Inclusive lower entry boundary |
| 1M RSI maximum | 60 | Inclusive upper entry boundary |
| StochRSI RSI length | 14 | RSI input length |
| StochRSI length | 14 | RSI high/low range length |
| K smoothing | 3 | SMA smoothing of raw StochRSI |
| D smoothing | 3 | SMA smoothing of K |
| Stoch overbought | 80 | Short trigger K threshold |
| Stoch oversold | 20 | Long trigger K threshold |
| 5M fast EMA | 7 | Fast trend EMA |
| 5M slow EMA | 26 | Slow trend EMA and TP reference |
| 5M cross lookback | 10 | Inclusive age in completed 5M bars |
| 5M RSI length | 14 | Exit RSI |
| 5M ATR length | 14 | Structural stop ATR |
| ATR multiplier | 1.0 | Stop buffer |
| 1H ADX length | 14 | DMI/ADX length |
| 1H ADX minimum | 20 | Inclusive hard trend threshold |
| Long TP RSI | 70 | Strict long exit RSI threshold |
| Short TP RSI | 30 | Strict short exit RSI threshold |
| EMA26 TP extension | 3% | Distance beyond 5M EMA26 |
| Entry validity | 5 minutes | Candidate expiry |

The candidate intentionally omits a target. The executor contract derives the
external fixed 2R target:

```text
long target  = entry + 2 * (entry - stop)
short target = entry - 2 * (stop - entry)
```

The analyst emits no quantity, risk amount, leverage, order type, or account
route.

## Indicator Definitions

```text
EMA99       = EMA(1M close, 99)
RSI1        = Wilder RSI(1M close, 14)
raw StochRSI = 0 when RSI high == RSI low
              100 * (RSI - RSI low) / (RSI high - RSI low) otherwise
K1/D1       = SMA(raw StochRSI, 3) / SMA(K1, 3)

EMA7/EMA26  = EMA(5M close, 7/26)
RSI5        = Wilder RSI(5M close, 14)
ATR5        = Wilder ATR(5M, 14)
ADX1H       = DMI(14, 14) ADX
```

ADX measures trend strength only. DI values are recorded as evidence but do
not choose direction.

## Touch State

Touch state is replayed over completed 1M bars for the current evaluation.
State is reset whenever the ADX gate is not satisfied.

Short state:

```text
touch1 absent and (1M high >= EMA99 or near EMA99), with close <= EMA99
    -> touch1 = true; save first-touch high

touch1 and close > EMA99
    -> clear touch1, touch2, and saved high

touch1 and no touch2 and a later candle touches EMA99
    -> touch2 = true
```

Long state is the exact mirror:

```text
touch1 absent and (1M low <= EMA99 or near EMA99), with close >= EMA99
    -> touch1 = true; save first-touch low

touch1 and close < EMA99
    -> clear touch1, touch2, and saved low

touch1 and no touch2 and a later candle touches EMA99
    -> touch2 = true
```

The first and second touch must have different 1M timestamps. A single 1M
candle cannot satisfy both touches.

## Entry Flow

```text
Completed 5M cutoff
        |
        v
Read latest completed 1H ADX
        |
        +-- ADX < 20 -> reject and do not replay touch state
        |
        +-- ADX >= 20
                |
                v
Replay 1M EMA99 touch state over completed 1M bars
                |
                v
Read the latest completed 1M trigger bar
                |
                +-- Long:
                |     touch2 active
                |     K1 <= 20
                |     K1 crosses above D1
                |     RSI1 in [40, 60]
                |     latest 5M golden cross <= 10 bars old
                |
                +-- Short:
                      touch2 active
                      K1 >= 80
                      K1 crosses below D1
                      RSI1 in [40, 60]
                      latest 5M death cross <= 10 bars old
                |
                v
Require symbol/account to be flat downstream
                |
                v
Lock 5M execution price and structural stop
                |
                +-- Long: 5M close; first-touch low - 1 * ATR5
                +-- Short: 5M close; first-touch high + 1 * ATR5
                |
                v
Emit candidate with five-minute validity
                |
                v
Admission and clash resolution
                |
                v
Executor attaches structural SL and derives external 2R TP
```

RSI range boundaries are inclusive. ADX minimum is inclusive. EMA touch
invalidation is directional and is evaluated during 1M state replay.

## Exit Flow

The executor-owned structural stop and external 2R target remain active
independently of strategy exits.

```text
Open position
        |
        +-- Executor structural SL hit -> close; never vetoable
        |
        +-- Executor external 2R TP hit -> close
        |
        +-- Otherwise evaluate the latest completed 5M candle
                |
                +-- Long exit:
                |     5M RSI > 70
                |     close >= EMA26 * 1.03
                |
                +-- Short exit:
                      5M RSI < 30
                      close <= EMA26 * 0.97
```

The deterministic exit evaluator returns a policy signal only. It does not
place orders, move protection, or create a second order writer. The current
LLM-only PM sidecar remains the live management authority until a compatible
mechanical policy integration is separately approved.

## Candidate Contract

Entry events include:

```text
strategy_id, plugin_version, input_snapshot_id, asset, direction,
observed_at, valid_until, entry_price, invalidation_price,
feature_snapshot, metadata
```

`targets` is omitted so the producer/executor derives 2R. No sizing or routing
fields are emitted by the plugin.

## Data and Warmup

The plugin requires fresh completed 1M data through the cutoff, enough 1M
history for EMA99 and StochRSI warmup, at least 26 completed 5M bars plus ATR
warmup, and enough completed 1H history for DMI/ADX14 with smoothing14.

Missing, future, or stale data produces no candidate. Forming bars must never be
used to create touch state, cross state, stops, or exits.

## Recommendation

1. Keep the true 1M semantics and evaluate only at completed 5M cutoffs.
2. Keep distinct-touch and ADX-state repairs; do not reproduce the Pine
   same-candle double-touch behavior.
3. Use confirmed higher-timeframe values even where Pine realtime requests may
   repaint.
4. Keep the strategy registered and active under the agreed Fundamo routing;
   continue audit and paper validation during rollout.
5. Validate against TradingView-exported data after deciding whether the
   TradingView comparison is literal 5M behavior or corrected true 1M behavior.

## Required Tests

1. The strategy is registered at 5M cadence and present in the configured
   enabled and active allowlists.
2. ADX below the minimum rejects before touch/oscillator evaluation.
3. ADX equal to the minimum passes.
4. A second touch requires a later 1M candle.
5. Long and short touch invalidation clears state correctly.
6. True 1M RSI/StochRSI values drive the trigger.
7. Long and short 5M EMA cross recency is inclusive through ten bars.
8. Long and short RSI boundaries are inclusive.
9. Stops use the saved first-touch extreme plus/minus one 5M ATR.
10. Candidate expiry is five minutes, `targets=[]` is explicit, and no sizing is
    emitted.
11. Long and short dynamic 5M exits use strict RSI and inclusive price extension.
12. Executor intent derives 2R and preserves analyst-owned stop without sizing.
13. Future or stale bars are excluded.
