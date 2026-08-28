# CoinAnalyze-Truth Market Bars with Binance+Bybit Venue-Aggregate Failover

## Status

Build-ready implementation specification. Locked by operator grilling on
2026-08-18 and deep repo audit the same day. This document is the single source
of truth for market-bar freshness failover.

Implementation must follow this document. Do not expand scope into OpenMarket
backbone replacement, discovery ranking, OI-rotation product changes, or plugin
scoring redesign.

## Implementation Progress

- [x] Grill lock (normalize=A, prefer-CA, A+B trigger, core+hot-cap, partial OK,
      OHLCV+best-effort OI/funding, price-structure emit only, feature purity
      rules, 2h catch-up, liquidity-weighted blend, single blended row, OM
      independent, event purity stamps, asset-level readers, usable-CA exception)
- [x] Deep repo audit (wire map + do-not-touch list)
- [x] specs/ca-truth-venue-agg-failover.md
- [ ] config knobs + `.env.example`
- [ ] `api_clients/binance_futures.py` (15m klines / OI / funding as needed)
- [ ] `api_clients/bybit_linear.py` (15m klines / OI / funding)
- [ ] `ingest_venue_agg_failover.py` (or equivalent module) + orchestrator call site
- [ ] Shared prefer-CA bar loader (`strategy_v2_context` + shared helper)
- [ ] Wire loaders: alpha_evaluator, outcome_evaluator, analyze, regime_signal
- [ ] Orchestrator feature-mat + health prefer-merge
- [ ] `data_purity` stamp + `alpha_outbox.write_event` emit gate
- [ ] Publisher / Discord context purity line
- [ ] Unit + integration tests
- [ ] Live verification under CA 429 pressure

## Problem Statement

The 15-minute research pipeline treats CoinAnalyze aggregate perps
(`*USDT_PERP.A`, `venue=aggregate_perp`) as the market backbone. Under sustained
CoinAnalyze rate limits (observed ~80% 429s in production windows), live ingest
inserts zero rows, `max(source_end)` stalls, health reports multi-bar staleness,
feature materialization and plugins evaluate on incomplete series, and mixed
strategies either go silent or would score on hollow data.

OpenMarket Free cannot backfill the full backbone (weight budget, enrichment
role). A single venue (Binance-only or Bybit-only) is a weak proxy for CA
aggregate. The operator requires:

1. **CoinAnalyze remains truth** whenever a usable CA bar exists.
2. **Automatic freshness continuity** via Binance USDM + Bybit linear synthetic
   aggregate when CA bars are missing or unusable.
3. **Honest provenance** — never claim synthetic bars are pure CA.
4. **CA-shaped convention** for schema, bar clock, and field names only
   (identity normalization), not value impersonation.
5. **Minimal wire surface** — do not disturb OI rotation, OM enrichment,
   delivery, discovery ranking, or plugin math internals.

## Solution

```text
CA ingest (primary)
    |
    v
gap + circuit detect
    |
    +--> missing/unusable completed 15m bars
    |         |
    |         v
    |    Binance + Bybit public APIs
    |         |
    |         v
    |    blend or partial row
    |    source=venue_agg_v1  (never coinalyze)
    |
    v
source_observations (append-only, multi-source)
    |
    v
prefer_bars(asset, t): usable CA else venue_agg_v1
    |
    +--> zones / health / plugins / outcomes
    |
    v
emit gate: price_structure may fire on synthetic;
           mixed/positioning require pure_ca
```

When CA recovers, new usable CA bars win automatically. No sticky “failover mode”
flag is required for correctness (circuit is only a budget accelerator).

## Locked Product Decisions

### 1. Normalization (CA convention)

**Identity / shape only (grill A):**

