# Binance OI Rotation Scanner

## Problem Statement

The current research discovery system uses CoinAnalyze/Coinalyze data to identify
volume and open-interest participation, while each downstream trading bot runs its
own venue-local discovery. There is no low-cost, complete, Binance-native feed for
detecting an asset whose one-hour open interest and volume are unusually active.

Ranking contracts only by one-hour OI percentage change is not reliable: it
over-ranks small contracts and does not distinguish normal volatility from an
asset-specific OI spike. Adding an unbounded external candidate list to downstream
bots would also increase their ticker, OHLCV, backfill, memory, and API-rate-limit
load.

The user needs a strategy-neutral rotation discovery feed. It must identify
economically meaningful Binance USDT-linear perpetual OI spikes, preserve a
point-in-time research record, complement rather than replace local bot discovery,
and remain safe for constrained consumer API budgets.

## Solution

Add a Binance USD-M OI rotation scanner that the Alpha Producer orchestrator runs
once per completed hourly interval alongside the existing CoinAnalyze-specific
scanner. The scanner uses Binance public futures data to discover eligible USDT
linear perpetual contracts, calculate OI and volume activity features, rank
qualifying candidates, persist complete point-in-time research observations, and
publish a versioned atomic candidate feed.

The scanner is a discovery input, not a trade signal. It does not infer trade
direction, select an execution venue, place orders, or replace each bot's native
discovery. Consumer bots independently resolve a candidate against live venue
markets, deduplicate it with their own candidates, enforce their own caps, and
perform normal strategy and risk evaluation.

## User Stories

1. As a research analyst, I want to scan Binance USDT perpetuals at every completed hourly boundary, so that I can observe fresh derivatives activity without a paid data provider.
2. As a research analyst, I want the scan to start from the full Binance liquid perpetual universe, so that candidates are not limited to a manually curated watchlist.
3. As a research analyst, I want to apply a rolling 24-hour notional-liquidity floor, so that thin contracts cannot dominate rotation ranks.
4. As a research analyst, I want each candidate to include one-hour OI percentage change, so that I can compare relative positioning activity.
5. As a research analyst, I want each candidate to include one-hour OI notional change in USD, so that economically insignificant moves are filtered out.
6. As a research analyst, I want each candidate to include an asset-relative trailing OI-spike percentile, so that normal behavior for one asset is not misclassified as exceptional behavior for another.
7. As a research analyst, I want each candidate to include a one-hour volume anomaly measure, so that I can distinguish participation from isolated OI movement.
8. As a research analyst, I want all scan observations, including rejected contracts, persisted with their observation time, so that later research is free from survivorship bias.
9. As a research analyst, I want a qualifying OI event deduplicated within a completed hourly bucket, so that retries and repeated scans do not create duplicate research events.
10. As a research analyst, I want a later qualifying OI event for the same asset to remain independently observable, so that separate rotation episodes can be measured.
11. As a research analyst, I want a qualified asset retained in the native research watchlist for 24-48 hours after its last qualifying event, so that its post-event behavior can be studied with warmed 15-minute context.
12. As a research analyst, I want an asset already held in another native discovery pool to receive an additional rotation rationale rather than a duplicate data pipeline, so that backfills and local data are not duplicated.
13. As a downstream bot operator, I want an atomic, versioned rotation feed with explicit expiry, so that a partial write or stale feed never affects a trading cycle.
14. As a downstream bot operator, I want to resolve canonical candidate identity against my venue's live market metadata, so that exchange-specific aliases, scaled contracts, inactive markets, and quote/settlement differences are handled locally.
15. As a downstream bot operator, I want feed candidates deduplicated with pinned assets, local discovery, and open positions, so that an instrument is evaluated once per cycle.
16. As a downstream bot operator, I want a bounded Binance OI candidate quota, so that the feed complements local discovery without increasing the final evaluation-set size.
17. As a downstream bot operator, I want external candidates to expire after two completed hourly bars, so that historical OI activity does not consume current API and memory budget.
18. As a downstream bot operator, I want to admit only a limited number of cold external candidates per cycle, so that historical OHLCV warmup cannot produce an API burst.
19. As a Bybit testnet operator, I want to validate feed reading, symbol resolution, deduplication, expiration, and quota behavior before production consumers use the feed, so that integration faults do not affect live trading.
20. As a system operator, I want scanner and consumer metrics to explain acceptance, rejection, expiry, and admission decisions, so that API load and feed value can be monitored.

## Implementation Decisions

