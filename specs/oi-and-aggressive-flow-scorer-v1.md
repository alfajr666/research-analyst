# OI and Aggressive-Flow Scorer v1

**Status:** Proposed, staged implementation

## Decision

Implement cutoff-bound open-interest observations and score them in shadow.
Implement trade-level aggressive-flow capture only as a bounded research pilot.
Neither component receives production score weight until the acceptance gates in
this specification pass.

| Component | Expected incremental value | Operating cost | v1 decision |
| --- | --- | --- | --- |
| Open-interest participation | Medium-high | Low-medium | Implement for candidate assets |
| Aggressive-flow imbalance (CVD-derived) | Potentially medium-high at short horizons | High | Bounded liquid-universe capture; shadow only |

Research Analyst remains an intent producer. These observations may annotate and
eventually rank an admitted candidate, but never place orders, size a position,
or bypass admission. The terminal boundary remains a validated TradeIntent
published to the shared bus.

## Rationale and evidence

The current quality scorer already contains structural context, RVOL, funding,
freshness, and same-direction agreement. OI adds whether a price move is
accompanied by changing derivatives exposure. Aggressive flow adds the direction
of taker-initiated notional, which candle volume does not contain.

The value is conditional:

- OI has no direction by itself; it requires price change, candidate direction,
  market family, and funding context.
- Raw CVD level is anchor-dependent. Use bounded signed-flow windows and
  price/flow divergence, not an ever-growing cumulative number.
- Both observations are venue-local and must not be called global positioning
  or global order flow.
- Both overlap partly with RVOL and funding. Only incremental out-of-sample
  value justifies production weight.

