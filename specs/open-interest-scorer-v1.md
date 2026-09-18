# Open-Interest Scorer v1

**Status:** Proposed, staged implementation

## Decision

Implement cutoff-bound open-interest observations as an operationally weighted
part of the reaction scorer's shadow/enforce path.
Do not implement CVD or a trade-level aggressive-flow collector for the current
completed-bar architecture.

| Component | Expected incremental value | Operating cost | v1 decision |
| --- | --- | --- | --- |
| Open-interest participation | Medium-high | Low-medium | Implement for candidate assets |
| CVD / aggressive-flow imbalance | Low-confidence incremental value | High | Rejected for this architecture |

Research Analyst remains an intent producer. OI may rank an admitted candidate
when the reaction scorer is enforced, but never place orders, size positions,
bypass admission, or publish anywhere except the shared intent bus.

## Why CVD is out of scope

Strategies evaluate completed 5m bars, including strategies whose setup frame
is 15m or higher. They do not make sub-bar execution decisions. Aggregating
every trade back into the same completed decision window gives up most of the
timing advantage that motivates continuous aggressive-flow capture.

At these horizons, bounded buy/sell flow would also overlap existing OHLCV,
RVOL, candle structure, momentum, funding, and agreement inputs. Incremental
value is therefore uncertain while the operational cost is certain: continuous
subscriptions, deduplication, reconnect and gap handling, capacity during
volatility, retention, and a new database-owning worker.

CVD may be reconsidered only if either condition becomes true:

1. execution changes to intrabar or sub-minute decisions; or
2. a trustworthy historical trade archive demonstrates material incremental
   value over the existing inputs on locked, time-split candidate outcomes.

Do not build a candle-direction proxy and label it CVD. Bybit exposes true
taker-side trades through
[`publicTrade.{symbol}`](https://bybit-exchange.github.io/docs/v5/websocket/public/trade),
but this specification intentionally does not consume that stream.

## Rationale for OI

OI adds a distinct question: whether a price move is accompanied by expanding
or contracting derivatives exposure. It remains contextual rather than
directional by itself and must be combined with price change, candidate
direction, strategy family, and funding.

Bybit exposes timestamped 5-minute OI history through
[`/v5/market/open-interest`](https://bybit-exchange.github.io/docs/v5/market/open-interest).
Canonical 5m rows currently persist `open_interest=null`; optional OI terms in
retired strategies are not a live contract. Missing history remains
unavailable, never fabricated as zero.

`data/binance_oi.db` remains owned by `binance-scanner-oi`. This implementation
must never open, copy, prune, or depend on it.

## Data contract

The orchestrator owns candidate-scoped OI enrichment because it owns
`data/analyst.sqlite3` and already performs post-candidate enrichment.

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
passes immutable observations into `ScoreContext`; score components perform no
network or database I/O.

## `oi_participation` (`oi-participation-v1`)

Inputs are candidate direction, 15m and 60m price returns, same-window OI
changes, and rolling within-asset absolute OI-change percentiles.

| Price | OI | Interpretation |
| --- | --- | --- |
| Up | Up | new participation biased with the move |
| Down | Up | new participation biased with the move |
| Up | Down | position closing / short covering |
| Down | Down | position closing / long liquidation |

Convert price return into candidate-aligned sign before interpreting the table.
For each horizon, aligned price plus rising OI scales from `0.50` toward `1.00`
by OI-change percentile; aligned price plus falling OI is closing-led and scores
`0.55`; opposing price plus rising OI scales from `0.50` toward `0.00`; and
opposing price plus falling OI scores `0.35`. Flat price is neutral.

Combine the horizons without candidate-specific renormalization:

```text
oi_confirmation = 0.60 * oi_15m + 0.40 * oi_60m
```

Funding does not enter this calculation. The reaction scorer's separate
crowding leg owns funding so the same evidence is not counted twice.

Normalize magnitude within each asset. Fixed cross-asset OI thresholds are
forbidden. Fewer than 32 observations, stale data, gaps, or cutoff mismatch
returns `value=0.5`, `status=unavailable`.

## Weighting and rollout

`REACTION_SCORER_MODE=off|shadow|enforce` is the sole rollout control.
`OI_SHADOW_ENABLED` is removed rather than retained as an alias.

| Mode | OI collection and v3 calculation | Operational result |
| --- | --- | --- |
| `off` | no candidate-scoped OI fetch; no v3 | legacy v2 |
| `shadow` | collect OI and compute v3 with OI weight `0.15` | legacy v2 |
| `enforce` | identical collection and v3 computation | weighted v3 |

Reaction-scorer v3 fixes OI at weight `0.15`, funded by reducing RVOL and
funding/crowding weights rather than by weakening admission. Missing data is
neutral `0.5`, retains the fixed 0.15 denominator, and never hard-gates
admission. Shadow therefore measures the exact score that enforce will select;
there is no zero-weight OI mode and no silent normalization.

Promotion requires:

1. at least eight weeks of observations and resolved candidate outcomes;
2. anchored or walk-forward time splits, never asset-random splits;
3. baseline versus OI-only comparison on identical candidates;
4. reporting by family, strategy, direction, and liquidity tier;
5. block-bootstrap confidence intervals by cutoff/day;
6. a positive lower confidence bound on the locked primary metric with no
   material live-family degradation;
7. demonstrated incremental value after RVOL and funding; and
8. transforms and weight locked before the final holdout.

Insufficient sample size or redundant value keeps the whole reaction scorer in
`shadow`; it does not create a nominal enforce mode in which OI has no effect.

## Cost, rollout, and tests

OI cost is bounded by emitted candidate assets and cached history. Record calls,
latency, errors, cache hits, observation age, and rows written.

Rollout order:

1. add the OI schema, retention, loader, and deterministic fixtures;
2. compute the fully weighted v3 in `shadow` and collect at least eight weeks;
3. run the locked ablation; and
4. switch `REACTION_SCORER_MODE=enforce` only if every acceptance gate passes.

Tests cover exact cutoffs, replay without network calls, mirrored directional
mapping, within-asset normalization, neutral missing data, unchanged shadow
operational scores, a weighted shadow-v3 OI contribution, enforce selection of
that exact v3 result, and the inability of OI to bypass admission or publish
directly to the bus.

## Non-goals

- CVD, aggressive-flow capture, raw trades, order-book imbalance, liquidations,
  or VPIN.
- Cross-exchange synthetic OI or reading `data/binance_oi.db`.
- Hard-gating a candidate on OI.
- Venue-adapter, execution, position-management, or LLM behavior.
