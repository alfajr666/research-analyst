# Alpha Producer

Alpha Producer is a research-grade, medium-frequency source of venue-neutral crypto-perpetual trade theses. It identifies and records a portable market thesis, then delivers it to Telegram and, only when explicitly enabled, target-bot inboxes. It does not place orders.

```text
Producer: market data -> discovery -> evaluation -> alpha event -> delivery
Engine:                                                    event -> risk -> execution
```

The producer owns candidate selection, direction, setup class, phase, entry condition, invalidation, targets, expiry, feature snapshot, and research confidence. A consuming engine owns venue mapping, live tradeability, liquidity and cost checks, portfolio conflicts, sizing, leverage, order selection, and position lifecycle.

## Architecture

```text
Binance 24h contract metadata + CoinAnalyze 15m/hourly market data
                              |
                              v
                    orchestrator (DB_PATH)
      snapshots -> discovery -> watchlists -> backfill -> evaluators
                              |
                              v
              data/alpha_outbox/*.json (atomic, deduplicated)
                              |
                              v
                 signal-publisher (ALPHA_DB_PATH)
                 /              |               \
                v               v                v
          alpha ledger     Telegram       enabled bot inboxes
```

### Live topology (Nautilus cutover)

**Production for NT keeps only the OI scanner.** Do not restart `orchestrator` / `signal-publisher` for Nautilus deploys.

| Process | Responsibility | Live |
| --- | --- | --- |
| `binance-oi-rotation-scanner` | BN OI qualify/rank, feed, membership TTL (~36h), hard prune, static membership skip | **Online** |
| `orchestrator` | Legacy full research pipeline | **Stopped** |
| `signal-publisher` | Legacy alpha delivery | **Stopped** |

Retention + static skip: [`specs/binance-oi-rotation-retention.md`](specs/binance-oi-rotation-retention.md). NT consumes via `data-oi` static-subtract ([ADR-013](../nautilus-trading-os/specs/ADR-013-oi-rotating-static-subtract-and-ttl.md)).

### Historical PM2 apps (research path)

| Process | Responsibility | Schedule |
| --- | --- | --- |
| `orchestrator` | Ingestion, universe snapshots, two-pool discovery, durable backfill, retention, regime and strategy evaluation | Every 15 minutes; discovery hourly |
| `signal-publisher` | Event validation/persistence, optional local-evidence research, Telegram and configured bot-inbox delivery | Every 30 seconds |

When those apps run, `orchestrator` is the sole writer of `DB_PATH` and `signal-publisher` of `ALPHA_DB_PATH`. Keep paths distinct; do not duplicate writers.

When CoinAnalyze is rate-limited (high 429s or stale 15m bars), non-critical CA calls are shaped and the system automatically fails over to Binance USDM + Bybit Linear (venue_agg_v1) for OHLCV + best-effort OI/funding. Core CA ohlcv continues for recovery detection. See specs/ca-limited-takeover.md, MARKET_FAILOVER_ENABLED, CA_SHAPE_ON_CIRCUIT. Preferred loader and purity stamps preserve strategy correctness.

## Discovery and Evaluation

Every eligible Binance USDT perpetual is retained in point-in-time universe and broad-discovery snapshots, including rejected contracts. Assets are classified as `core`, `emerging`, or `not_eligible`, so historical work can use the universe that existed at the time rather than today's survivors.

The hourly scanner independently ranks two capped pools:

| Pool | Finds | Excludes |
| --- | --- | --- |
| `ignition` | Quiet, compressed bases where activity or positioning may precede a move | Fresh breakouts, post-breakout pullbacks, high current movement/volume |
| `continuation` | Established directional movement with volume and OI participation | Illiquid and exhausted expansions |

New watchlist selections create a durable `deep_backfill_jobs` record. The scanner obtains 14 days of 15-minute OHLCV, OI, and funding before a candidate qualifies. Watchlist history is append-only; selection has a 24-hour minimum residency and then requires continued qualification.

Evaluators use local warmed DuckDB data only. They never call CoinAnalyze and never write raw market data. The optional `FREQTRADE_DATA_DIR` Feather archive is for historical research, not live input.

| Setup class | Hypothesis |
| --- | --- |
| `accumulation_base` | EMA99 pullback and completed-bar confirmation |
| `impulse_ignition` | Pre-breakout compression with supporting activity and positioning |
| `continuation_breakout` | Established move with re-acceleration evidence |

All decisions use completed 15-minute candles. Evaluators cut off at the current UTC 15-minute boundary, query only earlier bars, and reject stale data. The intended horizon is 15 minutes to hours, not sub-minute execution.

