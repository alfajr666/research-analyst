# Research Analyst

`research-analyst` is a read-and-decide market research service. It consumes
completed Bybit perpetual bars, evaluates the four live compact strategies,
keeps an auditable analyst ledger, publishes advisory alpha, and can hand a
selected intent to the existing `bybit / hyro` executor. It does not hold
exchange credentials, size positions, place orders, or claim that an intent was
filled.

## Runtime topology

```text
Bybit public WS: 1m + 5m kline, mark price
  -> ws_gateway -> market.sqlite3/source_observations
       5m -> local 15m/1h/4h resampling
  -> orchestrator -> finalized cutoff snapshots
       -> four compact strategies
       -> raw_signals (before admission)
       -> hard admission -> soft context scoring -> live clash resolution
            -> analyst.sqlite3 alpha ledger / Discord alpha outbox
            -> immediate bybit / hyro TradeIntent, when selected
  bybit-executor 1m position snapshots
  -> LLM PM sidecar -> HOLD/REDUCE/EXIT PMDecision files
  raw_signals -> non-blocking 30m Discord observation batch
```

SQLite ownership is deliberately split:

- `MARKET_DB_PATH` (`data/market.sqlite3`) is written by `ws_gateway` and holds
  source observations and market freshness data.
- `ANALYST_DB_PATH` (`data/analyst.sqlite3`) is written by the orchestrator and
  holds cutoffs, features, strategy state, raw signals, alpha, delivery, and PM
  state.

Do not point both services at one database and do not run duplicate writers.
Derived 15m, 1h, and 4h bars are local resamples of completed 5m observations;
they are not a CoinAnalyze live-ingestion dependency. The gateway may perform a
small REST warm backfill at startup, but the live stream is Bybit WS.

## Live strategies

The live compact universe is `BTC`, `ETH`, `PAXG`, and `QQQ` from
`symbols/static_universe.json`. The active execution-admission set is:

| Strategy | Primary evaluation |
| --- | --- |
| `failed-break-v3` | 5m, with 15m and 4h context |
| `bb-rsi-meanrev-v1` | 5m |
| `williams-fractal-scalp-v1` | 1m |
| `ema9-continuation-stochrsi-v1` | 1m trigger with 5m setup |

`STRATEGY_ENABLED_IDS`, `STRATEGY_ACTIVE_IDS`, and `plugin_states` control
registration and runtime activation. Other registered v2 plugins are research
capabilities, not part of the four-strategy live compact admission path.

## Decision pipeline

Each plugin runs against a finalized point-in-time cutoff. Every returned
candidate is first captured in append-only `raw_signals`, including candidates
that later fail or are suppressed. Admission then applies only deterministic
hard safety rules:

- valid finite positive prices and future expiry;
- long: `stop < entry < target`; short: `target < entry < stop`;
- reward/risk at least `INTENT_MIN_RR` (default `2.0`);
- stop distance from `0.1%` through `5%` of entry;
- deterministic candidate identity and required strategy-local data.

HTF bias, confirmed swings, FVGs, order blocks, alignment, freshness, strategy
evidence, agreement, and contradictions are soft bounded score components.
Missing context is `unavailable`, not fabricated support and not an automatic
rejection. Scores rank candidates but cannot rescue a failed hard gate.

Live clash resolution ranks same-direction candidates by score with deterministic
priority and lexical tie breaks. Opposite directions require a score margin of
`CLASH_MIN_SCORE_MARGIN` (default `2.0`); an unresolved clash remains advisory
and produces no intent. Losing and failed candidates remain auditable.

## Intent and execution safety

When `INTENT_DELIVERY_ENABLED=true`, a selected candidate is atomically written
to the executor intent inbox. Compact intents are forcibly routed to
`exchange_id=bybit`, `account_id=hyro`; old multi-target routing is not the live
path. The analyst sends thesis fields only. The executor owns credentials,
quantity, risk sizing, leverage, venue precision, portfolio gates, orders,
fills, lifecycle, hard protective SL, and fixed full-close TP.

Keep delivery disabled until the executor paper path, account, symbol universe,
and protection behavior are verified. A Discord or LLM failure must not create,
delay, or suppress an executor intent.

## PM sidecar

The active LLM position-management sidecar runs on a 5m cutoff. It reads
executor 1m snapshots from `EXECUTOR_SNAPSHOT_DIR`, joins the originating intent
and market context, and emits `HOLD`, `REDUCE`, or `EXIT` to `pm_advice` and,
when configured, `EXECUTOR_DECISION_DIR`. Missing credentials, timeout, parse
failure, or invalid output fails safe to `HOLD`. It is emit-only and cannot
change entry, stop, target, direction, sizing, or executor hard protections.

## Raw Discord batch

After a completed daemon cycle, raw candidates are published by a daemon thread
to fixed UTC 30-minute windows. The batch reads committed ledger state and is
non-blocking: webhook latency, retry, or outage cannot affect evaluation or
immediate Bybit/Hyro intent delivery. It is observation-only and disabled unless
`RAW_SIGNAL_DISCORD_BATCH_ENABLED=true` and a webhook is configured.

## Service operation

Production services are managed by the host's `oxmgr` definitions: one
`ws_gateway`, one orchestrator, and one PM sidecar role. The repository's
`ecosystem.config.js` is retired and must not be used to resurrect PM2
processes. Run only one owner for each database.

## Setup and verification

```bash
cp .env.example .env
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python src/research_analyst/config.py
./venv/bin/python src/research_analyst/orchestrator.py --once
./venv/bin/python -m pytest -q
git diff --check
```

For a controlled live-data run, start `ws_gateway` under oxmgr and inspect
market freshness before starting the orchestrator. Then inspect, in order,
`data/health.json`, completed observations, cutoff/plugin results, raw-signal
statuses, `data/alpha_outbox/`, the analyst ledger, executor intents, position
snapshots, and PM decisions. Never commit `.env`, keys, webhooks, databases, or
executor credentials. Signals and LLM prose are evidence, not proof of alpha or
a fill.