- The scanner uses Binance USD-M public market-data endpoints as its only discovery source. CoinAnalyze/Coinalyze ingestion and its current scanner remain unchanged and run alongside this scanner.
- The scanner operates only on completed one-hour intervals. It must not rank an in-progress OI or OHLCV bar.
- The eligibility universe is active Binance USDT-settled linear perpetual contracts above the configured rolling 24-hour notional-volume floor. The universe snapshot is stored for every scan.
- The scanner calculates, at minimum, current OI notional, one-hour OI percentage change, one-hour OI notional delta, one-hour price change, one-hour volume, and an asset-relative volume anomaly measure.
- An OI spike is based on both relative and absolute activity. A candidate must pass configured liquidity, positive OI-notional-delta, OI-percentile, and volume-anomaly thresholds before ranking. Exact thresholds are configuration, not embedded strategy logic.
- The scanner ranks qualifying candidates by OI-spike percentile, OI-notional increase, and volume anomaly. It does not produce a long, short, entry, target, or confidence value.
- The scanner preserves an immutable broad snapshot for all eligible contracts and an immutable event ledger for qualifying rotation events. The event identity includes source, canonical asset identity, completed interval timestamp, and scanner version.
- Retention is intentionally distinct by object: raw rotation observations follow the existing long-term research retention policy; native rotation watchlist membership remains for 24-48 hours after the last qualifying event; downstream feed candidates expire after two completed hourly bars.
- A qualifying asset entering the native rotation watchlist queues the existing deep-history warmup behavior once. If it is already active or warming in an existing discovery pool, it receives a rotation source/rationale annotation instead of a duplicate membership or backfill job.
- The scanner publishes one versioned feed atomically. The document contains generation time, source, completed interval time, expiry time, scanner version, and ranked candidate records. Candidate records contain canonical base asset, source symbol, quote, contract type, rank, eligibility evidence, and the OI/volume metrics used for discovery.
- Candidate identity in the published feed is venue-neutral. It is not a CCXT symbol and it is not a Bybit, MEXC, Propr, or testnet instrument identifier.
- Consumer integrations occur at the existing watchlist/discovery boundary, before the venue's existing discovery filter and market-context build. Reading the local feed performs no remote API call.
- Every consumer resolves a candidate only against its loaded live market metadata. It must reject unavailable, inactive, wrong-settlement, non-linear, or venue-blocked instruments. MEXC retains its API-allowed contract validation; Propr retains its approved-universe and Hyperliquid/Propr namespace rules.
- Consumers deduplicate after venue resolution. The resolved venue instrument is merged with pinned symbols, local discovery candidates, and open or pending positions. A merged instrument is evaluated once, while diagnostics retain all discovery-source reasons.
- External candidates do not use the local top-mover grace-retention state. They are derived from the fresh feed each cycle and are removed when absent or expired.
- Each consumer preserves a fixed final rotating evaluation budget. Initial external quotas are: Bybit testnet 8, Bybit production 6, MEXC 6, and Propr 4. Local discovery receives the remaining rotating budget. Pinned symbols and open positions remain independently protected.
- Each consumer has a bounded cold-admission rate. Initial limits are two new external candidates per cycle for Bybit and MEXC, and one for Propr. Valid candidates beyond the admission rate wait in feed-rank order.
- Consumer metrics record feed freshness, records received, venue resolution failures, deduplications, external quota admissions, cold-admission deferrals, and final evaluated-symbol count.

## Testing Decisions

- The highest test seam is the scanner's completed-hour discovery output: given a Binance market snapshot and historical OI/OHLCV inputs, assert the persisted observation/event outcome and the published feed content. Tests must not assert internal request order, helper calls, or implementation-specific storage operations.
- Scanner tests cover completed-bar alignment, liquidity exclusion, absolute OI-notional gating, percentile/volume qualification, deterministic ranking, hourly event deduplication, separate later events, stale/missing data rejection, and atomic-feed validity metadata.
- Native research lifecycle tests cover one-time deep-warmup enqueue, longer watchlist retention after a rotation event, expiry after the configured absence window, and no duplicate pipeline when an asset overlaps another discovery pool.
- Consumer integration tests use mocked feed documents and mocked venue markets at the existing watchlist boundary. They verify fresh-feed acceptance, stale-feed rejection, canonical-to-venue resolution, local-market rejection, source-aware deduplication, fixed external quota, and bounded cold admission.
- Consumer tests must verify observable final watchlists and evaluation budgets, not private cache contents or source-file implementation details.
- Bybit testnet is the first end-to-end integration target. Its tests and cycle reports establish the feed contract before enabling the production Bybit, MEXC, and Propr integrations.
- Existing discovery-pool, scanner, top-mover pairlist, and symbol-adapter tests are prior art. New tests should follow their data-driven, mock-exchange style.

## Out of Scope

- Aggregated multi-exchange OI from Coinglass, Coinalyze, or another paid provider.
- OI-based long/short classification, trade entries, sizing, leverage, targets, stops, or order placement.
- Replacing local top-mover, static, regime, or strategy discovery in any bot.
- Direct scanner-to-exchange dispatch, shared execution state, or cross-bot order coordination.
- Supporting non-Binance source contracts, Binance coin-margined contracts, spot markets, or non-perpetual instruments in the initial scanner.
- Changing Propr's approved tradeable-universe policy or treating Bybit testnet liquidity as production evidence.
- Historical claims of predictive alpha before point-in-time outcomes have been collected and evaluated.

## Further Notes

The scanner is a medium-frequency selection system. It should be scheduled once
after each completed hourly interval, with retry behavior that preserves the event
deduplication identity. It is not a sub-minute feed.

The scanner's data-quality and performance contract is more important than the
initial threshold values. Thresholds and quotas should be configurable and revised
after the native event ledger has accumulated sufficient point-in-time outcomes.

The repository's existing data ownership remains intact: the orchestrator is the
only writer of external market-data research state, while downstream evaluators and
consumer bots use the published artifact and their own venue-local state.

### Amendment — retention + NT rotating budget (2026-08-20)

Soft feed/membership TTLs in this document remain. **Hard table prune**, optional
**static membership skip**, and the NT rule that **`ROTATING_LIMIT` is novel-only
after static-subtract** are specified in:

- [`binance-oi-rotation-retention.md`](./binance-oi-rotation-retention.md) (this repo)
- [`ADR-013`](../../nautilus-trading-os/specs/ADR-013-oi-rotating-static-subtract-and-ttl.md) (nautilus-trading-os)
- [`IMPL-013`](../../nautilus-trading-os/specs/IMPL-013-oi-rotating-static-subtract-and-ttl.md) (patch order)

Live topology: PM2 `binance-oi-rotation-scanner` owns scan + prune; NT `data-oi`
consumes only. Do not embed the scanner in NT.