| Layer | Rule |
| --- | --- |
| Bar clock | Completed UTC 15m bars; `source_start`/`source_end` same convention as CA writers |
| Payload field names | Same CA keys: `open,high,low,close,volume,open_interest,funding_rate,predicted_funding,liquidation_long,liquidation_short,long_short_ratio` |
| Values | Honestly derived from BN/BY; never scaled or faked to match CA history |
| `source` | Failover rows **must not** use `coinalyze` |
| `native_symbol` | Venue-honest synthetic id (not `_PERP.A`); see Data Model |
| CA symbol on events | Alpha events may still carry CA-form `source_symbol` for identity; bar loads resolve via **asset** |

### 2. Prefer-CA read rule

For each `(asset, interval, source_end)`:

1. If a **usable** CA observation exists → use it (`data_purity=pure_ca`).
2. Else if a `venue_agg_v1` observation exists → use it (purity from provenance).
3. Else → missing.

**Usable CA OHLCV** (grill Q18):

- `close > 0`
- `volume` present and finite (allow `0` only if explicitly documented as real;
  default require `volume >= 0` and `close > 0`; reject all-null OHLC)

Unusable CA rows do **not** block failover for that bar (treated as missing).
Do **not** override usable CA because it “disagrees” with Binance.

Never average CA and synthetic on the same `source_end`.

### 3. When failover runs

| Mode | Behavior |
| --- | --- |
| **Per-bar gap (always)** | After CA ingest each cycle: for in-scope assets, fill missing/unusable completed 15m bars |
| **CA-sick circuit** | If freshness **or** 429-rate trips → expand universe to hot set (capped) and prioritize failover work that cycle |
| **Not always dual-write** | Do not fetch BN/BY every cycle for all assets when CA is healthy and bars are present |

Circuit trip (OR), with hysteresis clear:

| Signal | Default trip | Default clear |
| --- | --- | --- |
| Core freshness | preferred core `max(source_end)` age > **30 minutes** | age ≤ **20 minutes** |
| CA 429 rate | `429/(429+ok)` over last **30 minutes** ≥ **0.50** | rate ≤ **0.25** for **30 minutes** |

Config must expose these thresholds. Exact numbers may be tuned without changing
the OR + hysteresis shape.

### 4. Universe

| Tier | Membership | When |
| --- | --- | --- |
| **Core** | `OPENMARKET_PERMANENT_ASSETS` (default BTC,ETH,SOL,PAXG,XAUT) | Always eligible for gap-fill |
| **Hot expansion** | Scanner hot set: rankings + accumulation alerts + fresh OI-rotation candidates mapped to assets (not full 200+ liquid universe) | Only while circuit open |
| **Cap** | `FAILOVER_WATCHLIST_CAP` default **20** after core | `min(len(hot), cap)` |

Discovery ranking itself must not be rewritten to depend on synthetic CA
positioning fields.

### 5. Catch-up depth

Fill all missing completed 15m bars in the last **`FAILOVER_CATCHUP_HOURS=2`**
per asset, subject to a per-cycle request budget
(`FAILOVER_MAX_REQUESTS_PER_CYCLE`). Do not deep-repair arbitrary multi-day holes
in v1 (see Non-Goals).

### 6. Partial venues

- Both Binance and Bybit succeed → synthetic aggregate (`data_purity=synthetic_agg`).
- Exactly one succeeds → write bar with `partial=true`, `data_purity=single_venue`.
- Neither → no row.

### 7. Field policy

| Field | Failover policy |
| --- | --- |
| OHLCV | Required for a written bar |
| `open_interest` | Best-effort USD sum when available; else null + field tag unavailable |
| `funding_rate` | Best-effort OI-weighted mean when available; else null |
| `predicted_funding` | Usually unavailable unless both (or documented one) provide it |
| `long_short_ratio` | **unavailable** (no fake 1.0 / 0.0) |
| `liquidation_long/short` | **unavailable** unless real venue series implemented (v1: unavailable) |

Never fill missing positioning fields with zeros that look like real neutrals.

### 8. Blend formula (both venues present)

Units must be normalized before blend (base volume vs quote; OI coin vs USD).
Document chosen units in provenance.

