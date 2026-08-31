# EMA Stack ADX StochRSI v1

## Status

Implementation specification. Bidirectional strategy.

## Cadence and data

- Evaluation and execution cutoff: completed **5m** candle.
- Direction filter: previous completed **15m** EMA values.
- Strength filter: previous completed **1H** ADX.
- Trigger and risk calculations: completed 5m bars.
- All higher-timeframe requests must be point-in-time and non-repainting.

## Parameters

| Parameter | Default |
|---|---:|
| Trend timeframe | 15m |
| ADX timeframe | 1h |
| Execution timeframe | 5m |
| Maximum EMA20/EMA200 spread | 1.0% |
| DI length | 14 |
| ADX smoothing | 14 |
| Minimum ADX | 20.0 |
| Use ADX filter | true |
| RSI length | 14 |
| StochRSI length | 14 |
| K smoothing | 3 |
| D smoothing | 3 |
| Oversold | 20 |
| Overbought | 80 |
| ATR length | 14 |
| Stop ATR multiplier | 1.5 |
| Target | 2.0R |

## Trend regime

Long requires the confirmed 15m stack:

`EMA20 > EMA50 > EMA100 > EMA200`

Short requires the inverse stack. Both directions require absolute EMA20 versus
EMA200 spread below 1.0%. ADX is required to be at least 20 when enabled; ADX
measures strength and does not determine direction.

## StochRSI trigger

Calculate RSI14, then the raw StochRSI over 14 RSI values, then SMA3 K and SMA3
D. On the completed 5m signal candle:

- Long: K <= 20, D <= 20, and K crosses above D.
- Short: K >= 80, D >= 80, and K crosses below D.

Require the 5m bar to be confirmed and require the symbol to be flat for this
strategy before emitting.

## Locked risk geometry

Long:

- Stop = 5m EMA200 - 1.5 * 5m ATR14.
- Target = entry + 2 * (entry - stop).

Short:

- Stop = 5m EMA200 + 1.5 * 5m ATR14.
- Target = entry - 2 * (stop - entry).

Reject only mathematically invalid local geometry such as non-positive prices or
non-positive risk. Global admission remains responsible for all policy gates.

## Event metadata

Include confirmed 15m EMA stack, spread percentage, confirmed 1H ADX, RSI,
StochRSI raw/K/D, 5m EMA200, ATR, stop, target, and source/native symbol. Use
`setup_class=ema_stack_adx_stochrsi` and directional phase names.
