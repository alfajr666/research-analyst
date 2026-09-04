# Regime History Bootstrap v2

## Status

Locked design specification, agreed during the operator discussion on
2026-09-04.

This specification defines the historical data contract for the regime-session
module. It is deliberately separate from the live strategy market-data path.
The regime worker owns direct 1h and 4h history; live strategy data is
unchanged.

## 1. Decision

The regime-session module uses persistent, direct Bybit REST `1h` and `4h`
history caches for regime scoring. It does not wait for live 5m bars to
accumulate higher-timeframe regime warmup requirements.

```text
rotation feed
    |
    +--> regime worker -> Bybit public REST 1h + 4h history
    |                       -> regime.sqlite3 regime_1h_bars + regime_4h_bars
    |                       -> regime score and gate
    |
    +--> ws_gateway -> 1m/5m REST bootstrap
                         -> live 1m/5m WebSocket
                         -> market.sqlite3
```

The regime cache is exclusive to the regime-session module. Existing strategy
features and candidate admission continue to use the canonical market path:
completed 5m observations and local 15m/1h/4h resampling.

## 2. Objectives

- Make a newly rotated asset regime-ready without waiting for live
  higher-timeframe bars to accumulate.
- Keep live WebSocket subscriptions limited to the active rotation feed.
- Avoid a second writer for `data/market.sqlite3`.
- Keep the regime input small, durable, point-in-time, and auditable.
- Preserve the existing 5m-derived higher-timeframe contract for strategies.

## 3. Locked Data Requirements

### 3.1 Direct 4h history

The regime worker fetches at least 15 calendar days of completed Bybit linear
perpetual `4h` candles for every asset that enters the active feed and does not
already have sufficient cache coverage.

The logical retention target is at least 14 complete days. The extra fetch day
provides margin for UTC bucket boundaries, request timing, and incomplete
candles.

The cache must contain at least:

```text
57 complete 4h bars
```

The mathematical minimum for one in-house ADX series with length 14 and
smoothing 14 is:

```text
length * 2 + smoothing + 1 = 14 * 2 + 14 + 1 = 43 bars
```

The production score contract deliberately reserves 57 complete 4h bars as the
readiness requirement, providing a margin above that mathematical minimum. The
implementation must enforce 57 at the regime boundary; the raw 43-bar formula
must not be used to make rotation appear ready.

### 3.2 Direct 1h history

The regime worker fetches at least 4 calendar days of completed Bybit linear
perpetual `1h` candles for every asset that enters the active feed and does not
already have sufficient cache coverage. The logical retention target is at
least 3 complete days, or 72 complete bars. The extra fetch day provides margin
for UTC bucket boundaries, request timing, and incomplete candles.

The cache must contain at least:

```text
57 complete 1h bars
```

This is the production readiness threshold for the in-house ADX14
implementation. The direct 1h cache supplies `adx_1h`, previous 1h ADX
readings, and all completed-1h RSI/divergence and reversal-gate inputs.

### 3.3 Remaining regime inputs

Direct higher-timeframe history does not replace every regime input. The worker
still uses:

- completed direct 1h bars from the regime cache;
- completed 5m bars for recent and prior realized-volatility windows.

The live evaluation bootstrap remains responsible for enough 1m and 5m history
for active strategy paths. Direct 1h regime history does not replace or alter
the strategy-facing 1h/4h views derived from the gateway's 5m data.

### 3.4 Completed-bar rule

The worker must discard the currently open 4h candle and any candle whose end
timestamp is after the requested completed cutoff. A bar is eligible only when:

- its end timestamp is normalized to the canonical UTC boundary;
- its OHLC values are finite and positive where applicable;
- its end timestamp is at or before the score cutoff;
- its interval is exactly the cache interval (`1h` or `4h`);
- it is not duplicated for the same asset and end timestamp.

Missing or gapped bars fail readiness for the affected asset. The worker must
not interpolate, fabricate, or silently compress a gap.

## 4. Ownership

`data/regime.sqlite3` remains owned and written by the managed
`research-analyst-regime-session` worker.

