# Research Analyst Agent Guide

## Mission

This repository evaluates market hypotheses against completed market-data bars and
publishes two portable outputs:

1. **Advisory alpha events** to Discord.
2. **Executor trade intents** to the shared `bybit-executor` inbox when explicitly
   enabled.

It is not an exchange bot. It never stores exchange credentials, sizes positions, or
places orders.

## Source Of Truth

Application code lives under `src/research_analyst/`. Run commands from the project
root. The source directory is placed on `sys.path` by `tests/conftest.py`; production
entrypoints are explicit paths such as:

```bash
./venv/bin/python src/research_analyst/ws_gateway.py
./venv/bin/python src/research_analyst/orchestrator.py
```

Strategies live under `src/research_analyst/strategies/compact/` and
`src/research_analyst/strategies/v2/`. Tests live under `tests/`. Do not recreate
root-level module shims.

## Ownership

### Analyst owns

- Static/rotated universe selection and local market-data history
- Completed-bar cutoffs and feature snapshots
- Strategy plugin invocation and failure isolation
- Alpha-event validation, deduplication, persistence, and Discord delivery
- Optional executor intent file generation
- Optional PM sidecar decision generation

### bybit-executor owns

- Credentials, exchange connectivity, instrument mapping, and tradeability
- Position sizing and leverage
- Portfolio conflict handling and duplicate order protection
- Order placement, retries, fills, protective orders, and lifecycle

Never add `quantity` or `risk_amount` to an analyst intent.

## Runtime Topology

```text
Bybit WS (1m, 5m, mark price)
          |
          v
   ws_gateway -> source_observations
          |
          v
   orchestrator -> cutoff/features -> strategy plugins
          |                                  |
          v                                  v
   alpha_outbox -> Discord             intent_outbox -> executor inbox
```

- `ws_gateway` is the sole writer of live `source_observations`.
- `orchestrator` is the sole writer of the configured DuckDB database.
- `1h` and `4h` are resampled from completed `5m` bars.
- Bybit WS is the live source. Binance WS, CoinAnalyze evaluation, and REST failover
  are disabled by default.
- The PM sidecar reads executor snapshots and emits executor decision files; it is
  advisory and disabled by default.
- `ecosystem.config.js` has no active PM2 apps; the prior PM2 topology was retired.

## Live Strategies

The local live set is limited to `BTC`, `ETH`, `PAXG`, and `QQQ`:

| ID | Trigger | Context |
| --- | --- | --- |
| `failed-break-v3` | 5m | 15m/4h |
| `bb-rsi-meanrev-v1` | 5m | 5m |
| `williams-fractal-scalp-v1` | 1m | 1m |
| `ema9-continuation-stochrsi-v1` | 1m | 5m setup |

`STRATEGY_ENABLED_IDS` controls registration. `STRATEGY_ACTIVE_IDS` controls the
runtime-active subset. A plugin failure must not abort other plugins in the same
cutoff.

## Event And Intent Contracts

An alpha event contains `schema_version`, deterministic `alpha_id` and `dedupe_key`,
strategy identity, asset, direction, setup/phase, observation and expiry, entry
condition, invalidation, targets, confidence, feature snapshot, and data provenance.
Confidence is uncalibrated and must not be described as probability.

Before executor delivery, validate:

- LONG: `stop_loss < entry_price < take_profit`
- SHORT: `take_profit < entry_price < stop_loss`
- Limit intents: minimum `INTENT_MIN_RR` of `2.0`
- Stop distance: `0.1%` to `5%` by default
- Entry validity and deterministic delivery ID

If the target fails 2R or geometry admission, emit the advisory event but do not emit
an executor intent.

The executor inbox is configured with:

```dotenv
BYBIT_EXECUTOR_DIR=/home/ubuntu/bybit-executor
INTENT_DELIVERY_ENABLED=true
```

The executor derives sizing from its own account profile. Delivery is file-based,
atomic, and idempotent on replay.

## PM Sidecar Contract

With `PM_SIDECAR_ENABLED=true`, read executor 1m position snapshots and write
`HOLD`, `REDUCE`, or `EXIT` decision files to the configured decision directory.
`HOLD` may veto a discretionary strategy exit. It cannot override a hard stop loss or
fixed take profit. Keep PM reasons short and factual; an LLM explanation is not a
trade authorization.

## Data Purity And Time Safety

Live events must use pure WebSocket data and completed bars. Never mix future bars,
in-progress candles, or stale REST data into a deterministic event. Every evaluator
must cut off at the completed boundary for its evaluation interval and query strictly
earlier observations.

## Operational Checks

```bash
./venv/bin/python src/research_analyst/config.py
./venv/bin/python src/research_analyst/orchestrator.py --once
./venv/bin/python -m pytest -q
```

Inspect, in order, when an event is missing:

1. Universe membership and active strategy IDs
2. Fresh completed `source_observations`
3. Finalized cutoff and feature snapshot
4. Plugin result and `data/alpha_outbox/`
5. Alpha ledger and Discord delivery
6. Executor inbox and executor journal

Health output is `data/health.json`. It must identify the service as
`research-analyst` and report ingestion/evaluation freshness.

## Safety Rules

- Never enable live intent delivery without validating the executor paper path.
- Never put secrets in tracked files or documentation.
- Never claim an advisory signal was filled.
- Never silently change the TradeIntent schema.
- Never run duplicate database writers.
- Never use discovery rank, confidence, or LLM prose as proof of alpha.
