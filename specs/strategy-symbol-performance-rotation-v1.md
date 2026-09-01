# Upstream Symbol Universe Rotation v1

## Status

Accepted design specification. This document is authoritative for performance
rotation and supersedes conflicting strategy-universe wording elsewhere.

## Decision Summary

Strategies are symbol-dumb. They evaluate every symbol delivered to them and
contain no symbol, account, rotation, or subscription logic.

The upstream market-data layer owns the subscription universe. Four permanent
symbols are always subscribed and never rotated away: `BTC`, `ETH`, `PAXG`, and
`QQQUSDT` (the QQQ stock-index contract). A separate rotation plugin publishes
configurable numbers of the top gainers and top losers from the remaining
valid Bybit linear USDT ticker universe.

Symbol-account-strategy admission is a deterministic hard gate after strategy
output and before alpha selection or intent publication. It is a safety
boundary, not a performance optimization.

```text
24h performance for valid Bybit linear USDT tickers
                  |
                  v
       rotation universe plugin
                  |
                  v
       durable versioned feed
                  |
                  v
       gateway subscription supervisor
                  |
                  v
    every active strategy sees every feed symbol
                  |
                  v
     symbol-account-strategy hard admission gate
                  |
                  v
       normal admission and intent delivery
```

## Terminology

- **Approved universe**: the 92 canonical bases in
  `symbols/static_universe.json`.
- **Performance source**: the lightweight, point-in-time source used to obtain
  24-hour performance for every approved symbol.
- **Rotation plugin**: the module that ranks the approved universe and publishes
  a versioned subscription feed. It is not a strategy plugin.
- **Rotation feed**: the durable snapshot consumed by the gateway subscription
  supervisor.
- **Subscription universe**: the symbols currently subscribed to by the
  gateway and delivered to evaluators.
- **Symbol-account-strategy policy**: the allowlist mapping a symbol, account,
  and strategy to permission to create an intent.
- **Hard gate**: a rejection that prevents a candidate from reaching scoring,
  alpha publication, or executor intent delivery.

## Invariants

1. The performance source is the valid Bybit linear USDT ticker universe; the
   approved universe remains the symbol-account-strategy policy universe.
2. Strategies never call the rotation plugin or inspect rotation configuration.
3. Strategies evaluate every symbol delivered by their evaluator input.
4. The gateway never subscribes to an empty universe because of a feed failure.
5. A rotation decision is point-in-time and cannot use future data.
6. Symbol-account-strategy rejection is durable and auditable.
7. Executor sizing, protection, routing receipts, and execution state remain
   executor-owned.
8. Binance OI rotation is a separate feature and is not an input to this feed.

## Strategy Delivery

The evaluator passes the same subscription universe to every active strategy
applicable to the cutoff. No strategy may contain an asset allowlist or call
`config.load_static_symbols()` to decide whether to evaluate a symbol.

The compact strategies remain restricted for trading through admission:

```text
failed-break-v3                    -> hyro    -> BTC, ETH, PAXG, QQQUSDT
bb-rsi-meanrev-v1                 -> hyro    -> BTC, ETH, PAXG, QQQUSDT
williams-fractal-scalp-v1         -> hyro    -> BTC, ETH, PAXG, QQQUSDT
ema9-continuation-stochrsi-v1     -> hyro    -> BTC, ETH, PAXG, QQQUSDT
dual-zone-follower-v2             -> fundamo -> approved universe
dual-zone-short-follower-v2      -> fundamo -> approved universe
ema20-pullback-h4-trend-v1        -> fundamo -> approved universe
ema-stack-15m-adx-stochrsi-5m-v1  -> fundamo -> approved universe
```

All strategies calculate candidates for every symbol in the subscription
universe. The hard gate rejects compact candidates for non-compact symbols
before scoring and intent publication. When rotation is disabled, the same gate
remains in force across all 92 symbols.

## Configuration

```text
SYMBOL_ROTATION_ENABLED=true
SYMBOL_ROTATION_REFRESH_HOURS=4
SYMBOL_ROTATION_LOOKBACK_HOURS=24
SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT=30
```

`SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT` is the number of rotating slots, not the
total subscription count. The four permanent symbols are added to it. The
rotating count must be a positive even number. The split is always equal:

```text
30 -> 15 top gainers + 15 top losers, plus 4 permanent = 34 total
40 -> 20 top gainers + 20 top losers, plus 4 permanent = 44 total
60 -> 30 top gainers + 30 top losers, plus 4 permanent = 64 total
```