The worker may write the direct 1h/4h caches and derived regime observations in
that database. It must not write `data/market.sqlite3`. The orchestrator reads
the regime database read-only. The gateway remains the sole writer of
`data/market.sqlite3`.

The regime worker uses only Bybit's public REST API for historical 1h and 4h
bootstrap. This is not a second live market provider and is not a competing
source for 5m, 1m, or strategy evaluation bars.

## 5. Storage Contract

### 5.1 Direct-history bar tables

`regime_1h_bars` and `regime_4h_bars` each contain one immutable raw
direct-history row per asset, bar end, and source version. Their schemas are
identical except for the interval-specific table and bar version.

```text
bar_id             TEXT PRIMARY KEY
asset              TEXT NOT NULL
bar_end            TIMESTAMP NOT NULL
source             TEXT NOT NULL              -- bybit_rest
venue              TEXT NOT NULL              -- bybit
open               REAL NOT NULL
high               REAL NOT NULL
low                REAL NOT NULL
close              REAL NOT NULL
volume             REAL
source_start       TIMESTAMP
source_end         TIMESTAMP NOT NULL
request_id         TEXT
retrieved_at       TIMESTAMP NOT NULL
bar_version        TEXT NOT NULL
UNIQUE(asset, bar_end, source, bar_version)
```

The cache stores source provenance, not strategy candidates. It must be
queryable by asset and completed cutoff without scanning unrelated assets.

### 5.2 Direct-history backfill jobs

`regime_1h_backfill_jobs` and `regime_4h_backfill_jobs` each track durable
backfill state per asset.

```text
asset              TEXT PRIMARY KEY
status             TEXT NOT NULL              -- pending/running/ready/retryable/failed
required_from      TIMESTAMP NOT NULL
required_through   TIMESTAMP NOT NULL
covered_bars       INTEGER NOT NULL
missing_bars       INTEGER NOT NULL
attempts           INTEGER NOT NULL
lease_until        TIMESTAMP
next_retry_at      TIMESTAMP
last_error         TEXT
updated_at         TIMESTAMP NOT NULL
```

The job is `ready` only after the completeness check passes. A successful
HTTP response without sufficient complete bars is not success.

### 5.3 Regime score provenance

Each `regime_scores` row must identify the direct 1h and 4h bar IDs used for
ADX and reversal evidence, plus the canonical 5m source IDs used for
realized-volatility inputs. This makes mixed-input provenance explicit rather
than implying that every input came from one table.

## 6. Backfill Lifecycle

### 6.1 First entry or re-entry

1. The rotation feed identifies the active asset and feed version.
2. The regime worker creates or resumes its 1h and 4h backfill jobs.
3. The worker requests 4 days of public Bybit 1h history and 15 days of public
   Bybit 4h history.
4. The worker normalizes, validates, and transactionally upserts both intervals.
5. The worker verifies at least 57 complete bars and no gaps in each required
   range.
6. The worker marks each interval job `ready` independently and scores the next
   eligible cutoff only when all required direct history and volatility inputs
   are ready.

If the job is not ready by the cutoff grace deadline, that asset receives a
data-blocked regime decision. Other assets continue independently.

### 6.2 Refresh

For an already-ready asset, normal 1h and 4h updates can be obtained from the
same public REST path on their completed cadences. Live 1h and 4h WebSocket
topics are not required for correctness and are not part of this design.

The implementation may optimize refresh requests, but it must preserve the
same completed-bar and source-version rules as the initial bootstrap.

### 6.3 Live evaluation bootstrap

The gateway continues to backfill the active asset's recent `1m` and `5m`
history before or during re-entry. WebSocket data is the ongoing latest source.
The 5m bootstrap must remain sufficient for the enabled strategy cadences,
1h-derived context, volatility, and existing deep-warmup checks.

The regime and gateway readiness states are separate:

```text
regime_1h_ready       -> direct 1h regime history is usable
regime_4h_ready       -> direct 4h regime history is usable
regime_history_ready  -> both direct regime histories are usable
market_1m5m_ready     -> live evaluation history is usable
websocket_live        -> latest observations are flowing
```

