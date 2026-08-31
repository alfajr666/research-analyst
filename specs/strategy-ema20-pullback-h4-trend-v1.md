# EMA20 Pullback H4 Trend v1

## Status

Implementation specification. Bidirectional strategy.

## Cadence and data

- Dedicated evaluation cadence: completed **1H** cutoff.
- Local indicators use completed 1H bars: EMA20 and ATR14.
- Trend indicators use completed 4H bars: close, EMA50, EMA200.
- The 4H values must be the latest completed 4H values available at the 1H
  cutoff. No lookahead or forming 4H bar is allowed.
- The latest 1H candle is the signal candle and must itself be completed.

## Parameters

| Parameter | Default |
|---|---:|
| EMA20 length, local 1H | 20 |
| EMA50 length, 4H | 50 |
| EMA200 length, 4H | 200 |
| ATR length, 1H | 14 |
| Swing lookback | 10 |
| Risk/reward target | 2.0R |
| Use session filter | true |
| Session | 15:00-23:00 exchange timezone |
| Use trailing stop | false |
| Trailing ATR multiplier | 1.0 |

Session handling must use the venue/exchange timezone contract, not the analyst
machine timezone. If the platform cannot represent the exchange timezone
unambiguously, the strategy must fail closed for session-gated signals.

## Trend regime

Long regime:

- Completed 4H close > completed 4H EMA200.
- Completed 4H EMA50 > completed 4H EMA200.

Short regime is the exact inverse.

## Signal candle

Long requires:

- Low <= local EMA20.
- Close > local EMA20.
- Current candle bullish: close > open.
- Previous candle bearish: previous close < previous open.
- Current close >= previous open.
- Session filter passes when enabled.

Short requires the mirrored bearish engulfing pattern:

- High >= local EMA20.
- Close < local EMA20.
- Current candle bearish and previous candle bullish.
- Current close <= previous open.
- Session filter passes when enabled.

## Locked risk geometry

Long:

- Stop = lowest low over the 10-bar local lookback minus one local ATR.
- Target = entry + 2 * (entry - stop).

Short:

- Stop = highest high over the 10-bar local lookback plus one local ATR.
- Target = entry - 2 * (stop - entry).

The entry price is the completed signal close. The stop and target are locked at
signal creation. Optional trailing behavior is an execution/intent capability
only if the executor contract supports it; it must not replace the fixed target
when trailing is disabled.

## Event metadata

Include 1H EMA20, 4H close/EMA50/EMA200, ATR, swing extreme, engulfing flags,
session result, stop, target, and timeframe provenance. Use explicit long/short
phase names and `setup_class=ema20_pullback_h4_trend`.
