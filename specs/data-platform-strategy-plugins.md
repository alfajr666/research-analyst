# Data Platform and Strategy Plugin Architecture

## Status

Historical platform design, retained for schema and plugin-contract reference.
The current operational truth is `README.md` and `agent.md`: SQLite is split
between the Bybit WS market writer and analyst writer, and the four compact
strategies own the live admission path. Earlier CoinAnalyze, PM2, and multi-target
deployment language below is superseded where it conflicts with those documents.

## Problem Statement

The Alpha Producer already collects market data, ranks discovery pools, emits
venue-neutral alpha events, and delivers them through Telegram and optional
bot-inbox adapters. The boundaries between those layers are not explicit enough
for a multi-source data platform.

Current blockers include:

- orchestration, technical analysis, confluence logic, and presentation are
  partly interleaved
- raw market storage lacks provider/venue provenance and is rewritten in place
- two processes can write the same market database
- strategies are hard-coded daemons rather than enable/disable plugins
- OpenMarket free-tier enrichment cannot be added safely without a selected
  universe, budget accounting, and unavailable-state semantics

## Solution

Build a source-aware platform with four ownership layers:

```text
Discovery and ingestion
  -> database: append-only source observations and derived snapshots
  -> strategy plugins: read-only point-in-time decisions
  -> signal publisher: validated event persistence and delivery
```

CoinAnalyze remains the durable broad-universe backbone. OpenMarket Free is an
optional Bybit Perpetual enrichment source for a bounded selected universe.
Missing, stale, unaffordable, or rate-limited OpenMarket data produces
`unavailable` enrichment and never blocks CoinAnalyze discovery, ingestion,
strategy emission, or delivery.

Strategies are locally registered plugins. Existing accumulation-base,
impulse-ignition, and continuation-breakout logic migrates into enabled plugins
and becomes the sole event producer at a direct cutover. Downstream publisher
behavior, including Telegram and the existing execution adapter path, is left
unchanged by this work.

## Locked Product Decisions

### Sources and roles

| Source | Role | Retention |
| --- | --- | --- |
| CoinAnalyze | Broad universe backbone: 15m OHLCV, OI, funding, liquidations, discovery inputs | 365 days |
| OpenMarket Free | Selected-asset Bybit Perp enrichment: HTF profile, 15m flow, event-time VP/flow | 30 days for enrichment snapshots |
| Binance USDT-M | Liquidity/eligibility and OI-rotation discovery inputs | existing discovery retention policy, owned by orchestrator |

OpenMarket never replaces CoinAnalyze. It never decides broad-universe
membership. Raw live OpenMarket trades are not stored in phase one.

### OpenMarket enrichment universe

Permanent assets:

- BTC
- ETH
- SOL
- PAXG
- XAUT

Rotating candidates:

- top 15 ignition-pool ranks
- top 15 continuation-pool ranks
- unused slots may be filled by the other pool
- total rotating capacity is 30 candidates in addition to the permanent set

Discovery `DISCOVERY_TOP_N` expands from 10 to 15 per pool so the enrichment
allocation is actually supplyable.

Reference venue:

- OpenMarket exchange identity is Bybit Perpetual
- no multi-venue fallback in phase one
- unavailable Bybit contract => explicit `unavailable` enrichment state

### OpenMarket free-tier budget policy

OpenMarket Free limits are 1,000 weight/day and 10 weight/minute. Continuous
native profile refresh across the full selected universe is not feasible.

Budget allocation:

1. All selected assets: 7-day HTF volume profile refresh every 4 hours.
2. Permanent assets only: 15-minute buy/sell-flow candles every 15 minutes.
3. Ranked candidates: 15-minute VP/flow only at event-time, when a base
   strategy has a qualified candidate or pending event.
4. Remaining budget is reserved for those event-time checks.

Request policy:

- every OpenMarket call is optional and deadline-bounded
- if the deadline is missed, the strategy emits the normal CoinAnalyze-based
  event with OpenMarket context marked `unavailable`
- retries apply only to future observations, never to block the current cutoff
- durable `source_request_log` records type, weight, remaining budget, selected
  universe, status, and cutoff ID
- plugins never see request internals; they only see feature availability

Freshness:

- HTF profile evidence is valid for up to 4 hours plus a 15-minute grace period
- event-time 15m flow/profile evidence must end at the completed 15m cutoff and
  may be no more than 20 minutes old
- otherwise the evidence is `unavailable`

### Higher-timeframe structure and zones

Hierarchy:

```text
4h FVG / order block  = primary structural context
1h FVG / order block  = secondary refinement
15m                   = trigger only; no structural zone materialization
```