The permanent symbols are removed from the ranking pool before selecting the
rotating sides, so they cannot consume rotating slots or be removed by ranking.
When `SYMBOL_ROTATION_ENABLED=false`, the subscription universe is all 92
approved symbols. This setting controls the upstream subscription universe and
does not alter strategy code or symbol-account-strategy policy.

This feature must not reuse or infer from `ROTATION_FEED_ENABLED`, which
controls the separate Binance OI feed.

## Performance Source

The rotation plugin must obtain performance for all approved symbols without
depending on heavy candle subscriptions for the currently selected symbols.
Acceptable adapters include:

- a lightweight exchange 24-hour ticker snapshot for all valid linear USDT contracts;
- a low-frequency bar source retained for the approved policy universe;
- an external authoritative performance feed.

The preferred adapter is a four-hour ticker snapshot with an explicit
`as_of` timestamp. The feed must not become unable to rank unsubscribed symbols
after the first rotation.

The performance source must provide for every valid linear USDT ticker:

- canonical asset;
- performance interval;
- start and end/as-of timestamps;
- positive finite reference and current prices, or a validated percentage;
- source identity and retrieval timestamp.

## Rotation Algorithm

1. At a UTC refresh boundary, load the latest complete Bybit linear ticker snapshot.
2. Normalize and reserve the four permanent symbols.
3. Read the newest performance snapshot whose `as_of` is not after the
   boundary, except for a fresh startup bootstrap of the currently open UTC window.
4. Require valid 24-hour performance for each qualified rotating asset.
5. Reject unknown assets, malformed values, non-positive prices, future data,
   and stale snapshots according to configured freshness limits.
6. Calculate simple return when the source provides prices:

   ```text
   return = current_price / reference_price - 1
   ```

7. Rank gainers descending and losers ascending.
8. Break ties by canonical asset name.
9. Select `SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT / 2` from each side.
10. Union the rotating selection with the permanent symbols and publish a feed
    snapshot.

An asset can only occur once in the feed. If fewer qualified assets exist than
the requested count, publish all qualified top-side results without inventing
unqualified symbols and record the shortfall. If no valid performance snapshot
exists, retain the last valid feed until expiry; after expiry, subscribe to the
permanent symbols only and record the fallback. Fresh OPEN position assets are
added independently by the gateway.

## Feed Contract

Each feed snapshot is immutable and contains:

```json
{
  "schema_version": 1,
  "feed_id": "performance-2026-08-31T04:00:00Z-v42",
  "algorithm_version": "performance-24h-v1",
  "generated_at": "2026-08-31T04:00:05Z",
  "valid_from": "2026-08-31T04:00:00Z",
  "valid_until": "2026-08-31T08:00:00Z",
  "source_as_of": "2026-08-31T03:59:00Z",
  "permanent_symbols": ["BTC", "ETH", "PAXG", "QQQUSDT"],
  "rotating_symbol_count": 30,
  "symbol_count": 34,
  "gainers": [{"asset": "AAA", "return": 0.12}],
  "losers": [{"asset": "BBB", "return": -0.11}],
  "symbols": ["AAA", "BBB"],
  "status": "ready",
  "fallback_reason": null
}
```

The feed is written atomically. Readers either see the previous complete
snapshot or the next complete snapshot, never a partial file. A feed version
change is the gateway's reconciliation trigger.

## Gateway Subscription Supervisor

The current startup-only `select_universe()` path must be replaced or extended
with a supervisor that:

1. Loads a valid feed before opening heavy WebSocket subscriptions.
2. Uses all 92 when rotation is disabled.
3. Reconciles subscriptions at each feed version change.
4. Cancels streams for removed symbols and starts streams for added symbols.
5. Does not block WebSocket reads while computing or loading a feed.
6. Coalesces repeated feed versions and reconnects idempotently.
7. Keeps the previous valid feed during transient refresh failures.
8. Falls back to the permanent symbols after feed expiry.
9. Logs and exposes subscribed count, feed ID, version, and fallback state.

The market gateway remains the sole writer of `market.sqlite3`. The rotation
plugin may read market data, but it must not introduce a second market database
writer.

## Symbol-Account Admission

The policy is evaluated from the strategy ID, canonical asset, and resolved
account route. Candidate-supplied account fields cannot override the policy.

The gate must run before `trade_admission.score()`, clash resolution, and
`write_event()` intent delivery. A failed result includes:

