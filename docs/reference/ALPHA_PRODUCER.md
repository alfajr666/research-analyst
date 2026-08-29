# Alpha Producer

## Purpose

This project evolves from a Telegram research-and-alert system into an **alpha
producer**: an independent source of predictive, trade-ready crypto-perpetual
opportunities.

It complements each trading engine. It does not replace the engine's existing
discovery and strategy pipeline.

```text
Engine-native path
Discovery -> Strategy -> Positioning -> Execute

Analyst-assisted path
Alpha producer ----------------------> Positioning -> Execute
```

Both paths may operate at the same time. The positioning/risk layer remains
responsible for duplicate suppression, portfolio conflicts, sizing, and venue
eligibility.

## Ownership Boundary

The alpha producer owns a portable market thesis:

- Candidate selection and ranking.
- Direction.
- Setup class and market phase.
- Expected horizon and event expiry.
- Entry condition and invalidation.
- Confidence calibrated from historical outcomes.

Each engine owns execution-specific decisions:

- Mapping a canonical asset to its MEXC, Bybit, Bybit testnet, or Propr
  instrument.
- Verifying that the mapped instrument is tradeable and liquid.
- Position sizing, leverage, and portfolio risk controls.
- Market versus limit order selection, order placement, retries, and lifecycle.

The producer must not emit a ticker alone. A ticker has no direction, timing,
or falsifiable thesis, and is not sufficient to bypass discovery and strategy.

## Alpha Event

The producer publishes a versioned, venue-neutral alpha event. Venue mapping
happens after the event is accepted by an engine.

```json
{
  "schema_version": 1,
  "alpha_id": "uuid",
  "strategy_id": "continuation-breakout-v1",
  "asset": "SOL",
  "direction": "long",
  "setup_class": "continuation_breakout",
  "phase": "confirmed_expansion",
  "observed_at": "2026-08-16T10:15:00Z",
  "valid_until": "2026-08-16T11:00:00Z",
  "horizon_minutes": 240,
  "confidence": 0.67,
  "entry_condition": {
    "type": "breakout_above",
    "price": 145.20
  },
  "invalidation_price": 142.70,
  "targets": [148.10, 151.00],
  "feature_snapshot": {
    "regime": "trending_up",
    "volume_zscore": 2.1,
    "oi_change_1h": 0.08
  }
}
```

`confidence` is a calibrated estimate, not a count of overlapping technical
indicators. The immutable feature snapshot allows every production event to be
evaluated later.

## Alpha Families

The producer intentionally supports two different forms of alpha. They have
different labels, data requirements, risk profiles, and success criteria.

| Family | Question | Typical phase | Trade-off |
| --- | --- | --- | --- |
| Continuation | Is an established move likely to continue? | Confirmed expansion or pullback | Later entry, generally more confirmation and less remaining range |
| Impulse ignition | Is a large directional move likely to start soon? | Pre-breakout or early expansion | Earlier entry, lower certainty, potentially larger payoff |

### Continuation

Continuation is valid alpha: buy high and sell higher, or short weakness and
cover lower. The current 15m EMA/POC/RSI/4h/HMM stack is a starting hypothesis
for this family because it deliberately confirms structure and momentum.

It should be evaluated as a continuation system, rather than described as
predictive discovery.

Candidate setup classes:

- `continuation_breakout`: established trend, local value compression, then
  directional acceptance beyond the range.
- `continuation_pullback`: established trend, controlled pullback to a
  structural reference, then renewed directional participation.

### Impulse Ignition

Impulse alpha tries to find an asset before it appears in top-gainer or
top-mover discovery screens. It predicts an upcoming expansion rather than
confirming a move already underway.

An initial testable hypothesis using the current dataset is:

```text
Tight realized range / declining ATR
+ abnormal volume or trade activity
+ open-interest change disproportionate to price change
+ uncrowded funding or positioning in the proposed direction
+ relative strength or weakness against BTC
= elevated probability of a directional expansion
```

Candidate setup classes:

- `impulse_ignition`: compressed local balance with positioning and activity
  changes that precede a directional break.
- `squeeze_ignition`: positioning is crowded against a locally strengthening
  direction, increasing the chance of forced continuation.

## Research Universe and Liquidity Tiers

The research universe includes large and small assets, but it is segmented by
tradability rather than market capitalization. A small asset with reliable
perpetual volume and open interest belongs in the research universe; a thin
asset with an impressive historical percentage move may not be executable.

| Tier | Definition | Initial use |
| --- | --- | --- |
| `core` | High-notional, consistently liquid perpetuals | Benchmark and lower-cost continuation research |
| `emerging` | Smaller but liquid perpetuals meeting the research volume floor | Primary expansion and ignition research universe |
| `not_eligible` | Insufficient liquidity, history, or data quality | Retain only when data is available; do not emit alpha |

Tier membership is a point-in-time observation. The producer records every
scanner universe snapshot with 24-hour notional volume, price, tier, and
whether the contract was selected for detailed scanning. Backtests must use
the historical snapshot instead of applying today's universe to past data.

