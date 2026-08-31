# Enhanced Dual-Zone Follower v2

## Status

Implementation specification. Replaces the retired dual-zone v1 long and short
plugins.

## Cadence and data

- Evaluation cutoff: completed **5m** candle.
- Local EMAs: 5m EMA7, EMA26, EMA99.
- Higher-timeframe filter: previous completed **1H** DMI/ADX values.
- The 1H values must be constant throughout the following 1H period and must
  not use the forming 1H candle.
- Minimum warmup is sufficient history for EMA99 plus DMI/ADX warmup; insufficient
  history means no candidate, not an error.

## Parameters

| Parameter | Default |
|---|---:|
| Exit EMA length | 7 |
| Anchor EMA length | 26 |
| Trend EMA length | 99 |
| ADX timeframe | 1h |
| DI length | 14 |
| ADX smoothing | 14 |
| Minimum ADX | 22.0 |
| Require directional DI | true |
| Channel A entry distance | 1.0% |
| Channel A stop distance | 1.0% |
| Channel A target distance from EMA7 | 3.0% |
| Channel B entry distance | 1.5% |
| Channel B stop distance | 1.0% |
| Channel B target distance from EMA7 | 5.0% |

All parameters need strategy-specific configuration prefixes. Long and short
defaults are mirrored but independently configurable.

## Long thesis

The long regime is valid when:

- EMA26 is above EMA99.
- Close is above EMA26 and EMA99.
- Confirmed 1H ADX is at least 22.
- If DI direction is enabled, confirmed +DI is greater than confirmed -DI.

Channel A is selected first when close is no more than 1.0% above EMA26.
Channel B is selected otherwise when close is no more than 1.5% above EMA99.
Channel B retains the long regime and requires close above EMA99; it does not
require the Channel A EMA26 proximity condition.

For Channel A:

- Stop = EMA26 * (1 - 1.0%).
- Target = EMA7 * (1 + 3.0%).

For Channel B:

- Stop = EMA99 * (1 - 1.0%).
- Target = EMA7 * (1 + 5.0%).

## Short thesis

Mirror the long thesis:

- EMA26 is below EMA99.
- Close is below EMA26 and EMA99.
- Confirmed 1H ADX is at least 22.
- If DI direction is enabled, confirmed -DI is greater than confirmed +DI.

Channel A has precedence:

- Entry distance is no more than 1.0% below EMA26.
- Stop = EMA26 * (1 + 1.0%).
- Target = EMA7 * (1 - 3.0%).

Channel B applies otherwise when close is no more than 1.5% below EMA99:

- Stop = EMA99 * (1 + 1.0%).
- Target = EMA7 * (1 - 5.0%).

## Event metadata

Include confirmed ADX, +DI, -DI, EMA7, EMA26, EMA99, channel, entry-distance
percentages, cutoff, and source/native symbol in the feature snapshot. Use
`setup_class=dual_zone_follower` for long and
`dual_zone_short_follower` for short, with `phase=channel_a|channel_b`.