When 1h conflicts with 4h, 4h wins.

Source for zones:

- FVG and order-block zones are computed from CoinAnalyze-derived 1h and 4h bars
- Bybit OpenMarket VP/flow remains separate venue-scoped evidence

FVG rules:

- three-candle imbalance created only after the third bar closes
- minimum gap width: `0.25 * 14-period ATR` on the same timeframe
- partially mitigated on first wick into the zone
- filled when price reaches the far boundary
- separate invalidated state on the defined directional close-through condition

Order-block rules:

- last opposing candle before a displacement that closes through the prior
  20-bar swing high/low
- displacement bar must be at least `1.5 * 14-period ATR`
- zone is the full high-to-low range of the opposing candle
- partial mitigation follows the same wick-into-zone rule as FVG
- bullish block invalidates only on a completed close below its low
- bearish block invalidates only on a completed close above its high

Snapshot bound:

- full append-only zone history is retained for replay
- each strategy snapshot receives at most the three most recent active zones
  per asset, timeframe, direction, and zone type

### Confluence semantics

OpenMarket profile/flow and CoinAnalyze FVG/order-block evidence are advisory in
phase one:

- values are `support`, `neutral`, `contradict`, or `unavailable`
- they are attached to emitted events as evidence
- they do not suppress emission
- they do not alter confidence
- a count of supporting indicators is never treated as calibrated probability

Approximate CoinAnalyze volume profile remains available only as:

```text
coinalyze_candle_distributed_volume_profile_v1
```

It must never be labeled or substituted as native OpenMarket volume-at-price.

### Strategy plugins

Existing strategies migrate as enabled plugins:

- `accumulation-base-v1`
- `impulse-ignition-v1`
- `continuation-breakout-balanced-v1`

Enablement:

- restart-controlled allowlist `STRATEGY_ENABLED_IDS`
- unknown configured IDs fail startup
- resolved enabled set is logged at startup
- disabled plugins are not invoked
- prior events remain immutable and follow normal expiry lifecycle

Invocation:

- plugins run only inside the orchestrator after a completed UTC 15m cutoff is
  finalized
- independent evaluator daemons become library code and leave the runtime at
  cutover
- plugins are read-only against market data
- plugins write only through the existing alpha outbox seam
- one plugin failure is isolated and must not stop other plugins or delivery of
  already written outbox events

Event identity becomes:

```text
strategy_id + plugin_version + asset + direction + observed_at + input_snapshot_id
```

`alpha_id` and the outbox dedupe key derive from that full material. Events also
carry source-evidence IDs and the immutable feature snapshot.

Confidence:

```text
confidence = existing heuristic score mapped to 0..1
confidence_status = "uncalibrated"
```

Enrichment evidence is stored separately and never folded into confidence.

### Publisher and downstream delivery

This work does not redesign downstream signal-producer behavior:

- Telegram delivery remains
- existing execution adapter / bot-inbox path remains as currently implemented
- publisher continues to own the alpha ledger and delivery attempts
- Telegram delivery must gain a pre-send claim (`claimed` -> `sent`/`failed`)
  with lease recovery so concurrent publishers cannot double-send
- publisher rendering may add a compact Context section from already-persisted
  available evidence and must omit unavailable items rather than inventing
  neutral readings
- research-analyst remains a signal producer; bots execute under their own
  protocols outside this redesign's scope

## Locked Internal Decisions

### Runtime topology

PM2 runs:

1. `orchestrator` — sole writer of the market database
2. `signal-publisher` — sole writer of the alpha ledger

Binance OI rotation is united under the orchestrator:

- remove `binance-oi-rotation-worker` from PM2
- preserve external behavior: hourly completed-interval scan and atomic bot
  discovery feed file
- bots continue consuming the feed file
- research continues consuming discovery/watchlist state from the market DB

### Database ownership

Split schema initialization:

- `init_market_db()` for orchestrator-owned raw, discovery, feature, request-log,
  and cutoff-run tables
- `init_alpha_db()` for publisher-owned event, delivery, candidate, outcome, and
  research-ledger tables
- never create market tables inside `ANALYST_DB_PATH`

Outcomes:

- publisher stops evaluating outcomes against empty local `futures_data`
- a dedicated market-aware outcome evaluator reads market bars read-only and
  writes outcomes into the alpha ledger through an explicit single-owner handoff

### Storage migration

Additive v2, then drop legacy:

1. Introduce append-only `source_observations` with:

   ```text
   source, venue, native_symbol, asset, market_kind, interval,
   source_start, source_end, retrieved_at, observation_id, payload_json
   ```