Features and performance reports are normalized and reported by tier. A volume
spike, OI change, or expected move is relative to an asset's own trailing
history; global absolute thresholds do not transfer between BTC and a smaller
perpetual.

An event is eligible for production only when a target engine can map it to a
currently tradeable venue instrument. The engine remains the final authority
on current liquidity and executable cost.

## Two-Pool Discovery Module

The broad discovery module runs before deep SQLite ingestion. It does not
select assets because they are already trending; it records a point-in-time
eligible universe and independently ranks two small pools.

```text
Eligible perpetual universe
        |
Hourly low-cost state snapshot
        |
        +-> ignition pool: quiet, constructive pre-breakout bases
        +-> continuation pool: active participation and established movement
        |
20 deep-watchlist assets total
        |
15m OHLCV/OI/funding backfill and strategy evaluation
```

### Broad Snapshot

Every eligible contract records its hourly liquidity and discovery features,
including volume, open interest, price change, funding, and long/short-ratio
change when available. It is retained even for rejected assets so research can
reconstruct the selection decision without survivorship bias.

Eligibility is static and execution-oriented: venue mapping, rolling notional
floor, history warmup, and data freshness. A current top-mover rank is not an
eligibility condition.

### Independent Rankings

| Pool | Purpose | Positive inputs | Exclusions |
| --- | --- | --- | --- |
| `ignition` | Find latent expansion before broad attention | Quiet price, compression, modest relative strength, OI pressure with limited price movement, neutral funding | Fresh breakout, high current price change, volume spike, post-breakout pullback |
| `continuation` | Find active second-wave opportunities | Volume/OI participation, price movement, trend confirmation | Insufficient liquidity or exhausted/crowded expansion |

Raw metrics are converted to each asset's trailing percentile or robust z-score
where history exists. `volume / OI` never ranks alone: it is combined with
absolute-liquidity floors because low OI can otherwise dominate the list.
Long/short-ratio **change**, not its absolute level, is the intended feature.

### Lifecycle

1. An asset enters a pool when it ranks in that pool's top 10 and is eligible.
2. On first entry, deep ingestion backfills at least 14 days of 15m OHLCV, OI,
   and funding before the asset can produce a strategy event.
3. An active asset remains for at least 24 hours, then must requalify.
4. It expires on stale data, loss of liquidity, strategy invalidation, or a
   completed/exhausted move.

The module emits research watchlist entries only. Strategy modules evaluate
them later; no discovery rank is an order instruction.

## Evaluator Topology

`ws_gateway` owns live Bybit WS market ingestion and writes the market SQLite
database. The orchestrator owns finalized cutoffs and the analyst SQLite
database. Evaluators read committed observations and publish versioned events;
they do not call CoinAnalyze or any live external market provider directly.

```text
orchestrator -> raw data + active watchlists -> evaluators -> alpha outbox
```

| Process | Cadence | Watchlist | Responsibility |
| --- | --- | --- | --- |
| `ws_gateway` | continuous | static/optional rotated universe | Bybit WS 1m/5m/mark; local resampling |
| `orchestrator` | configured cutoff loop | compact universe | Finalized cutoffs, four compact plugins, admission, delivery |
| `pm_sidecar` | 5m cutoff | open executor positions | LLM HOLD/REDUCE/EXIT, fail-safe HOLD |
| raw Discord batch | 30m UTC windows | captured candidates | Non-blocking observation delivery |

The outbox is append-only and deduplicated by strategy, asset, direction, and
observation timestamp. It is the seam consumed later by the alpha-event writer
and execution engines. Evaluators remain read-only against SQLite, avoiding
multi-writer contention.

## Signal Publisher: Alpha and Intent

`signal_publisher.py` persists and delivers the alpha ledger. Selected compact
candidates also follow the immediate atomic intent path to the fixed `bybit / hyro`
executor; this is independent of Discord delivery.

```text
evaluator outbox event
        |
signal publisher
        |
        +-> alpha_events: immutable authoritative DB log
        +-> signal_deliveries: Telegram attempt and result log
        +-> Telegram notification
```

The publisher is the sole SQLite writer for alpha-event delivery records in the
dedicated `ANALYST_DB_PATH` database, separate from the gateway-owned market database. It
must persist a validated event before attempting Telegram delivery, deduplicate
by the event outbox key, and retry undelivered Telegram messages without
duplicating the event itself.

### Event Lifecycle

| Event status | Meaning |
| --- | --- |
| `active` | Published event remains valid until its expiry or later invalidation |
| `expired` | Validity window elapsed |
| `invalidated` | An evaluator explicitly withdrew the thesis |

Telegram delivery is independent of event status. A Telegram outage creates a
pending/failed delivery receipt, not a missing alpha event.

### Delivery Requirements

- Render the strategy family, asset, direction, phase, confidence, trigger,
  invalidation, targets, expiry, and source timestamp.
- Send only active events with unexpired `valid_until`.
- Record every delivery attempt, response/error, and completion timestamp.
- Retry transient failures with bounded exponential backoff.
- Never infer execution, venue mapping, sizing, or order type.