Bybit exposes timestamped 5-minute OI history through
[`/v5/market/open-interest`](https://bybit-exchange.github.io/docs/v5/market/open-interest).
Its public trade stream exposes taker side, size, price, trade ID, and sequence
through [`publicTrade.{symbol}`](https://bybit-exchange.github.io/docs/v5/websocket/public/trade).
Research finds predictive information in crypto order flow, but that does not
prove a venue-local five-minute CVD feature improves this scorer; the repository
must establish that on its own candidates and outcomes. See
[Order flow and cryptocurrency returns](https://doi.org/10.1016/j.finmar.2026.101047).

Canonical 5m rows currently persist `open_interest=null`; optional OI terms in
retired strategies are not a live data contract. There is no active taker-trade
store. Missing history must remain unavailable, never fabricated as zero.

`data/binance_oi.db` remains owned by `binance-scanner-oi`. This implementation
must never open, copy, prune, or depend on it.

## Open-interest data contract

The orchestrator owns candidate-scoped OI enrichment because it owns
`data/analyst.sqlite3` and already performs post-candidate enrichment.

Add this table:

```sql
CREATE TABLE oi_observations (
  venue TEXT NOT NULL,
  native_symbol TEXT NOT NULL,
  asset TEXT NOT NULL,
  interval TEXT NOT NULL,
  source_at TEXT NOT NULL,
  retrieved_at TEXT NOT NULL,
  open_interest REAL NOT NULL,
  source_version TEXT NOT NULL,
  PRIMARY KEY (venue, native_symbol, interval, source_at)
);
```

Rules:

1. Fetch only assets that emitted candidates for the cutoff.
2. Request native Bybit `5min` OI ending at the exact evaluation cutoff. Never
   accept `timestamp > evaluation_cutoff`.
3. Seed up to 200 observations on first use; fetch only the missing suffix later.
4. Persist immutable source timestamps before scoring.
5. Bound concurrency and retries. Failure produces `unavailable` and does not
   fail the pipeline.
6. Replays use persisted observations only unless an explicit research backfill
   is requested.
7. Retain at least 30 days through analyst-owned bounded retention.

The scorer remains pure. A `derivatives_context` stage loads cutoff-bound OI and
passes immutable observations into `ScoreContext`; components perform no I/O.

## Aggressive-flow data contract

Recent-trade REST responses are not a completeness contract for active
five-minute windows. True aggressive flow requires the public trade stream.

Add an optional `research-analyst-market-flow` worker owning
`data/market_flow.sqlite3`. It must not write `market.sqlite3` or
`analyst.sqlite3`. Initially subscribe only to permanent assets with native
Bybit mappings plus the 32 highest-turnover assets in the effective universe.
Expansion requires a capacity review.

Persist completed one-minute aggregates, never raw trades:

```sql
CREATE TABLE aggressive_flow_1m (
  venue TEXT NOT NULL,
  native_symbol TEXT NOT NULL,
  asset TEXT NOT NULL,
  source_start TEXT NOT NULL,
  source_end TEXT NOT NULL,
  buy_notional REAL NOT NULL,
  sell_notional REAL NOT NULL,
  net_notional REAL NOT NULL,
  total_notional REAL NOT NULL,
  trade_count INTEGER NOT NULL,
  first_sequence INTEGER,
  last_sequence INTEGER,
  completeness TEXT NOT NULL,
  source_version TEXT NOT NULL,
  PRIMARY KEY (venue, native_symbol, source_start)
);
```

Use taker side for buy/sell notional. Ignore duplicate trade IDs and sequences.
Any sequence gap, reconnect gap, clock regression, malformed trade, or partial
minute marks the window incomplete. Retain aggregates for 14 days. The scorer
opens the database read-only and uses only completed rows ending at or before
the cutoff.

## Scorer observations

### `oi_participation` (`oi-participation-v1`)

Inputs are candidate direction/family, 15m and 60m price returns, same-window OI
changes, the rolling OI-change percentile, and funding overheating.

| Price | OI | Interpretation |
| --- | --- | --- |
| Up | Up | new participation biased with the move |
| Down | Up | new participation biased with the move |
| Up | Down | position closing / short covering |
| Down | Down | position closing / long liquidation |

The first two states may support a same-direction trend candidate. Closing-led
moves receive weaker trend support. Mean-reversion/reversal mappings may treat
extreme same-direction OI expansion plus overheated funding as crowding.

Normalize magnitude within each asset. Fixed cross-asset OI thresholds are
forbidden. Fewer than 32 points, stale data, gaps, or cutoff mismatch returns
`value=0.5`, `status=unavailable`.

### `aggressive_flow` (`aggressive-flow-v1`)

For complete 5m, 15m, and 60m windows compute:

```text
delta_ratio = (buy_notional - sell_notional) / total_notional
```

Normalize from observations strictly before the evaluated window. Combine
candidate-direction alignment, 5m/15m persistence, price/flow divergence, and
within-asset notional readiness. Never expose lifetime CVD or compare raw
notional across assets. Any incomplete contributing minute makes the affected
window unavailable.

## Weighting

Both components initially persist with weight `0.0`. Their presence bumps score
policy/profile versions because audit provenance changes.

If promoted, combined weight must not exceed `0.12`. Take weight primarily from
RVOL, funding, and same-direction agreement, never from identity, geometry,
structural stop, or freshness. Missing data remains neutral `0.5`; it does not
change normalization per candidate.

## Acceptance gates

1. Collect at least eight weeks of observations and resolved candidate outcomes.
2. Use anchored or walk-forward time splits; asset-random splits are forbidden.
3. Compare baseline, OI-only, flow-only, and OI+flow on the same candidates.
4. Report by family, strategy, direction, and liquidity tier.
5. Measure rank discrimination, selected-candidate outcome rate, calibration,
   coverage, and selection turnover.
6. Use block-bootstrap confidence intervals by cutoff/day. Promotion requires a
   positive lower confidence bound on the locked primary metric and no material
   live-family degradation.
7. Prove incremental value after RVOL and funding; redundant components remain
   at zero weight.
8. Lock transforms/weights before the final holdout.

Insufficient sample size means the component stays shadow.

## Cost and health

OI cost is bounded by emitted candidate assets and cached history. Record calls,
latency, errors, cache hits, observation age, and rows written.

CVD cost scales with trades, not cutoffs. Record messages/trades per second,
bytes, queue depth/age, CPU, memory, duplicates, sequence gaps, completeness,
database bytes, and prune duration. Flow is unhealthy if it delays canonical
evaluation, p99 queue age exceeds one second for five minutes, or incomplete 5m
windows exceed 1% per day. Unhealthy flow never blocks OI, evaluation,
admission, or shared-bus publication.

## Rollout and tests

1. Add OI schema, retention, loader, and deterministic fixtures.
2. Emit shadow OI observations and collect eight weeks.
3. Promote OI only after its locked ablation passes.
4. Separately deploy the bounded flow worker at score weight zero.
5. Validate capacity/completeness before predictive analysis.
6. Promote flow only after its independent ablation passes.

Tests must cover exact cutoffs, replay without network calls, family-aware OI
mapping, neutral missing data, trade dedupe, sequence/reconnect gaps, taker-side
mapping, window boundaries, unchanged shadow scores, and the inability of either
module to bypass admission or publish directly to the bus.

## Non-goals

- Cross-exchange synthetic OI/CVD or reading `data/binance_oi.db`.
- Candle-direction proxies presented as CVD.
- Raw trade retention, order-book imbalance, liquidations, or VPIN.
- Hard-gating a candidate on OI or flow.
- Venue-adapter, execution, position-management, or LLM behavior.