| Field | Rule |
| --- | --- |
| `open`, `close` | Volume-weighted average across venues |
| `high` | `max(venue highs)` |
| `low` | `min(venue lows)` |
| `volume` | Sum (single chosen unit) |
| `open_interest` | Sum USD notional |
| `funding_rate` | OI-weighted average |

Single-venue partial: copy that venue’s normalized fields; tag component list
length 1.

### 9. Strategy emit policy

| Class | strategy_id | On non-`pure_ca` signal bar / window |
| --- | --- | --- |
| **price_structure** | `accumulation-base-v1`, `accumulation-base-v2`, `rsi-reclaim-v1` | **May emit**; stamp purity |
| **mixed** | `impulse-ignition-v1`, `impulse-ignition-v2`, `continuation-breakout-balanced-v1`, `continuation-breakout-v2` | **Must not emit** (fail closed) |

**Feature windows:**

- Raw price series (EMA/RSI/structure): may span mixed sources after prefer-merge.
- Positioning distributional features (OI z/percentile, funding z, LS): if any bar
  in the computation window is not `pure_ca` → feature = `unavailable` / strategy
  blocked for mixed class.

`COALESCE(oi/funding, 0)` in loaders must not invent neutrals for purity-gated
logic. Prefer nulls + explicit unavailable.

### 10. OpenMarket

**Independent.** OM remains optional enrichment (HTF profile / 15m flow). Failover
backbone uses **public Binance + Bybit APIs only**. Do not spend OM weight on
backbone bars.

### 11. Storage shape

**Single blended (or partial) row per bar** — no leg rows in v1. Component venues
live only in `payload.provenance`.

### 12. Automatic return to CA

No manual mode switch. Every cycle: CA ingest first; prefer-CA on read; circuit
clears on hysteresis. Operators may disable failover via config only.

## Data Model

### `source_observations` row (failover)

| Column | Value |
| --- | --- |
| `observation_id` | `venue_agg_v1:{asset}:{source_end.isoformat()}` (stable, not `coinalyze:`) |
| `source` | `venue_agg_v1` |
| `venue` | `binance_bybit` |
| `native_symbol` | Synthetic, e.g. `{ASSET}-USDT-PERP-VAGG` (honest; not `_PERP.A`) |
| `asset` | Canonical asset (`BTC`, …) |
| `market_kind` | `perpetual` |
| `interval` | `15m` |
| `source_start` / `source_end` | Completed bar bounds (same clock as CA) |
| `retrieved_at` | Wall clock UTC |
| `retrieval_kind` | `failover` |
| `payload_json` | CA field names + `provenance` object |

### Provenance object (required)

```json
{
  "kind": "synthetic_aggregate",
  "pure_ca": false,
  "data_purity": "synthetic_agg",
  "aggregator": "binance_bybit_v1",
  "partial": false,
  "reason": "ca_missing_bar",
  "components": [
    {
      "venue": "binance_usdm",
      "symbol": "BTCUSDT",
      "weight_vol": 0.55,
      "weight_oi": 0.62
    },
    {
      "venue": "bybit_linear",
      "symbol": "BTCUSDT",
      "weight_vol": 0.45,
      "weight_oi": 0.38
    }
  ],
  "fields": {
    "ohlcv": "blended",
    "open_interest": "sum_usd",
    "funding_rate": "oi_weighted",
    "predicted_funding": "unavailable",
    "long_short_ratio": "unavailable",
    "liquidation_long": "unavailable",
    "liquidation_short": "unavailable"
  },
  "units": {
    "volume": "base_asset",
    "open_interest": "usd_notional"
  }
}
```

`data_purity` enum:

- `pure_ca` — usable CoinAnalyze bar
- `synthetic_agg` — BN+BY blend
- `single_venue` — partial one-leg failover
- `mixed_window` — optional stamp when a feature window spans purities (emit path)

### Alpha event / feature_snapshot stamps (required when non-CA used)

```text
data_purity: pure_ca | synthetic_agg | single_venue | mixed_window
price_source: coinalyze | venue_agg_v1
fallback_reason: ca_missing_bar | ca_unusable_ohlcv | ca_circuit  (omit if pure)
```

