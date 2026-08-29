# Dual-Zone Short Follower v1

## Goal

Evaluate all 97 configured static symbols on completed 5m candles and emit
short intents when price is near EMA26 or EMA99 during an EMA26-below-EMA99
bearish regime. Route only to `bybit/fundamo`.

## Contract

```text
strategy_id: dual-zone-short-follower-v1
plugin_version: v1
timeframe: 5m
direction: short only
universe: static_universe.json (count 97)
```

Calculate EMA7, EMA26, and EMA99 on the same completed 5m series. The bearish
filter is `ema26 < ema99`.

Channel A requires `close < ema99`, `close < ema26`, and
`(ema26 - close) / ema26 * 100 < 1.0`. It uses stop
`ema26 * 1.01` and target `ema7 * 0.97`.

Channel B requires `close < ema99` and
`(ema99 - close) / ema99 * 100 < 1.5`. It uses stop
`ema99 * 1.01` and target `ema7 * 0.95`.

Channel A wins if both qualify. Emit one target, `limit_at_ema_context` entry
metadata at the completed close, and no `order_type`; the executor owns order
type and execution lifecycle.

## Universal Pipeline Rules

Apply freshness, warmup, finite-price, short-geometry (`target < entry < stop`),
global R:R, stop-distance, clash, duplicate, and executor risk gates. A target
above the short entry is invalid and must not execute. No Pine-specific bracket
or tick-monitor behavior is carried into the analyst.

Register as a non-compact plugin with required dataset `bars_5m`. Iterate
`config.load_static_symbols()` directly, never the compact asset set or rotated
watchlist. Add the strategy to `STRATEGY_ENABLED_IDS` to activate it.

## Routing and Verification

The route is fixed to `exchange_id=bybit`, `account_id=fundamo`. Required tests
cover bearish calculations, Channel A precedence, all-static-symbol iteration,
5m cutoff exclusion, invalid short geometry, universal gates, routing, absent
`order_type`, and duplicate delivery handling.
