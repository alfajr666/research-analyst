# Research Analyst

`research-analyst` is a research-grade market-data and strategy-evaluation service.
It produces advisory alpha events and optional, executor-owned trade intents. It does
not hold exchange credentials, size positions, or place orders.

```text
Bybit public WebSocket -> source_observations -> strategy evaluation
                                             |-> alpha outbox -> Discord
                                             `-> intent inbox -> bybit-executor
```

## Current Design

- **Live market data:** Bybit WebSocket, with `1m` and `5m` klines plus mark price.
- **Higher timeframes:** `15m`, `1h`, and `4h` are resampled locally from the `5m`
  feed; they are never fetched as live REST evaluation data.
- **Live Binance WebSocket:** disabled by default.
- **Live CoinAnalyze:** disabled by default and skipped by the orchestrator.
- **Database:** DuckDB at `DB_PATH`; the orchestrator owns database writes.
- **WebSocket writer:** `ws_gateway` owns writes to `source_observations`.
- **Universe:** 97 static bases from `symbols/static_universe.json`; optional OI
  rotation is controlled by `WS_SYMBOL_SOURCE` and `ROTATION_FEED_ENABLED`.
- **Notifications:** Discord webhook for advisory alpha events.
- **Execution:** JSON files delivered to `/home/ubuntu/bybit-executor/data/intents`.

## Repository Layout

```text
src/research_analyst/       Application code
  strategies/compact/       Four live compact strategy ports
  strategies/v2/            Research/plugin strategies
  api_clients/              External API clients
tests/                      Automated tests
docs/                       Design and reference documentation
specs/                      Decision/specification records
symbols/                    Versioned static universe
data/                       Local DuckDB, outboxes, and health output
research/                   Research notes
ecosystem.config.js         Retired PM2 declaration
```

The source directory is intentionally separate from project metadata. Tests add
`src/research_analyst` to their import path through `tests/conftest.py`.

## Active Strategies

The live compact set is restricted to `BTC`, `ETH`, `PAXG`, and `QQQ`.

| Strategy | Evaluation | Context |
| --- | --- | --- |
| `failed-break-v3` | 5m | 15m and 4h context |
| `bb-rsi-meanrev-v1` | 5m | local 5m indicators |
| `williams-fractal-scalp-v1` | 1m | local 1m indicators |
| `ema9-continuation-stochrsi-v1` | 1m trigger | 5m setup |

Strategies are registered by `STRATEGY_ENABLED_IDS` and live-selected by
`STRATEGY_ACTIVE_IDS`. The compact strategies are the local default. Other plugins
remain available for research and tests but are not part of the live default.

Every event must contain direction, entry condition, invalidation, target, expiry,
and feature context. A limit intent must have valid direction geometry, a minimum
`2.0R` reward/risk ratio, and stop distance between `0.1%` and `5%`. Events that do
not meet executor admission remain advisory-only.

## Outputs

### Advisory alpha

`alpha_outbox.py` writes atomic, deterministic JSON files to `data/alpha_outbox/`.
`signal_publisher.py` validates and persists them to the alpha ledger, then sends
Discord messages when `DISCORD_ALPHA_WEBHOOK_URL` is configured.

An alpha event is a signal, not an order. It does not guarantee venue availability,
fill, quantity, leverage, or execution.

### Executor intent

When `INTENT_DELIVERY_ENABLED=true`, accepted events are written atomically to the
executor inbox. Configure:

```dotenv
BYBIT_EXECUTOR_DIR=/home/ubuntu/bybit-executor
INTENT_DELIVERY_ENABLED=true
```

The default inbox is `${BYBIT_EXECUTOR_DIR}/data/intents`. The intent contains no
`quantity` or `risk_amount`; `bybit-executor` sizes from its account profile and owns
venue checks, risk, order placement, retries, and position lifecycle.

### PM sidecar

`pm_sidecar.py` is disabled by default. When enabled, it reads executor position
snapshots and emits `HOLD`, `REDUCE`, or `EXIT` decision files. It cannot override a
protective stop loss or fixed take profit.

## Setup

```bash
cp .env.example .env
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python src/research_analyst/config.py
```

Set `DISCORD_ALPHA_WEBHOOK_URL` for Discord delivery. Keep intent delivery disabled
until the executor's paper path has been verified.

## Run

Run from the repository root so relative `data/`, `logs/`, and `.env` paths resolve:

```bash
./venv/bin/python src/research_analyst/ws_gateway.py
./venv/bin/python src/research_analyst/orchestrator.py
```

Run one evaluation cycle:

```bash
./venv/bin/python src/research_analyst/orchestrator.py --once
```

Only one WebSocket gateway and one orchestrator should write to a given database.
The current `ecosystem.config.js` intentionally contains no active PM2 apps because
the former orchestrator/signal-publisher topology was retired in favor of the
external Nautilus runtime.

## Configuration Essentials

| Variable | Current default/purpose |
| --- | --- |
| `WS_BYBIT_ENABLED` | `true` |
| `WS_BINANCE_ENABLED` | `false` |
| `COINANALYZE_EVAL_ENABLED` | `false` |
| `MARKET_FAILOVER_ENABLED` | `false` |
| `WS_STREAM_TIMEFRAMES` | `1m,5m` |
| `WS_SYMBOL_SOURCE` | `static` |
| `EVAL_INTERVALS` | `1m,5m,15m` |
| `STRATEGY_ACTIVE_IDS` | four compact strategies in local `.env` |
| `INTENT_DELIVERY_ENABLED` | `false` |
| `INTENT_MIN_RR` | `2.0` |
| `PM_SIDECAR_ENABLED` | `false` |

The complete variable reference is `.env.example`. Never commit `.env`, API keys,
webhook URLs, database files, or executor credentials.

## Verification

```bash
./venv/bin/python -m pytest -q
```

The suite includes strategy contracts, ingestion, outbox idempotency, executor
handoff, PM decisions, and runtime ownership checks. Some legacy tests may describe
retired components; those failures must be distinguished from regressions before
deployment.

## Research Rules

Treat each strategy and plugin version as an independent hypothesis. Use completed
bars only, point-in-time universes, immutable candidate records, walk-forward splits,
asset-relative normalization, and conservative fee/spread/slippage assumptions.
Discovery rank, confidence, LLM commentary, and delivery status are not evidence of
alpha or execution authorization.