## Cadence and Data-Speed Boundary

Engines cycle every five minutes and do not compete for sub-minute execution.
The producer is therefore a medium-frequency selection system, not a latency
or order-book-arbitrage system.

The initial producer may emit after each completed 15-minute market bar while
engines poll for new events every five minutes. An event must remain valid long
enough for at least one engine cycle. Signals must use completed bars only;
in-progress 15-minute candles are not eligible for research or production
decisions.

```text
15m aggregate futures data -> early expansion and continuation research
5m engine polling -> event delivery, position checks, and execution
15m to hours holding horizon -> initial alpha target
```

The first research iteration should use the existing 15m data and target
opportunities that develop over at least one 15-minute bar. Faster data sources
are out of scope unless a later, measured result shows that they are necessary.

## Research Rules

Every setup class is an independently versioned hypothesis. Never combine
impulse and continuation samples into one score or one performance report.

The candidate ledger stores both armed and triggered candidates. It is
append-only: a later result never overwrites the features available at the
candidate timestamp. Outcome rows are recorded separately and joined through
the candidate identifier.

For every emitted candidate or alpha event, retain:

- Observation and emission timestamps.
- Asset, direction, setup class, phase, and strategy version.
- Complete point-in-time feature snapshot.
- Entry condition, invalidation, targets, and expiry.
- Forward returns at fixed horizons.
- Target/stop/expiry outcome and elapsed time.
- Maximum favorable and adverse excursion.
- Estimated fees, spread, slippage, and funding.
- Results by liquidity bucket and market regime.

Performance must be measured with walk-forward, point-in-time evaluation.
The model may only use information available at the event timestamp. The
current HMM backtest does not meet this requirement and must not be used as
evidence of alpha until corrected.

## Initial Research Sketch

### 1. Continuation Breakout v1

**Thesis:** In a higher-timeframe trend, a narrow 15m value/EMA compression
with renewed volume and directional acceptance is more likely to continue than
reverse during the next 1-4 hours.

**Existing inputs:** 15m OHLCV, volume profile, EMA26/99, RSI, ATR, 4h
structure, daily VWAP/HMM regime, open interest, funding, liquidations.

**Outcome:** Long/short return from confirmed breakout through 1h and 4h, plus
barrier outcome against a predefined invalidation.

**Baseline:** Trend direction plus a simple range breakout. The full setup must
outperform this baseline after costs.

### Trend Re-acceleration Quality Score

The first scanner is a ranking model, not an exact chart-pattern matcher. No
explosive move is expected to reproduce a fixed sequence. Instead, every asset
with sufficient point-in-time history receives component scores for:

- Established trend.
- Quality of the recent base and volatility compression.
- Breakout acceptance above the base.
- Spot/perpetual participation through volume and OI change.
- Relative strength versus BTC.
- Crowding and late-entry risk from funding, range expansion, and extension.

Presets change the relative importance of the components rather than imposing
different market truths:

| Preset | Intent | Relative emphasis |
| --- | --- | --- |
| `early` | Rank assets near the end of a constructive base | Trend, compression, relative strength |
| `balanced` | Rank first-breakout candidates | Even quality score with breakout confirmation |
| `confirmed` | Rank demonstrated follow-through | Breakout acceptance and participation |

The score is a research prior. It is not a probability, order instruction, or
hard eligibility gate. Backtests must evaluate score buckets and individual
components against matched controls before any preset can produce alpha events.

`explosion_ignition` is separate from this continuation ranker. It only ranks
the armed state: price remains inside a compressed base after a prior impulse.
Fresh breakouts and post-breakout pullbacks are excluded rather than scored.

### 2. Impulse Ignition v1

**Thesis:** Before a large 15m-to-4 hour move, an asset often enters compressed
price action while activity and derivatives positioning change ahead of price.

**Initial features:** 15m and 1h realized range percentile, ATR compression,
volume z-score, volume change, OI change, OI-to-price-change divergence,
funding percentile/change, long-short ratio change, and BTC-relative return.

**Label:** A directional move of at least a chosen ATR threshold within a fixed
horizon before an opposite adverse threshold is reached.

**Baselines:** Random liquid-universe selections, top-volume selection,
top-mover selection, and a simple range-breakout rule.

### 3. Evaluation Sequence

1. Retain sufficient raw history across multiple market regimes.
2. Build an immutable candidate and outcome ledger before parameter tuning.
3. Implement point-in-time feature generation and walk-forward evaluation.
4. Compare each setup class with its baselines after conservative trading
   costs.
5. Calibrate confidence from out-of-sample outcomes.
6. Promote only proven setup classes to `alpha_event` production output.

## Non-Goals for the First Iteration

- Replacing engine-native discovery or strategy modules.
- Venue-specific execution logic in this repository.
- Treating technical confluence counts as calibrated probability.
- Claiming sub-minute prediction from 15-minute aggregate data.
- Promoting an untested heuristic alert into executable alpha.