2. One-shot migrate existing `futures_data` into v2 as:

   ```text
   source=coinalyze
   venue=aggregate_perp
   interval=15m
   retrieval_kind=legacy_import
   ```

3. Gap-fill missing coverage and newly selected symbols from the CoinAnalyze API.
   Do not discard the lake for a full scratch rebuild.

4. Direct cutover: plugins become the sole event producers immediately. No live
   dual-running legacy producers and no shadow parity gate.

5. After cutover and verification, stop writing `futures_data` and drop it.
   Reuse existing scorer/discovery logic as much as possible inside the new
   seams; do not preserve the old mutable-table design.

Normalized bars, versioned features, and cutoff snapshots are separate
projections over source observations. Corrections insert new observations;
nothing deletes prior facts. Deep backfill stops deleting history windows.

### Completed-cutoff runs

Each cycle materializes one durable cutoff run for the completed 15m UTC
boundary:

```text
running -> finalized | failed
```

Plugins may run only against a finalized cutoff snapshot ID. In-progress bars
are never eligible.

### Feature materialization

Derived features are versioned and scoped by market identity and cutoff:

- CoinAnalyze bars and TA features
- CoinAnalyze approximate candle-distributed VP
- CoinAnalyze 1h/4h FVG and order-block zones
- OpenMarket Bybit HTF profile and 15m flow/VP when available
- explicit unavailable states for missing enrichment

The feature layer is descriptive. It does not assign a trade thesis.

### Discovery and market identity

- expand discovery top-N to 15 per pool
- preserve append-only discovery snapshots and watchlist history
- every ranking snapshot must retain input observation IDs and declare any
  cross-source basis
- one market-identity mapper owns provider, venue, native symbol, canonical
  asset, quote, and market kind
- multiplier contracts keep native symbols; canonical asset mapping is explicit

## User Stories

1. As an operator, I want CoinAnalyze to remain the broad discovery and history
   backbone, so that OpenMarket Free cannot stop the core pipeline.
2. As an operator, I want OpenMarket enrichment limited to permanent benchmarks
   plus 30 ranked candidates on Bybit Perp, so that free-tier limits are
   respected.
3. As an operator, I want unavailable, stale, or rate-limited enrichment to be
   explicit, so that missing data is never treated as market evidence.
4. As an operator, I want HTF profiles refreshed every 4 hours across the
   selected universe, so that higher-timeframe value context is available.
5. As an operator, I want permanent assets to keep 15m buy/sell flow, so that
   benchmark event confirmation has continuous flow context.
6. As an operator, I want ranked candidates to request event-time OpenMarket
   checks only when a strategy is about to emit, so that budget is spent where
   it can affect a real signal.
7. As an operator, I want FVG and order-block zones materialised on 1h and 4h
   CoinAnalyze bars, so that structure context exists without extra OpenMarket
   spend.
8. As a strategy author, I want plugins to receive an immutable finalized
   cutoff snapshot, so that look-ahead and mutable shared state are impossible.
9. As a strategy author, I want to declare required and optional datasets, so
   that missing enrichment can skip or degrade without crashing the platform.
10. As an operator, I want `STRATEGY_ENABLED_IDS` restart control, so that I can
    enable or disable strategies without code deletion.
11. As an operator, I want existing strategies enabled after migration, so that
    current signal families continue after cutover.
12. As an operator, I want a plugin failure isolated, so that other strategies
    and delivery continue.
13. As a signal consumer, I want events to carry plugin version, snapshot ID,
    and source-evidence IDs, so that every thesis is replayable.
14. As a signal consumer, I want advisory confluence evidence attached without
    changing emission eligibility, so that research can measure its value later.
15. As an operator, I want Binance OI rotation united under the orchestrator but
    still publishing the bot feed file, so that bot discovery behavior remains
    while market-DB ownership is singular.
16. As an operator, I want legacy CoinAnalyze history imported into v2 rather
    than discarded, so that multi-week research depth is preserved.
17. As an operator, I want downstream Telegram and execution-adapter behavior
    left unchanged, so that this redesign does not disturb bot pickup protocols.
18. As a researcher, I want uncalibrated confidence labeled as such, so that
    heuristic scores are not mistaken for probabilities.

## Implementation Decisions

### Modules

Build or reshape these seams:

- market schema owner and alpha schema owner
- CoinAnalyze source adapter writing append-only observations
- OpenMarket source adapter with budget, deadline, and request log
- market-identity mapper
- cutoff-run coordinator inside the orchestrator
- feature materializers for bars, approximate VP, FVG/order blocks, and
  OpenMarket profile/flow