Publisher/Discord Context should show a short purity line derived from these
fields (e.g. `data: synthetic (BN+BY)`).

## Architecture and Wire Map

### Ownership

| Layer | Owner process | Notes |
| --- | --- | --- |
| CA + failover writes | `orchestrator` only | Single market-DB writer invariant unchanged |
| Prefer-merge reads | plugins / evaluators via shared helper | No second writer |
| Emit gate | `alpha_outbox.write_event` + strategy classification | Defense in depth |
| Delivery | `signal_publisher` | Display purity only; no market fetch |

### Cycle insertion point

In `orchestrator` pipeline, **immediately after** `ingest_coinalyze()` and
**before** prune is optional; **must be before** confluence / regime / feature
mat / plugins:

```text
ingest_coinalyze()
ingest_venue_agg_failover()   # NEW
prune_db / alerts / regime / cutoff / features / plugins / ...
```

### Modules (implementation targets)

| Module | Action |
| --- | --- |
| `config.py` + `.env.example` | Knobs (below) |
| `api_clients/binance_futures.py` | **New** thin client; log to `source_request_log` source=`binance_usdm` |
| `api_clients/bybit_linear.py` | **New** thin client; source=`bybit_linear` |
| `ingest_venue_agg_failover.py` | **New** gap detect, circuit, blend, insert |
| `strategy_v2_context.py` | Prefer-merge in `load_15m_bars`, `load_btc_15m`; purity metadata |
| Shared helper (e.g. in `strategy_v2_context` or small `market_bars.py`) | One SQL/merge definition reused by alpha/outcome/analyze/regime |
| `alpha_evaluator.py` | Use shared loader (no raw unmerged SQL) |
| `outcome_evaluator.py` | Resolve bars by **asset** from event; prefer-merge |
| `analyze.py` | Asset loaders must prefer-merge (prevent double volume) |
| `regime_signal.py` | Same |
| `orchestrator.py` | Call failover; feature-mat + health use preferred bars |
| `alpha_outbox.py` | Emit gate by strategy class + purity |
| `strategy_plugins.py` / evaluate paths | Stamp snapshot purity before `write_event` |
| `signal_publisher.py` / `discord_format.py` | Context purity line |

### Reuse (read-only reference)

- `binance_oi_rotation_scanner.BinanceClient` klines/OI hist patterns — **extract or
  duplicate into api_clients**; do **not** write `BINANCE_OI_DB_PATH` from failover.
- `coinalyze_symbol_from_binance` / scanner symbol maps for asset↔venue symbol.

### Do not touch (explicit)

- `binance_oi_rotation_worker.py`, OI Discord notify, OI DB schema (except shared
  URL constants already in config)
- OpenMarket client behavior beyond remaining independent
- `scanner.py` ranking / two_pool discovery logic
- `backfill.py`, `ingest_deribit.py`
- `execution_adapter.py`, telegram core, LLM research agent stack
- Plugin scoring formulas inside `*_v2.py` / `rsi_reclaim_v1.py` (gate + loader only)
- Always-on dual PM2 market writers

## Prefer-Merge Reader Contract

### Canonical API (normative)

```text
load_preferred_15m_bars(conn, *, asset=None, native_symbol=None, cutoff, lookback_days=...)
  -> bars ordered by source_end ASC, one row per source_end
  -> each row includes data_purity / source / native_symbol used
```

Resolution:

1. If `native_symbol` is CA-form, map to `asset` then merge by asset.
2. SQL (conceptual): all observations for asset+interval with `source_end < cutoff`
   in lookback; rank per `source_end` by priority
   `coinalyze_usable=0`, `venue_agg_v1=1`; keep rank 1.
3. `load_btc_15m` uses the same helper with `asset='BTC'`.

### Call sites that must use it (v1)