- strategy ID;
- canonical asset;
- resolved account;
- policy version;
- rejection reason.

The same policy is used to derive the executor route. The existing forced
routing code is defense in depth, not the primary admission control.

## Cutoffs and Recovery

- Feed refresh boundaries are fixed UTC boundaries.
- Performance data is bounded by the boundary and never by wall-clock data
  after the cutoff.
- A feed snapshot remains stable for its valid interval.
- Gateway restart reloads the last valid feed before subscribing.
- A missing or expired feed cannot result in zero subscriptions.
- Evaluator cutoff idempotency remains independent of feed version idempotency.

## Observability

Expose or persist:

- feed ID and algorithm version;
- refresh boundary and validity interval;
- approved count;
- qualified count;
- configured rotating slot count;
- gainers and losers selected;
- subscribed count;
- fallback status and reason;
- per-strategy attempted symbol count;
- symbol-account-strategy hard-gate rejection counts.

Evaluation observability must report actual attempted symbols. It must not use a
static `strategy_count * 92` approximation when the feed contains another
number.

## Acceptance Criteria

1. With rotating count 30, a complete performance snapshot produces 15 gainers,
   15 losers, and the four permanent symbols, for 34 subscriptions.
2. With rotating count 40 or 60, the split remains equal and configurable, for
   44 or 64 subscriptions respectively.
3. Permanent BTC, ETH, PAXG, and QQQUSDT are present in every non-empty feed.
4. Rotation is recalculated only at four-hour UTC boundaries.
5. Rotation disabled produces all 92 approved subscription symbols.
6. Every active strategy evaluates every subscribed symbol without local symbol
   filtering.
7. Compact candidates for SOL are rejected for the Hyro account while compact
   candidates for BTC and QQQUSDT are eligible for further admission.
8. Fundamo candidates for any approved subscribed symbol pass the
   symbol-account-strategy gate, subject to normal price and risk gates.
9. Discovery and Binance OI feeds cannot replace the approved performance pool.
10. Missing performance data retains a valid feed or falls back visibly to the
    permanent symbols; it never silently fabricates rankings.
11. A gateway restart and feed replay are idempotent.
12. Existing executor routing, sizing, protection, PM behavior, and intent
    ownership remain unchanged.

## Test Plan

### Rotation plugin unit tests

- Equal top-side split for rotating counts 30, 40, and 60.
- Permanent-symbol inclusion and exclusion from ranking slots.
- Deterministic ties.
- Unknown and duplicate assets.
- Future and stale performance snapshots.
- Invalid and non-positive prices.
- Exact 24-hour coverage.
- Last-valid-feed retention and expired-feed permanent-only fallback.
- Atomic feed serialization and schema validation.

### Subscription supervisor integration tests

- Feed version changes reconcile 92 to 30, 30 to 40, and 40 to 60.
- Rotation disabled reconciles to all 92.
- Repeated feed versions do not duplicate streams.
- Refresh failure retains the previous feed.
- Expiry falls back to the permanent symbols.
- Gateway never publishes an empty subscription set.

### Strategy and admission tests

- A probe strategy receives every symbol in the subscription universe.
- No strategy imports or calls rotation policy.
- Compact Hyro symbols pass only for BTC, ETH, PAXG, and QQQUSDT.
- Non-compact Fundamo symbols pass for approved assets.
- Rejections are recorded before scoring and intent publication.

### End-to-end tests

1. Seed 92 approved symbols and a deterministic all-92 performance snapshot.
2. Run the rotation plugin and atomically publish a 34-symbol feed.
3. Start the gateway subscription supervisor and assert 34 subscriptions,
   including all four permanent symbols.
4. Run a symbol-dumb probe strategy and assert all 34 symbols are evaluated.
5. Emit compact BTC, QQQUSDT, and SOL candidates and assert only the first two
   can proceed through symbol-account-strategy admission.
6. Change the rotating count to 40 and publish the next boundary feed.
7. Assert the gateway reconciles to 44 and the probe sees all 44.
8. Disable rotation and assert reconciliation to all 92.
9. Expire the feed and assert visible permanent-only fallback.
10. Replay the same feed and cutoff and assert no duplicate subscriptions,
    alpha events, or intents.

## Non-Goals

- Do not move strategy execution into the gateway.
- Do not make the gateway or rotation plugin own executor state.
- Do not use a strategy plugin to implement market-universe selection.
- Do not change strategy indicator thresholds or normal risk gates.
- Do not conflate performance rotation with Binance OI rotation.
