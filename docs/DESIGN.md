# Research Analyst — Central Discovery & Strategy Evaluation (Design)

> Target architecture for the re-purposed `research-analyst` repo (formerly "Alpha
> Producer"). This document is the consolidated design reference. It is grounded in
> the actual code as scanned; sections that are *target-only* (not yet built) are
> marked **[TARGET]** and link to their spec in `specs/`.

---

## 1. Mission

A research-grade, venue-neutral **discovery + strategy-evaluation** engine for
crypto perpetuals. It continuously:

1. Maintains a market-data feed for a **static 97-symbol universe** (sourced from
    an approved tradeable-assets snapshot), with optional **rotated symbols** when enabled.
2. Evaluates **strategies as plugins** on **1m / 5m / 15m** bars, using **HTF
   (1h/4h) swing + FVG/OB** context resampled from the base feed.
3. Emits a **trade intent** per evaluation — a falsifiable directional thesis
   (entry, invalidation, targets, expiry) — delivered to **Discord as a signal**.
   A signal is **advisory only**: it is not an order and does not imply a fill on
   any venue.
4. Optionally runs an **LLM position-management sidecar** that advises the executor
   on open positions (hold / exit / reduce) — also emit-only, toggled on/off.

The engine **never holds exchange credentials and never places orders**.

---

## 2. System at a glance

```
                 ┌─────────────────────────────────────────────────────────┐
   public WS      │                 ws_gateway  [TARGET]                     │
   (Bybit on,     │  ConnectionPool → StreamRouter → IngestBuffer → SQLite   │
    Binance off)  │  ResampleWorker: 1m → 5m → 15m → 1h → 4h                │
                 └───────────────────────────┬─────────────────────────────┘
                                             │ source_observations (ws_bars)
                                             ▼
   static_universe.json (approved snapshot, 97)  ──►  discovery / universe selection
                                             │
                                             ▼
                 ┌─────────────────────────────────────────────────────────┐
                 │  orchestrator._run_pipeline (every INGEST_INTERVAL_MINS)  │
                 │   ingest → prune → regime eval → cutoff run →             │
                 │   materialize HTF zones (structure_zones) →               │
                 │   invoke_plugins_for_cutoff → outcome eval → health.json  │
                 └───────────────────────────┬─────────────────────────────┘
                                             │ alpha_outbox/*.json (trade intent)
                                             ▼
                 ┌─────────────────────────────────────────────────────────┐
                 │  signal_publisher.run_once (every 30s)                    │
                 │   validate → persist alpha_events → Discord/Telegram      │
                 │   (format_discord_signal)  ← SIGNAL, not an order         │
                 └─────────────────────────────────────────────────────────┘

   [TARGET] pm_sidecar (every 5m, if enabled)
         reads positions_feed (executor) + strategy direction + HTF/swings/RR/5m TA
         → emits pm_advice {hold|reduce|exit|near_tp} + one-liner → executor
```

---

## 3. Repository layout (module map)

| Module | Responsibility |
| --- | --- |
| `config.py` | Env-driven config, SQLite schemas (`init_db`), **static-universe loader** (`load_static_symbols`, `expand_perp_symbols`), WS toggles. |
| `orchestrator.py` | Main loop. `_run_pipeline()` runs ingest → scanner → prune → confluence alerts → regime evaluator → cutoff → feature materialization → plugins → outcome eval → `health.json`. |
| `scanner.py` + `two_pool_discovery.py` | Hourly two-pool discovery (`ignition`, `continuation`). Ranks eligible Binance perps by volume/OI/price action. Seeds `deep_backfill_jobs`. |
| `strategy_plugins.py` | Plugin **registry + invocation**. `StrategyPlugin` dataclass, `STRATEGY_ENABLED_IDS`, `invoke_plugins_for_cutoff()` (failure-isolated). |
| `strategy_v2_context.py` | Shared TA context: `load_preferred_15m_bars`, `resample_ohlcv`, `ema_last`, `atr_last`, `structure_bias_4h`, `zone_bias_4h`, `compute_htf_zones`, `compression_ok`, `zone_stack_and_ltf_scores`, `has_active_event`. |
| `structure_zones.py` | **HTF FVG + Order Block** detection (`detect_fvg`, `detect_order_blocks`, `compute_atr`) on resampled 1h/4h. Advisory zones. |
| `confluence_scoring.py` | `weighted_confluence`, `proximity_score`, `confidence_from_confluence` → uncalibrated `confidence`. |
| `analyze.py` | Broad TA profiling (VWAP/VA, EMA, HVN/LVN, RSI) used by confluence alerts. |
| `accumulation_evaluator.py`, `alpha_evaluator.py`, `regime_signal.py`, `regime_evaluator.py`, `outcome_evaluator.py` | Legacy + v2 evaluators and regime/outcome scoring. |
| `strategy_*.py` (`accumulation_base_v2`, `impulse_ignition_v2`, `continuation_breakout_v2`, `rsi_reclaim_v1`, `liquidity_sweep_reversal_v1`) | Concrete strategy plugins (each exposes `run_plugin`). |
| `alpha_outbox.py` | Append-only, atomic, deduplicated event writer. Enforces `data_purity` emit gate. |
| `signal_publisher.py` | Persists outbox → `alpha_events`, delivers to Telegram/Discord, retries (lease), routes research note. |
| `discord_transport.py`, `discord_format.py` | Discord webhook transport + markdown formatters (`format_discord_signal`). |
| `execution_adapter.py` | Disabled-by-default bot-inbox writer (bybit/bybit-test/mexc/propr). Never orders. |
| `ingest_coinalyze.py`, `ingest_deribit.py`, `ingest_venue_agg_failover.py` | **Current** REST ingestion + CA rate-limit shaping/failover. **[TARGET] replaced by `ws_gateway`.** |
| `binance_oi_rotation_scanner.py` + `binance_oi_rotation_worker.py` + `binance_oi_prune.py` | Existing **rotated-symbol** pattern (membership TTL ~36h, hard prune, static-membership skip). Analog for the rotation feed. |
| `llm_client.py` + `research_*.py` | Provider-neutral bounded LLM client + research coordinator (advisory only today). |
| `backfill.py`, `bootstrap_trend_history.py` | Warm 14-day history for newly selected symbols. |
| `specs/` | ADR-style specs, including `ws-ingestion.md` **[TARGET]** and `llm-position-sidecar.md` **[TARGET]**. |
| `symbols/static_universe.json` | **Persistent static universe** (approved CRYPTO snapshot, 97 bases). Version-controlled. |

---

## 4. Data model (SQLite tables)

Split by ownership to preserve single-writer discipline.

**Market data (`MARKET_DB_PATH`, gateway-owned):**
- `source_observations` — the canonical bar store: `asset, native_symbol, interval, source, source_end, payload_json`. Holds 1m/5m/15m (and HTF resampled) bars.
- `source_request_log` — ingestion rate-limit/freshness log (CA/OM circuits).
- `cutoff_runs` — `cutoff_id, cutoff_at, status (running|finalized)`, gates plugin runs.
- `feature_snapshots` — materialized features per cutoff (`fvg_ob_zones`, `coinalyze_candle_distributed_volume_profile_v1`, `openmarket_htf_profile`, …).
- `structure_zones` — HTF FVG/OB zones (`kind, direction, low, high, state, confidence_status`).
- `universe_snapshots`, `broad_discovery_snapshots`, `discovery_watchlist_history`, `deep_backfill_jobs` — point-in-time discovery + durable backfill.
- `regime_signals`, `confluence_alerts`, `scanner_history`, `brain_outputs`, `option_chains`, `alpha_candidates` — research/regime records.

**Analyst state (`ANALYST_DB_PATH`, orchestrator-owned):**
- `alpha_events` — authoritative persisted events (dedupe_key PK). `status ∈ active|expired|invalidated`.
- `alpha_event_status_history`, `alpha_confidence_observations`, `signal_deliveries` (per-channel attempt/retry), `execution_deliveries`, `research_requests/artifacts/evidence/run_metrics`, `pipeline_runs`.

**Rotation (`BINANCE_OI_DB_PATH`, dedicated worker-owned):** `binance_oi_rotation_*` tables — the existing rotated-symbol infrastructure (analog for the new rotation feed).

**[TARGET] new tables for the sidecar:**
- `positions_feed` — executor-written, read-only to PM: `position_id, symbol, side, entry, size, opened_at, strategy_id, current_pnl`.
- `pm_advice` — `advice_id, position_id, strategy_id, action(hold|reduce|exit|near_tp), confidence, reason, observed_at, htf_bias, rr`.

---

## 5. Evaluation pipeline (how a trade intent is born)

Driven by `orchestrator._run_pipeline()` → `strategy_plugins.invoke_plugins_for_intervals()` (per-interval; legacy single-cutoff `invoke_plugins_for_cutoff` retained for tests).

1. **Cutoff.** `completed_cycle_for(now, interval)` → the most recent completed boundary for each `EVAL_INTERVALS` member (1m/5m/15m); `_ensure_cutoff_run_finalized()` marks `cutoff_runs.status='finalized'`. Plugins require a finalized cutoff (bar-safety: only completed bars, `source_end < cutoff`).
2. **Feature materialization.** For the active universe, `structure_zones` computes FVG/OB on resampled 1h/4h; results written to `structure_zones` + `feature_snapshots`.
3. **Plugin invocation.** `load_enabled_plugins()` returns plugins whose id is in `STRATEGY_ENABLED_IDS`. Each `p.run(cutoff_id, snapshot)` is executed in a try/except — **failures are isolated** and reported per-plugin, never aborting the cycle.
4. **Event production.** Each plugin emits trade-intent dicts; `alpha_outbox.write_event()` stamps `alpha_id` (uuid5), `dedupe_key` (sha256 of `strategy_id|asset|direction|observed_at`), and enforces the `data_purity` gate (mixed strategies require `pure_ca`).
5. **Confidence.** `confluence_scoring.weighted_confluence` → `confidence ∈ [0,1]`, `confidence_status='uncalibrated'` (not a calibrated probability).

### Trade-intent event schema (schema_version 1)

Written by `alpha_outbox`, validated by `signal_publisher.validate_event`:

```
schema_version, alpha_id, strategy_id, asset, direction (long|short),
setup_class, phase, observed_at, valid_until, horizon_minutes, confidence,
entry_condition {type, price}, invalidation_price, targets[], feature_snapshot,
dedupe_key, (data_purity, price_source, plugin_version, input_snapshot_id)
```

This *is* the "outcome of strategy evals" — a portable, falsifiable directional
thesis. It is **not** an order and carries no sizing/venue.

### Executor-aligned trade-intent outbox (bybit-executor contract)

The advisory alpha event above feeds Discord/signal_publisher. To deliver to
`bybit-executor`, `alpha_outbox.write_event` also emits a `schema_version=1`
TradeIntent envelope via `intent_outbox` when `INTENT_DELIVERY_ENABLED=true`.
Contract: `bybit-executor/AGENTS.md` "Trade Intent Contract".

Mapping (internal α-event → executor intent):

| Executor field | Source |
| --- | --- |
| `delivery_id` | `alpha_id` (stable; executor journal dedupes) |
| `source` | `INTENT_SOURCE` (default `research-analyst`) |
| `exchange_id` | `INTENT_EXCHANGE_ID` (default `bybit`) |
| `account_id` | `INTENT_ACCOUNT_ID` (default `hyro`; compact strategies are forced here) |
| `asset` | `asset` |
| `symbol` | `to_ccxt_perp_symbol(asset)` → `BTC/USDT:USDT` |
| `direction` | `direction` upper (`long/bullish`→`LONG`, `short/bearish`→`SHORT`) |
| `entry_price` | `entry_condition.price` as the research reference price |
| `stop_loss` | `invalidation_price` |
| `take_profit` | `targets[0]` |
| `take_profit_mode` | `INTENT_TAKE_PROFIT_MODE` (default `fixed_full_close`) |
| `observed_at` | `observed_at` (ISO Z) |
| `entry_valid_until` | `valid_until` else `observed_at + INTENT_VALIDITY_MINUTES` |
| `metadata` | non-sizing metadata only — **sizing is executor-owned** (bybit-executor `runtime._amount` falls back to the account profile `risk.risk_amount`); the analyst never sends `quantity`/`risk_amount` |

Geometry is validated before delivery (`validate_geometry`): LONG ⇒
`stop_loss < entry_price < take_profit`, SHORT ⇒ `take_profit < entry_price <
stop_loss`; limit intents additionally require minimum `INTENT_MIN_RR` (2.0 by
default) and SL distance must meet `INTENT_MIN_STOP_DISTANCE_PCT` (0.1%);
structural HTF-zone admission must also pass with a 0.5-3.0 ATR buffer. Invalid events are skipped (the advisory event
still emits). The intent envelope is written atomically to `INTENT_INBOX` by
`delivery_id`, idempotent on replay.

The analyst does not emit `order_type`. Entry order policy is selected solely by
the receiving executor profile (`limit` with IOC by default, or an executor-
configured market policy). `entry_price` is the research reference price and,
when the executor selects limit, the submitted limit price.

Enable: `INTENT_DELIVERY_ENABLED=true` and point `INTENT_INBOX` at the
executor's `INTENT_INBOX` (e.g. `/home/ubuntu/bybit-executor/data/intents`).

The PM sidecar reads executor 1m snapshots and exports the executor's *PM Decision
Contract* (`POSITION_DECISION_DIR` files: `HOLD`/`REDUCE`/`EXIT`/`NEAR_TP`). A PM
`HOLD` is an ordinary no-op and cannot veto another decision. `REDUCE`, `EXIT`,
and `NEAR_TP` require the configured confidence threshold; none can override the
protective SL or fixed TP. The initial TP is supplied by the producer: an explicit
strategy target wins, otherwise the producer supplies a 2R target. The executor
remains strategy-dumb but safety-authoritative.

---

## 6. Static universe & rotation

- **Static (97 symbols).** `symbols/static_universe.json` is the persisted,
  git-tracked source, generated from an approved `propr_python.tradeable_assets`
  `CRYPTO` type. Canonical bases (e.g. `BTC`).
  - `config.load_static_symbols()` reads it (env override `STATIC_SYMBOLS`, or
    `STATIC_SYMBOLS_PATH`).
  - `config.expand_perp_symbols(base, venue)` → `BTCUSDT` perps for Bybit/Binance.
- **Rotation [TARGET].** `WS_SYMBOL_SOURCE ∈ {static, rotated, both}`. The existing
  `binance_oi_rotation_*` machinery (membership TTL ~36h, hard prune, static-membership
  skip, ADR-013) is the proven pattern to feed *rotated* symbols into the eval
  universe when enabled.
- **Capacity.** 97 symbols × (1m kline + 5m kline + markPrice) ≈ 291 streams — well within
  Bybit's sharded-pool and Binance's 1024-stream limits (see `specs/ws-ingestion.md`).

---

## 7. Market-data ingestion — current implementation

`ws_gateway` is the live market-data owner; CoinAnalyze and venue-aggregate
ingestion are not live defaults. `specs/ws-ingestion.md` documents the active
path:
- `WS_BYBIT_ENABLED=true` (default), `WS_BINANCE_ENABLED=false`.
- Stream **1m + 5m kline + markPrice**; **resample 15m/1h/4h locally from the 5m base** via
  `strategy_v2_context.resample_ohlcv`. Matches the "higher TF is resampled" rule while
  keeping 1m/5m available as direct eval feeds (per the 1m+5m correction).
- Seed a short warm window from REST, then maintain it via WS.
- Stamp `source`/`data_purity` so the existing emit gate and `_get_bar_purity`
  keep working unchanged.

---

## 8. Timeframe handling

- **Eval timeframes:** 1m, 5m, 15m — 1m/5m are streamed and 15m is locally resampled; plugins run on each
  via `invoke_plugins_for_intervals` (`config.EVAL_INTERVALS`). Each interval gets its own
  finalized `cutoff_runs` row and a snapshot carrying `eval_interval`.
- **HTF context:** 1h and 4h are **resampled** from the 5m base (never streamed), and feed
  plugins only as enrichment (zones/bias), not as standalone eval timeframes.
- Helpers in `strategy_v2_context`:
  - `resample_ohlcv(bars, every)` — generic group-by-dynamic resample.
  - `structure_bias_4h(bars_4h)` — `close vs EMA48_4h → long|short|missing`.
  - `zone_bias_4h(zones, ref_close, atr_4h)` — nearest active 4h FVG/OB → bias.
  - `resolve_bias(structure, zone)` — agree-or-abstain combiner.
  - `compute_htf_zones(bars_1h, bars_4h)` — runs FVG + OB detection on both.

---

## 9. HTF swing detector + FVG/OB

- **FVG/OB:** implemented in `structure_zones` (`detect_fvg`, `detect_order_blocks`,
  ATR-filtered, with naive mitigation/partial/fill/invalidate state tracking).
- **Swings are enrichment, not a detector.** Like FVG/OB, swing highs/lows are
  computed inside `structure_zones` (already derived via the prior `swing_lookback`
  window in `detect_order_blocks`) and exposed as **advisory swing levels** — scored
  through the same confluence machinery (`zone_stack_and_ltf_scores`, bias
  resolution) and surfaced in `feature_snapshot`/`structure_zones`. They never gate
  emission on their own; they enrich structure bias and feed PM-sidecar RR. No
  standalone swing module is required.
- Zones (and swing levels) are **advisory** (support/neutral/contradict); they
  never gate emission alone — only contribute to confluence score.

---

## 10. Strategy plugins (enable / disable, active / inactive)

- **Registry:** `strategy_plugins._REGISTRY` keyed by `strategy_id`; `StrategyPlugin`
  = `{id, version, required_datasets, optional_datasets, run}`.
- **Enable/disable:** `config.STRATEGY_ENABLED_IDS` (allowlist), plus
  `STRATEGY_ACTIVE_IDS` and `plugin_states`. Active plugins form the live admission
  set; legacy compact strategies retain their restricted assets while Dual-Zone
  strategies evaluate the static universe. Other registered plugins remain available
  for research.
- **Active/inactive [TARGET nuance]:** currently "enabled" = participates in the
  cutoff. Add a **runtime `active` flag** (per-plugin, toggleable without restart)
  distinct from the compiled `enabled` allowlist, so a strategy can be
  enabled-but-paused. Plumb via `STRATEGY_ACTIVE_IDS` or a `plugin_states` table.
- **Isolation:** every plugin runs in its own try/except; one plugin failing yields
  `{"failed": "..."}` for that id while others proceed.
- **Datasets contract:** `required_datasets` gate emission (`bars_15m` always
  available; optional `fvg_1h/fvg_4h/vp` skip-if-missing with a reported reason).

---

## 11. Trade intent → Discord (the signal)

Delivery is owned by `signal_publisher.SignalPublisher.run_once()` (every 30s):

1. Reads `data/alpha_outbox/*.json`, `validate_event()` (schema + bounds).
2. Persists to `alpha_events` (`ON CONFLICT dedupe_key DO NOTHING`).
3. For each active, unexpired event, renders and sends per channel.
4. **Discord:** if `DISCORD_ALPHA_WEBHOOK_URL` set, `DiscordWebhookTransport` +
   `discord_format.format_discord_signal(event)` renders:

   ```
   **ALPHA · LONG · SOL**
   Continuation · `continuation-breakout-v2`
   Phase: `armed_flag_breakout` · Confidence: **67%** (uncalibrated)

   **Trigger:** breakout above @ `145.2`
   **Invalidation:** `142.7`
   **Targets:** `148.1`, `151.0`
   **Window:** 2026-08-28 10:15 → 2026-08-28 14:15 UTC
   Context: 4h FVG:... · 4h OB:... · approx VP:...
   ```

5. **Critical boundary:** this is a **signal**, not an order. The repo does not
   place trades, choose venues, or guarantee fills. The executor (downstream,
   separate) decides whether to act on the signal.

Telegram mirror (`TELEGRAM_*`) and the disabled `execution_adapter` inbox follow
the same advisory model.

---

## 12. LLM position-management sidecar [TARGET]

`specs/llm-position-sidecar.md`. Emit-only:

- Toggle `PM_SIDECAR_ENABLED=false` to disable (default on for this deployment).
- **Cadence:** every 5m.
- **Inputs (read-only):** `positions_feed` (executor-written) + active trade-intent
  + HTF bias + swings + RR + 5m TA.
- **Output:** `pm_advice` with exactly one of `{hold, reduce, exit, near_tp}` + a
  one-line reason. On LLM timeout/error → emit `hold` (do-no-harm).
- **Confidence:** `hold` needs no confidence; `reduce`, `exit`, and `near_tp`
  require the configured minimum confidence.
- **NEAR_TP:** executor-owned one-time reduction when the venue mark is within
  five ticks of immutable original TP, using current quantity and protection state.
- **Boundary preserved:** reads positions, writes only advice; no credentials, no
  order placement — same discipline as the existing execution adapter.

---

## 13. Retention / tiered prune

`orchestrator.prune_db()` currently deletes `option_chains`, `brain_outputs`, and
`source_observations` older than `FUTURES_RETENTION_DAYS` (365), with nightly
`VACUUM`. **WS makes growth continuous**, so extend to a **tiered** TTL:

| Data | Keep | Rationale |
| --- | --- | --- |
| Raw 1m bars | 7–14 days | builds 5m/15m + short-term microstructure |
| 5m / 15m (resampled) | 30–90 days | main eval horizon |
| HTF 1h / 4h + `structure_zones` | 180+ days / indefinite | swing/FVG/OB context |
| `positions_feed` / `pm_advice` | 30 days | audit only |

The emit-gate (`data_purity`) and `cutoff_runs` finalization must remain intact
through pruning.

---

## 14. Safety & boundaries (carry-over, non-negotiable)

- Single writer per DB (`MARKET_DB_PATH` gateway, `ANALYST_DB_PATH` orchestrator,
  `BINANCE_OI_DB_PATH` rotation worker). Never duplicate writers.
- Evaluators read **only** local warmed data; they never call external market APIs
  and never write raw market data.
- `confidence` is uncalibrated research output, not a trade probability.
- No exchange credentials, no order submission, anywhere in this repo.
- Discovery rank, TA confluence, HMM output, LLM commentary, and Discord signals
  are **not** trade instructions.

---

## 15. Config reference (key knobs)

| Env | Default | Purpose |
| --- | --- | --- |
| `STATIC_SYMBOLS_PATH` | `symbols/static_universe.json` | Persistent static universe. |
| `STATIC_SYMBOLS` | "" | Comma override of the universe. |
| `WS_SYMBOL_SOURCE` | `static` | `static`\|`rotated`\|`both`. |
| `WS_BYBIT_ENABLED` | `true` | Primary public WS source. |
| `WS_BINANCE_ENABLED` | `false` | Opt-in WS source. |
| `WS_STREAM_TIMEFRAMES` | `1m,5m` | Base streamed TFs (15m resampled from 5m). |
| `WS_MARKPRICE_ENABLED` | `true` | Stream markPrice for live state. |
| `STRATEGY_ENABLED_IDS` | (v1+v2 allowlist) | Compiled plugin allowlist. |
| `STRATEGY_ACTIVE_IDS` | (empty ⇒ all enabled active) | Runtime active/inactive allowlist; `plugin_states` overrides per-id. |
| `DISCORD_ALPHA_WEBHOOK_URL` | "" | Signal delivery channel. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | "" | Telegram mirror. |
| `LLM_RESEARCH_ENABLED` | `false` | Advisory research note (today). |
| `PM_SIDECAR_ENABLED` **[TARGET]** | `false` | LLM position-management sidecar. |
| `PM_CADENCE_MINUTES` **[TARGET]** | `5` | Sidecar decision tick. |
| `PM_DECISION_VALIDITY_MINUTES` | `5` | Decision expiry. |
| `PM_ACTION_CONFIDENCE` | `0.70` | Minimum confidence for action-bearing PM decisions. |
| `FUTURES_RETENTION_DAYS` | `365` | Base prune window (extend to tiered). |
| `INGEST_INTERVAL_MINS` | `15` | Orchestrator loop interval. |

---

## 16. Build order (recommended)

1. **Static universe** — ✅ done (`symbols/static_universe.json` + `config` loaders).
2. **WS ingestion** — `ws_gateway.py` (ConnectionPool/StreamRouter/IngestBuffer) +
   `ResampleWorker`; Backfill seed; purity stamping. (`specs/ws-ingestion.md`)
3. **Swing enrichment** — expose swing levels from `structure_zones` as advisory
   enrichment (scored like FVG/OB via confluence); feed into bias + PM RR. No
   standalone detector.
4. **1m/5m eval timeframes** ✅ — `completed_cycle_for` + `load_bars_for_interval` in
    `strategy_v2_context`; v2 plugins honor `snapshot["eval_interval"]`;
    `invoke_plugins_for_intervals` runs each `EVAL_INTERVALS` member with its own cutoff.
    HTF (1h/4h) stays resampled-from-5m enrichment only.
5. **Trade-intent → Discord** — confirm `format_discord_signal` carries the intent;
   (already wired; verify fields).
 6. **Active/inactive flag** ✅ — `STRATEGY_ACTIVE_IDS` (env allowlist; empty = all enabled) +
    `plugin_states` table (runtime override: `active`/`inactive`/`paused`). `ensure_plugin_states()`
    seeds defaults; `load_active_plugins()` filters `enabled AND effective_active`. **Legacy v1
     evaluators** (research-only plugins outside the active set)
     are **retired** — kept registered but defaulted to `inactive` so they no longer evaluate; they can
    be re-activated via the flag. Hard-deletion of the legacy code is a separate, optional step.
 7. **LLM PM sidecar** ✅ — `pm_sidecar.py` + `positions_feed`/`pm_advice` tables. Emit-only
    `hold|exit|reduce` + ≤120-char reason on a 5m-cutoff cadence; reads `positions_feed`
    + trade-intent + HTF bias (`structure_bias_4h`) + swings (`market_structure` pivots) +
    RR + 5m TA; falls back to `hold` on any LLM error/timeout. `PM_SIDECAR_ENABLED=true`.
    One advice per position per cutoff (deterministic `advice_id` dedupe). (`specs/llm-position-sidecar.md`)
 8. **Tiered prune** ✅ — `prune_db` now deletes `source_observations` per interval via
    `config.PRUNE_INTERVAL_DAYS` (1m=7d, 5m=30d, 15m=90d, 1h/4h=365d; `0` disables a tier).
    Uncovered intervals fall back to the legacy `futures_retention_days`.
 9. **Rotation feed** ✅ (disabled by default) — `rotation_feed.py` exports active
    `binance_oi_rotation_watchlist_history` members to `BINANCE_OI_ROTATION_FEED_PATH`;
    `ws_gateway.load_rotated_bases()` consumes it when `WS_SYMBOL_SOURCE=rotated|both`.
    Gated by `ROTATION_FEED_ENABLED=false`.

---

## 17. Open items

- **Count:** the approved CRYPTO snapshot currently yields **97** bases, not 150.
  Working set = 97 unless the universe is extended.
- **Swing levels** are enrichment inside `structure_zones` (scored like FVG/OB), not
  a standalone detector.
- **Binance WS** off by default; enable only after Bybit path is proven.
- **PM sidecar** requires the executor to publish `positions_feed`; define that
  contract with the approved universe owner.
