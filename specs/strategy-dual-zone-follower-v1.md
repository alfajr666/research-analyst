# Dual-Zone Follower v1

## Status

Build specification. Adopt the supplied strategy's intent, parameters, and
goal, but not Pine-specific execution behavior.

## Goal and Scope

Evaluate every configured static-universe symbol on completed 5-minute candles.
Emit a long intent when either EMA proximity channel qualifies in a bullish
EMA26-over-EMA99 regime. Route qualifying events only to `bybit/fundamo`.

```text
strategy_id: dual-zone-follower-v1
plugin_version: v1
execution_timeframe: 5m
direction: long only
universe: static symbols only
```

This is not a compact strategy and must not be added to
`COMPACT_STRATEGY_IDS`; compact strategies use a restricted asset set and are
forced to `bybit/hyro`.

## Parameters

| Parameter | Default | Meaning |
| --- | ---: | --- |
| `exit_ema_length` | 7 | Exit/target EMA |
| `anchor_ema_length` | 26 | Channel A anchor |
| `trend_ema_length` | 99 | Bullish regime and Channel B anchor |
| `channel_a_entry_distance_pct` | 1.0 | Maximum close distance above EMA26 |
| `channel_a_target_distance_pct` | 3.0 | Target distance above EMA7 |
| `channel_a_stop_distance_pct` | 1.0 | Stop distance below EMA26 |
| `channel_b_entry_distance_pct` | 1.5 | Maximum close distance above EMA99 |
| `channel_b_target_distance_pct` | 5.0 | Target distance above EMA7 |
| `channel_b_stop_distance_pct` | 1.0 | Stop distance below EMA99 |

## Signal Rules

Use the latest completed 5m candle:

```text
ema7  = EMA(close, 7)
ema26 = EMA(close, 26)
ema99 = EMA(close, 99)

is_bullish = ema26 > ema99
pct_above_26 = (close - ema26) / ema26 * 100
pct_above_99 = (close - ema99) / ema99 * 100
```

Channel A qualifies when `is_bullish`, `close > ema99`, `close > ema26`, and
`pct_above_26 < 1.0`. Channel B qualifies when `is_bullish`, `close > ema99`,
and `pct_above_99 < 1.5`. If both qualify, Channel A wins. No short events are
emitted.

## Intent Values

Channel A emits:

```text
direction: long
entry: completed 5m close
stop: ema26 * (1 - 1.0 / 100)
target: ema7 * (1 + 3.0 / 100)
```

Channel B emits:

```text
direction: long
entry: completed 5m close
stop: ema99 * (1 - 1.0 / 100)
target: ema7 * (1 + 5.0 / 100)
```

The analyst owns entry, stop, and target intent values. It does **not** own or
emit `order_type`; order type is selected and enforced by the executor. The
intent has one target and uses fixed full-close semantics.

## Pipeline Integration

```text
static universe
  -> completed 5m bars
  -> dual-zone EMA evaluation
  -> alpha event
  -> universal admission/clash gates
  -> bybit/fundamo routing
  -> executor-owned order lifecycle
```

Register the plugin and enable it through `STRATEGY_ENABLED_IDS`. It emits only
through the existing alpha outbox seam and includes strategy ID, plugin version,
input snapshot ID, 5m execution-timeframe metadata, entry condition,
invalidation price, exactly one target, source evidence IDs where available,
and `confidence_status = "uncalibrated"`.

Static-universe iteration must use the configured static symbol source, not the
compact asset set, rotation feed, or discovery watchlist.

## Universal Gates

Apply the same gates as every strategy without bypass:

- completed and fresh 5m candle
- sufficient EMA warmup and finite positive values
- long geometry: `stop < entry < target`
- global minimum reward/risk
- global stop-distance bounds
- symbol and market eligibility
- clash resolution
- duplicate and active-event suppression
- executor position, sizing, leverage, and safety gates

Invalid or stale candidates produce structured non-emission reasons.

## Routing

The strategy route is fixed to:

```text
exchange_id: bybit
account_id: fundamo
```

It must not inherit the global `hyro` default or compact-strategy forced route.
The executor remains authoritative for whether the profile is configured and
operational.

## Executor Boundary

The analyst emits no Pine-specific position state, tick monitor, bracket order,
or order-type instruction. The executor owns order type, submission, fills,
position uniqueness, sizing, leverage, protective stop/TP placement,
reconciliation, and emergency close behavior.

## Required Verification

1. EMA fixtures verify values, thresholds, and Channel A precedence.
2. Static-universe tests prove every configured static asset is evaluated.
3. Cutoff tests exclude the in-progress 5m bar.
4. Universal R:R and stop-distance gates reject invalid candidates.
5. Routing tests prove only `bybit/fundamo` is selected.
6. Analyst intent tests prove `order_type` is absent.
7. Executor contract tests prove order type is executor-controlled.
8. Dry-run tests cover event, delivery, receipt, and duplicate handling.
