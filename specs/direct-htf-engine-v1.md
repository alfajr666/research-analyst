# Direct Higher-Timeframe Engine v1

## Status

Implementation specification. This supersedes `hybrid-htf-engine-v1.md` and
removes the direct-history-to-canonical-tail handoff from strategy evaluation.

## Decision

Strategy evaluation uses two independent, point-in-time data paths:

```text
regime.sqlite3: direct Bybit REST 1h/4h bars
        |                         |
        +--> regime scoring       +--> strategy HTF setup

market.sqlite3: committed completed 5m bars --> execution-time evaluation
```

The regime worker remains the sole writer of `regime.sqlite3`. The websocket
gateway remains the sole writer of `market.sqlite3`. The orchestrator and
strategy engine open both databases read-only. No timeframe is assembled by
merging direct and canonical bars.

## Scope

- Direct native history applies to strategy `1h` and `4h` frames.
- Completed `5m` observations remain the authoritative execution/evaluation
  input.
- `15m` remains a derived auxiliary frame from committed `5m` observations for
  any legacy or non-production plugin that explicitly requires it.
- Regime scoring and strategy HTF setup use the same direct-history contract,
  but remain separate consumers and computations.
- Strategy plugins remain source-blind. They continue calling
  `load_bars_for_interval` and do not receive database connections or source
  selection flags.
- The hybrid merge, handoff, parity gate, canonical HTF tail, and hybrid
  sequential cache are removed rather than retained as a runtime mode.

## Point-in-Time Cutoff

For an evaluation cutoff `T`, a direct HTF bar is eligible only when its
exclusive `source_end <= T`. The latest eligible bar is therefore the latest
completed native interval boundary at or before `T`:

```text
evaluation at 00:04 -> latest 1h/4h bar ending 00:00
evaluation at 00:05 -> latest 1h/4h bar ending 00:00
evaluation at 04:05 -> latest 4h bar ending 04:00
```

The engine never reads a forming bar, a future bar, or a bar after the exact
evaluation cutoff. Replay and delayed evaluation use the original cutoff, not
wall-clock time.

## Direct History Contract

The regime worker fetches and validates native Bybit linear-perpetual candles
for every subscribed asset independently for `1h` and `4h`.

Each accepted row must have:

- canonical asset identity;
- native Bybit source and the interval-specific bar version;
- finite positive OHLC values with valid high/low geometry;
- finite non-negative volume when present;
- a unique interval boundary per asset and bar version;
- `source_end` equal to the native interval end;
- no gap in the requested retained window.

The loader validates the returned frame again. Invalid rows, unresolved
duplicates, gaps, insufficient history, missing database tables, and cutoff
mismatches make that asset/timeframe unavailable. The engine does not
interpolate, resample, substitute `5m`, or silently use a stale frame.

## Readiness And Retention

The direct cache must cover the strongest of the regime and strategy contracts.
The initial strategy target is 240 completed bars for both `1h` and `4h`, with
fetch margin. The regime ADX minimum of 57 completed bars remains mandatory.

Retention is calculated from named direct-HTF settings, not from hybrid flags:

- `DIRECT_HTF_1H_SEED_BARS`, default `240`;
- `DIRECT_HTF_4H_SEED_BARS`, default `240`;
- `DIRECT_HTF_1H_RETAIN_DAYS`, at least 14 days;
- `DIRECT_HTF_4H_RETAIN_DAYS`, at least 45 days;
- `DIRECT_HTF_1H_FETCH_DAYS`, at least retention plus one day;
- `DIRECT_HTF_4H_FETCH_DAYS`, at least retention plus one day.

Retention cleanup runs only on the regime worker's writer connection. It must
preserve the complete configured window through the latest completed cutoff.
Job rows are retained while pending, running, retryable, or leased; terminal
job history may be aged according to the existing regime database policy.
Cleanup is batched, yield-aware, and followed by a passive WAL checkpoint.
The unrelated Binance OI database is never opened or pruned.

## Strategy Loading

Within an invocation-scoped direct HTF context:

1. `1h` and `4h` requests load only the corresponding regime-owned direct table.
2. The loader requests the configured seed target, which is sized to cover the
   longest enabled HTF indicator; requests are bounded by the exact evaluation
   cutoff.
3. The frame is ordered oldest-to-newest and preserves direct bar IDs,
   versions, source, and provenance columns.
4. Indicator state is computed once over that direct frame and cached only for
   the current invocation.
5. `5m` requests use the shared market read-only context and committed market
   observations at the same evaluation cutoff.
6. `15m` requests, if needed, resample only committed `5m` observations; they
   never participate in `1h`/`4h` setup.

Outside an invocation-scoped context, direct HTF loading requires an explicit
read-only regime connection. It must not silently fall back to market-derived
`1h`/`4h` bars for production strategy evaluation.

## Provenance

Candidate and feature provenance includes, for every requested HTF:

- `data_contract_version = direct-htf-v1`;
- evaluation cutoff;
- interval and direct source mode;
- direct bar IDs and bar versions;
- direct source and venue;
- availability and exact failure reason when unavailable.

There is no handoff timestamp, canonical HTF observation list, parity result,
or `canonical_only` mode in the new contract. Execution-bar provenance remains
the market observation provenance already carried by the `5m` path.

## Failure And Delivery Rules

- A missing `1h` frame blocks only strategies requiring `1h` for that asset.
- A missing `4h` frame blocks only strategies requiring `4h` for that asset.
- A missing `5m` execution window blocks evaluation for that asset.
- Regime readiness remains independent from candidate admission, structural
  context, scoring, and publisher delivery.
- No direct intent, alpha event, or publisher handoff may bypass admission
  proof because the HTF source changed.

## Cleanup And Operations

Startup and recurring maintenance must:

- use the existing owner-specific writer connections;
- refresh direct-history jobs before evaluating readiness;
- prune only rows outside the configured safe retention window;
- preserve active, pending, running, retryable, and leased work;
- avoid `VACUUM` from a second writer;
- report coverage, latest cutoff, cleanup counts, and failure reasons.

Managed services remain the only production process entrypoint. After code
deployment, restart only services importing changed modules and verify fresh
cutoff logs, process health, direct-history readiness, pipeline completion, and
publisher/PM behavior.

## Migration And Validation

The migration removes hybrid configuration and runtime branches. Existing
direct regime tables and backfill jobs are reused; no database migration is
needed. Historical hybrid provenance remains readable as audit data but is not
produced by new evaluations.

Tests must cover:

- exact cutoff exclusion and completed-boundary selection;
- native `1h` and `4h` loading with stable IDs and versions;
- direct gaps, duplicates, malformed bars, and insufficient history;
- no market fallback when direct history is absent;
- independent `1h`, `4h`, and `5m` readiness failures;
- direct indicator continuity within one frame;
- plugin failure isolation and candidate provenance;
- retention margins and cleanup safety for active/retryable jobs;
- regime worker and gateway behavior unchanged;
- replay at an historical cutoff without lookahead.
