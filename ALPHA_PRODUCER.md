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