- `strategy_v2_context.load_15m_bars` / `load_btc_15m`
- `alpha_evaluator` frame builder
- `outcome_evaluator` market path
- `analyze.py` futures metric loaders used on the live path
- `regime_signal` bar aggregations used on the live path
- Orchestrator zone materialization query (drop hard `source='coinalyze'` or
  replace with prefer view)

### Optional later

- `research_tools.py`, `accumulation_detection.py` — should migrate to the same
  helper when touched; not blocking if those paths are cold.

## Health and Observability

### Health

`data/health.json` and orchestrator health print must report:

- `dataLatestAt` / age from **preferred** core bars (not CA-only filter)
- Optional split: `caLatestAt`, `failoverLatestAt`, `failoverBarsLast2h`
- Existing CA/OM request status counts retained
- Add BN/BY request status counts from `source_request_log`

### Logging

Every Binance/Bybit HTTP call goes through `RateLimitedClient` (or equivalent)
into `source_request_log` with `source` in `{binance_usdm, bybit_linear}`.

Failover module logs summary per cycle:

```text
Failover: circuit=open|closed gaps_filled=N partial=P skipped_budget=S assets=...
```

## Configuration

| Env | Default | Meaning |
| --- | --- | --- |
| `MARKET_FAILOVER_ENABLED` | `false` | Master switch (ship dark, enable in prod when tests green) |
| `FAILOVER_SOURCE_NAME` | `venue_agg_v1` | `source` column value |
| `FAILOVER_CATCHUP_HOURS` | `2` | Missing-bar lookback |
| `FAILOVER_WATCHLIST_CAP` | `20` | Hot expansion cap beyond core |
| `FAILOVER_MAX_REQUESTS_PER_CYCLE` | `80` | Hard budget guard |
| `FAILOVER_CIRCUIT_AGE_MIN` | `30` | Trip on core preferred age (minutes) |
| `FAILOVER_CIRCUIT_CLEAR_AGE_MIN` | `20` | Clear freshness |
| `FAILOVER_CIRCUIT_429_RATE` | `0.50` | Trip threshold |
| `FAILOVER_CIRCUIT_CLEAR_429_RATE` | `0.25` | Clear threshold |
| `FAILOVER_CIRCUIT_WINDOW_MIN` | `30` | 429 window |
| `BINANCE_FUTURES_BASE_URL` | existing | Reuse |
| `BYBIT_LINEAR_BASE_URL` | `https://api.bybit.com` | New |
| Core assets | `OPENMARKET_PERMANENT_ASSETS` | Reuse; do not fork list in v1 |

RPS for BN/BY clients: conservative defaults; header-aware backoff; never block
the orchestrator past a short deadline (skip remaining gaps if budget/time
exhausted).

## Strategy Classification Registry (normative)

```text
PRICE_STRUCTURE_STRATEGY_IDS = {
  accumulation-base-v1,
  accumulation-base-v2,
  rsi-reclaim-v1,
}

MIXED_STRATEGY_IDS = {
  impulse-ignition-v1,
  impulse-ignition-v2,
  continuation-breakout-balanced-v1,
  continuation-breakout-v2,
}
```

`write_event` behavior:

- If `strategy_id` in PRICE_STRUCTURE → allow any purity; require stamp when not pure.
- If `strategy_id` in MIXED → require `data_purity == pure_ca` (signal bar); else
  refuse write (log skip).
- Unknown strategy_id → fail closed (require pure_ca) until classified.

## Acceptance Criteria

1. With CA healthy and usable bars present, **zero** `venue_agg_v1` rows are
   required for core assets; prefer-merge returns only CA.
2. When CA returns 429/empty for core OHLCV and failover enabled, within one to
   two cycles core preferred `source_end` advances on completed 15m boundaries
   via `venue_agg_v1` rows with valid provenance.
3. Prefer-merge never returns two rows for the same `(asset, source_end)`.
4. Asset-level volume aggregates (regime/analyze) do not double-count when both
   sources exist historically for different bars.
5. Mixed strategies produce **no** outbox events whose signal bar purity is
   non-`pure_ca`.
6. Price-structure strategies may emit on synthetic bars and include
   `data_purity` / `price_source` on the event.