- strategy plugin registry and invocation loop
- three migrated strategy plugins wrapping existing scorers
- outcome evaluator with market read + alpha-ledger write handoff
- publisher claim-before-send fix and optional Context rendering from
  persisted evidence only

Do not redesign:

- bot execution protocols
- engine-side venue mapping, sizing, or order placement
- strategy research, parameter search, or confidence calibration

### Contracts

Plugin input:

- finalized cutoff ID
- immutable market snapshot for the plugin's declared scope
- required/optional feature availability map
- source provenance and evidence IDs

Plugin output:

- zero or more validated alpha-event drafts
- structured skip/failure reasons

Alpha event additions:

- `plugin_version`
- `input_snapshot_id`
- `source_evidence_ids`
- `confidence_status = "uncalibrated"`
- advisory evidence objects for profile, flow, FVG, and order blocks when
  available

### Configuration

Document at least:

```dotenv
STRATEGY_ENABLED_IDS=accumulation-base-v1,impulse-ignition-v1,continuation-breakout-balanced-v1
DISCOVERY_TOP_N=15
OPENMARKET_ENABLED=false
OPENMARKET_API_KEY=
OPENMARKET_REFERENCE_VENUE=BYBIT
OPENMARKET_PERMANENT_ASSETS=BTC,ETH,SOL,PAXG,XAUT
OPENMARKET_CANDIDATE_CAP=30
OPENMARKET_HTF_REFRESH_HOURS=4
OPENMARKET_REQUEST_DEADLINE_MS=1500
OPENMARKET_RETENTION_DAYS=30
FUTURES_RETENTION_DAYS=365
```

OpenMarket remains disabled until credentials are supplied. Unsupported Bybit
contracts are recorded as unavailable rather than failing the cycle.

### Migration sequence

1. Fix topology: remove separate Binance OI worker from PM2; orchestrator owns
   the scan and feed publication.
2. Split market/alpha schema initialization.
3. Add `source_observations`, cutoff runs, request log, and feature tables.
4. Import legacy `futures_data` and gap-fill.
5. Materialize v2 features, including FVG/order blocks and approximate VP label.
6. Implement plugin registry and migrate the three existing strategies.
7. Fix Telegram pre-send claim.
8. Direct cutover: plugins sole producers; stop legacy evaluator runtime paths.
9. Drop `futures_data` after verification.
10. Enable OpenMarket optionally and attach advisory evidence only.

## Testing Decisions

Good tests assert external contracts, not private call order.

Required contract tests:

- single market-DB writer topology
- market schema absent from alpha DB
- append-only source observations and legacy-import provenance
- backfill no longer deletes authoritative history
- completed-cutoff finalization before plugin invocation
- no in-progress or future bar in plugin snapshots
- plugin registry: unknown ID startup failure, disabled non-invocation,
  failure isolation, required-dataset skip reporting
- event identity includes plugin version and input snapshot ID
- enrichment unavailable never blocks CoinAnalyze emission
- OpenMarket budget skip/rate-limit statuses are durable and observable
- FVG/order-block creation, mitigation, fill, and invalidation on fixtures
- approximate VP is distinctly named and not interchangeable with native VP
- Telegram claim-before-send prevents concurrent double send
- publisher/adapter downstream path remains behavior-compatible
- outcome evaluation no longer depends on empty publisher-local market tables

Prior art: existing discovery, evaluator, outbox, publisher, topology, and
schema-migration tests. Prefer local SQLite fixtures and deterministic
timestamps; no live provider calls in unit/contract tests.

## Out of Scope

- strategy research, tuning, walk-forward optimization, or confidence
  calibration
- making enrichment evidence a hard gate or score booster
- paid OpenMarket tiers, historical raw-trade replay, monthly/quarterly
  composites
- storing raw live trades in phase one
- arbitrary third-party plugin installation
- redesign of bot execution protocols, venue routing, sizing, or order
  placement
- dual-running legacy producers after cutover
- full scratch rebuild of CoinAnalyze history

## Further Notes

- Free-tier OpenMarket is an enrichment experiment. Its limits define the first
  selected-universe and cadence policy.
- A missing enrichment feature is a data-quality state, not bullish, bearish, or
  neutral evidence.
- Direct cutover accepts live port risk. Mitigate with contract tests and a
  one-shot historical dry-run before deploy, not a parallel live shadow.
- The durable seams are source observations, cutoff snapshots, feature
  contracts, and the plugin registry. Strategy hypotheses can evolve later
  without redesigning those seams.