## Alpha Events and Delivery

Evaluators atomically write schema-versioned JSON events to `data/alpha_outbox/`. Each event is deduplicated by `strategy_id`, `asset`, `direction`, and `observed_at`.

```json
{
  "schema_version": 1,
  "alpha_id": "deterministic UUID",
  "strategy_id": "impulse-ignition-v1",
  "asset": "SOL",
  "direction": "long",
  "setup_class": "impulse_ignition",
  "phase": "armed_base",
  "observed_at": "2026-08-16T10:15:00Z",
  "valid_until": "2026-08-16T14:15:00Z",
  "horizon_minutes": 240,
  "confidence": 0.67,
  "entry_condition": {"type": "breakout_above", "price": 145.2},
  "invalidation_price": 142.7,
  "targets": [148.1, 151.0],
  "feature_snapshot": {}
}
```

`confidence` is a score-derived research estimate, not a calibrated production probability. `feature_snapshot` is immutable. Events are `active`, `expired`, or `invalidated`.

The publisher validates and persists each event before delivery. `alpha_events` is authoritative. `signal_deliveries` records Telegram attempts, responses/errors, retry times, and completion. Active events may be sent to Telegram; bounded retries do not duplicate the event.

## Optional Integrations

### Local-Evidence Research

LLM research is disabled by default. Set `LLM_RESEARCH_ENABLED=true` only after supplying a provider key, model, pricing, and a suitable monthly budget. The coordinator works from local event evidence and is bounded by timeout, retry, report, and input/output limits. Its Telegram note is advisory and cannot change deterministic event fields.

### Execution Inboxes

The adapter is disabled by default. It can write validated, immutable messages into target bot inboxes for `bybit`, `bybit-test`, `mexc`, or `propr`; it never connects to an exchange or submits an order.

Enable an individual target with its `EXECUTION_*_ENABLED` setting and use an asset allowlist where required. Only active, unexpired events with a supported `limit_at_ema_context` entry, exactly one target, valid stop/target geometry, and a supported asset are forwarded. Delivery state and bot receipts are retained in `execution_deliveries`.

## Setup

1. Create your local configuration.

```bash
cp .env.example .env
```

Set `COINANALYZE_API_KEY` for live market collection. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for Telegram delivery. Keep every execution integration disabled until its receiving bot and allowlist are ready.

2. Create a virtual environment and install dependencies.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

3. Initialize the schemas.

```bash
python config.py
```

`config.py` creates missing tables and applies normal column migrations. Do not delete a populated database to apply regular schema changes: doing so destroys raw data, discovery snapshots, candidates, outcomes, and delivery history.

4. Optionally bootstrap selected assets with deep history.

```bash
./venv/bin/python bootstrap_trend_history.py --days 14
```

Normal discovery queues this work automatically when an asset first enters a pool.

## Running

Start the supported PM2 topology:

```bash
pm2 start ecosystem.config.js
pm2 status
pm2 logs orchestrator
pm2 logs signal-publisher
```

Restart it after changing Python or PM2 configuration:

```bash
pm2 restart ecosystem.config.js
```

Run a one-off ingestion, discovery, and evaluation cycle with immediate event publishing:

```bash
./venv/bin/python orchestrator.py --once
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No ingestion or discovery | `COINANALYZE_API_KEY`, then `pm2 logs orchestrator` |
| Selected asset has no event | `discovery_watchlist_history`, `deep_backfill_jobs`, fresh completed `source_observations`, then evaluator outbox files |
| No Telegram signal | Credentials, `alpha_events`, `signal_deliveries`, then `pm2 logs signal-publisher` |
| No bot-inbox item | Target enablement/allowlist, event shape and expiry, then `execution_deliveries` |
| DuckDB lock error | Stop duplicate processes; retain one market-data writer and one separate publisher-ledger writer |
| No regime context | Retain enough 15-minute history for daily/hourly aggregation; the Feather archive does not populate live DuckDB |
| CA rate limited / stale bars | Shaping active (see logs for "CA limited"), failover to venue_agg_v1; check caLimited in health.json; core CA still fetched lightly |

## Research Constraints

Treat each setup class and strategy version as an independent hypothesis. Evaluate with point-in-time universes and immutable candidate records, walk-forward splits, asset-relative normalization, liquidity-tier/regime reporting, conservative fees/spread/slippage/funding, and matched baselines. The current HMM results, discovery ranks, confidence scores, LLM commentary, and delivery records are not evidence of alpha or execution authorization.