7. Health age reflects preferred bars (failover success is visible).
8. Feature materialization zones can build from preferred OHLC (not CA-only hard
   filter).
9. OM request volume does not increase due to failover (independent).
10. OI rotation DB and feed behavior unchanged.
11. Disabling `MARKET_FAILOVER_ENABLED` restores pre-change write behavior
    (CA-only inserts).
12. Unit tests cover: usable-CA preference, unusable-CA gap, partial single venue,
    blend math smoke, emit gate matrix, circuit trip/clear hysteresis (mocked
    clocks/counters).

## Test Plan

| Test | Assert |
| --- | --- |
| `test_prefer_bars_ca_wins` | Usable CA + synthetic same bar → CA only |
| `test_prefer_bars_unusable_ca` | close=0 CA + synthetic → synthetic |
| `test_failover_insert_provenance` | payload has provenance, source≠coinalyze, id prefix |
| `test_partial_single_venue` | one leg → partial + single_venue |
| `test_blend_ohlc_oi_funding` | deterministic weights on fixture legs |
| `test_emit_gate_mixed_blocked` | ign/cont v1/v2 cannot write_event on synthetic |
| `test_emit_gate_price_structure_allowed` | acc/rsi may write with stamp |
| `test_circuit_hysteresis` | trip/clear thresholds |
| `test_catchup_window_budget` | respects 2h and max requests |
| `test_health_preferred_latest` | health uses merge not CA-only |
| `test_no_double_volume_regime_path` | fixture dual-source history |

Use mocked HTTP for BN/BY; temp DuckDB like existing tests.

## Rollout

1. Land code with `MARKET_FAILOVER_ENABLED=false`.
2. Run unit/integration tests green.
3. Enable in prod; watch `source_request_log` for binance_usdm/bybit_linear and
   failover summary logs.
4. Confirm health age recovers during CA 429 spikes without mixed false emits.
5. Tune RPS/budgets/circuit thresholds from live data.

## Non-Goals (v1)

- OpenMarket as backbone or third blend leg
- Deep multi-day history repair / bootstrap via BN+BY
- Rewriting discovery pools on synthetic positioning
- Storing per-venue leg rows
- Claiming bit-identical parity with CA `.A` aggregates
- Changing alpha event schema_version or execution adapter contracts beyond
  additive feature_snapshot fields
- Fixing CA RPS as a substitute for this work (complementary; separate change)

## Relationship to Other Specs

| Spec | Relationship |
| --- | --- |
| `data-platform-strategy-plugins.md` | CA backbone + OM enrichment unchanged; this adds a **tertiary freshness writer** with prefer-merge |
| `external-api-rate-limiting.md` | BN/BY clients must use the same client/logging patterns |
| `strategy-v2-shared-library.md` / v2 strategy specs | Load via prefer-merge; emit policy above |
| `binance-oi-rotation-*.md` | Orthogonal product; client code may be referenced only |
| `alpha-outcome-policy.md` | Outcomes read preferred OHLC by asset |

## Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Double-counting bars | Mandatory shared prefer-merge; ban new raw SQL on `source_observations` for live paths without merge |
| Fake neutral funding/OI | Null unavailable; mixed emit blocked; stop treating COALESCE 0 as real |
| Symbol map errors (1000PEPE etc.) | Reuse existing mappers; skip asset on map failure; log |
| BN/BY rate limits | Per-cycle budget, core-first, circuit cap |
| Outcome symbol mismatch | Events keep CA `source_symbol`; loader maps to asset |
| Scope creep into OI/OM | Explicit do-not-touch list |

## Open Tuning (not product forks)

These may change via env without a new grill:

- Exact circuit thresholds and catch-up hours
- `FAILOVER_MAX_REQUESTS_PER_CYCLE` and venue RPS
- Whether `volume == 0` with `close > 0` counts as usable CA (default: usable if
  close > 0)

Any change to prefer rules, purity emit matrix, blend identity, or OM
independence requires an explicit spec amendment.
