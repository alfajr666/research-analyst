# Alpha Producer Agent Guide

## Mission

This repository produces research-grade, venue-neutral crypto-perpetual trade theses. It complements a downstream trading engine; it does not replace the engine's discovery, risk, or execution systems.

```text
Producer: thesis -> validated event -> Telegram / configured bot inbox
Engine:                         event -> eligibility -> risk -> order -> position lifecycle
```

An alpha event must be a falsifiable directional thesis with an entry condition, invalidation, targets, and expiry. A ticker, discovery rank, indicator count, or LLM narrative alone is never alpha.

## Ownership Boundaries

The producer owns:

- Point-in-time discovery, watchlists, local market-data history, and research records.
- Canonical asset, direction, setup class, phase, entry condition, invalidation, targets, expiry, feature snapshot, and research confidence.
- Event validation, durable event/delivery ledgers, Telegram delivery, and disabled-by-default bot-inbox delivery.

The downstream engine owns:

- Venue/instrument mapping, current tradeability, liquidity, and executable costs.
- Duplicate and portfolio-conflict handling, sizing, leverage, order choice, retries, and position lifecycle.
- Any order placement. This repository never stores exchange credentials or submits orders.

## Runtime Topology

`ecosystem.config.js` defines two PM2 applications:

| Process | Owns | Cadence |
| --- | --- | --- |
| `orchestrator` | Ingestion, discovery, backfill, market-data retention, regime and strategy evaluation | 15-minute loop; discovery scans hourly |
| `signal-publisher` | Alpha-event persistence, optional research coordination, Telegram + optional Discord webhook, and configured execution-inbox delivery | Every 30 seconds |

The orchestrator is the only writer of `DB_PATH`, the raw-market database. The publisher is the only writer of `ALPHA_DB_PATH`, the alpha-event ledger. They must be distinct files. Do not run standalone evaluators or duplicate PM2/manual instances against these databases.

## Data and Discovery

`orchestrator.py` obtains CoinAnalyze 15-minute perpetual market data and runs the scanner using Binance public contract metadata plus CoinAnalyze hourly observations. It writes point-in-time universe and broad-discovery snapshots, including rejected contracts, to make later research reproducible.

The scanner ranks independent pools, each limited by `DISCOVERY_TOP_N`:

| Pool | Thesis | Character |
| --- | --- | --- |
| `ignition` | An expansion may begin soon | Quiet/compressed base, activity or positioning change, no fresh breakout |
| `continuation` | An existing move may continue | Directional movement with volume/OI participation, no exhausted expansion |

New selections are recorded in append-only watchlist history and queue durable `deep_backfill_jobs`. The scanner backfills 14 days of 15-minute OHLCV, OI, and funding before an asset can qualify. Watchlist residency is at least 24 hours; stale, ineligible, or no-longer-ranked assets expire.

Evaluators consume only local warmed data. They neither call CoinAnalyze nor write raw market data. `FREQTRADE_DATA_DIR` provides an optional historical Feather archive for research, not live data.

## Strategy and Bar Safety

The producer is medium frequency: it evaluates completed 15-minute candles for a 15-minute-to-hours horizon. Every evaluator uses the start of the current UTC 15-minute candle as its cutoff and must query strictly earlier bars. Forming candles and stale series are excluded.

Current strategy families remain separate hypotheses:

- `accumulation_base`: v1 legacy EMA99 pullback; **v2** (`accumulation-base-v2`) 1h compression + limit at 1h EMA99 with confluence score.
- `impulse_ignition`: v1 compressed pre-breakout; **v2** (`impulse-ignition-v2`) armed breakout of 1h base lid (not chase after breach).
- `continuation_breakout`: v1 balanced preset; **v2** (`continuation-breakout-v2`) 4h trend + 1h flag breakout (`armed_flag_breakout`).
- `continuation_pullback`: **`rsi-reclaim-v1`** — 4h bias + 1h EMA200 mild extension + 15m RSI pullback/turn + fast-EMA touch/reclaim (`confirmed_rsi_reclaim`). Opt-in via `STRATEGY_ENABLED_IDS` (not in default allowlist).

v2 plugins (and rsi-reclaim-v1) share the confluence scoring ADR (`confidence_status=uncalibrated`; LLM is post-emit booster only). Enable via `STRATEGY_ENABLED_IDS` (defaults include v1+v2 in parallel).

The HMM plus dual-VWAP evaluator provides macro context. Its historical results are not proof of alpha and must not be represented as calibrated or execution-authorizing.

## Event Contract

Evaluators atomically write version-1 JSON files to `data/alpha_outbox/`. Identity is deduplicated by `strategy_id`, `asset`, `direction`, and `observed_at`; `alpha_id` and `dedupe_key` are deterministic.

Required fields:

```text
schema_version, alpha_id, strategy_id, asset, direction, setup_class, phase,
observed_at, valid_until, horizon_minutes, confidence, entry_condition,
invalidation_price, targets, feature_snapshot, dedupe_key
```

`confidence` is a score-derived research value until it has been calibrated out of sample. Feature snapshots are immutable. Event statuses are `active`, `expired`, and `invalidated`.

The publisher validates and persists an event before attempting delivery. `alpha_events` is authoritative; `signal_deliveries` records per-channel attempts (`telegram`, `discord`) and bounded retry state. One channel failing must never delete, duplicate, or block the other. Optional Discord alpha delivery uses `DISCORD_ALPHA_WEBHOOK_URL`. Binance OI rotation posts 1h digests (and multi-hour digests every 6 completed hours) via `DISCORD_OI_WEBHOOK_URL` (falls back to the alpha webhook) after each completed scan.

## Optional Research and Bot Delivery

Local-evidence LLM research is disabled unless `LLM_RESEARCH_ENABLED=true`. It is bounded by report, retry, input/output, timeout, and monthly-budget settings. It may add an advisory Telegram note but cannot alter deterministic event fields or authorize execution.

The execution adapter is also disabled by default. When explicitly enabled, it writes one validated immutable item to each configured target inbox (`bybit`, `bybit-test`, `mexc`, or `propr`) and records receipts in `execution_deliveries`. It does not connect to exchanges, select a venue, or place an order. Only active, unexpired events with the supported `limit_at_ema_context` shape, a single target, valid geometry, and a target allowlist match can be forwarded.

## Research Discipline

Treat every setup class and version as an independent hypothesis. Preserve point-in-time candidates and feature snapshots separately from outcomes. Promotion requires walk-forward evaluation, asset-relative normalization, liquidity-tier and regime breakdowns, conservative fees/spread/slippage/funding, matched baselines, and out-of-sample confidence calibration.

Discovery rank, technical confluence, HMM output, LLM commentary, and a successfully delivered inbox item are not trade instructions.

## Operations

```bash
cp .env.example .env
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python config.py
pm2 start ecosystem.config.js
```

Set `COINANALYZE_API_KEY` for collection and `TELEGRAM_BOT_TOKEN` plus `TELEGRAM_CHAT_ID` for Telegram delivery. Use `./venv/bin/python orchestrator.py --once` for one pipeline cycle, `pm2 status` for process state, and `pm2 logs orchestrator` or `pm2 logs signal-publisher` for diagnosis.

When an expected event is absent, inspect the chain in this order: universe/discovery snapshot, watchlist history, `deep_backfill_jobs`, fresh completed `source_observations`, evaluator outbox file, `alpha_events`, `signal_deliveries`, and (if enabled) `execution_deliveries`.