Under `enforce`, a symbol may enter strategy evaluation only when the required
state for that path is ready. Under `shadow`, all state and would-block reasons
remain observable without suppressing evaluation.

## 7. Rotation Atomicity

A feed change must not create a hidden race where the regime worker evaluates a
new asset before its direct 1h and 4h caches exist.

The implementation must expose per-asset bootstrap status and use one of these
equivalent coordination mechanisms:

- delay enforcement activation for the asset until both bootstrap paths report
  ready; or
- publish the asset as present but require the exact readiness record in the
  regime gate before invoking any enforced plugin.

The evaluator must never treat a missing bootstrap record as ready. A temporary
backfill failure blocks only the affected asset and is recorded as data
readiness, not as an executor rejection.

## 8. Retention and Maintenance

The regime worker owns pruning of `regime_1h_bars`, `regime_4h_bars`, and both
backfill job tables on its writer connection.

- Retain at least 3 complete days of 1h bars per asset.
- Retain at least 14 complete days of 4h bars per asset.
- Retain score and gate observations according to the separate regime audit
  retention setting.
- Never prune bars needed by an active backfill job or an in-flight cutoff.
- Delete old rows before any throttled compaction.
- `VACUUM` must run only on the regime worker's writer connection.

At 72 1h bars and 84 4h bars per asset for 92 assets, the direct cache is
approximately 14,400 raw bars. Storage is intentionally negligible compared
with the existing market database.

## 9. Failure Semantics

- HTTP failure: retry the affected interval job with bounded backoff.
- Rate limit: honor the provider's retry signal and preserve the lease.
- Malformed response: mark the attempt retryable and record a sanitized error.
- Insufficient complete bars: keep the affected interval job `retryable` and
  record coverage counts; the asset's regime score is `insufficient_data`.
- Gap or duplicate: fail readiness; do not repair by interpolation.
- Missing exact cutoff result: block that asset in `enforce` mode.
- No fallback from direct 1h/4h history to a second live provider.
- No fallback from regime 1h/4h history to partial 5m-derived higher-timeframe
  scores.

## 10. Resource Budget

For 34 active assets and 92 approved assets:

```text
Live WebSocket topics:          34 x (1m + 5m) = 68
Regime 1h/4h WebSocket topics:  0
Initial direct 1h rows:         92 x 72 ~= 6,600
Initial direct 4h rows:         92 x 84 ~= 7,700
Initial direct history requests: approximately two per asset
Regime DB writers:               1
Market DB writers:               1
```

The direct 1h/4h caches avoid fetching thousands of 5m bars solely to
manufacture the regime's higher-timeframe warmup. The live 1m/5m bootstrap
remains bounded to active evaluation needs.

## 11. Validation Requirements

Before enabling enforcement with this cache:

- verify 4-day 1h and 15-day 4h requests exclude open candles;
- verify at least 57 complete 1h bars are available at every ready cutoff;
- verify at least 57 complete 4h bars are available at every ready cutoff;
- verify an interval-specific gap blocks only that asset and interval;
- verify a 4h gap blocks only that asset;
- verify repeated 1h and 4h fetches are idempotent;
- verify direct 1h and 4h values against 5m-resampled values over overlap windows;
- measure Bybit request latency and rate-limit behavior at 34-asset rotation;
- verify a new rotation cannot race the readiness decision;
- verify 1h and volatility inputs remain point-in-time and complete;
- verify retention leaves at least 14 days after pruning;
- verify the orchestrator never writes the regime database.

The direct and resampled comparison is a parity diagnostic, not permission to
silently substitute one source for the other. Any accepted numerical difference
must be documented in the score version.

## 12. Explicit Non-Goals

- No direct 1h/4h WebSocket subscription.
- No continuous 1h/4h stream for the full approved universe.
- No direct 1h/4h history written by the gateway.
- No direct 1h/4h history used to silently replace strategy 1h/4h context.
- No reduction of the 57-bar ADX readiness requirement.
- No global pause because one rotated asset is warming up.
- No code or production configuration change implied by this document alone.
