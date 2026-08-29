# Research Analyst Agent Guide

## Mission and boundaries

Evaluate completed Bybit market data with the configured active strategies, preserve the
full candidate history, and emit advisory alpha or a safe executor intent. This
service has no exchange credentials and never sizes or places orders.

## Ownership

- `ws_gateway` is the sole writer of `market.sqlite3/source_observations`.
- The orchestrator owns `analyst.sqlite3`, cutoffs, features, strategy state,
  raw signals, alpha, and delivery records.
- `bybit-executor` owns credentials, sizing, leverage, venue checks, orders,
  fills, lifecycle, protective SL, and fixed TP.
- The PM sidecar reads executor snapshots and emits advice; it does not trade.

Never add `quantity`, `risk_amount`, leverage, `order_type`, or credentials to an analyst
intent. Never call old multi-target adapters for the live compact path.

## Market data

Bybit public WS supplies confirmed 1m and 5m klines plus mark price. The gateway
has one market SQLite writer. It locally resamples completed 5m data into 15m,
1h, and 4h observations. Startup REST backfill is only a warm seed; CoinAnalyze
and venue-aggregate ingestion are not live defaults. Binance is opt-in.

Market and analyst databases are separate. Use read-only market connections from
analyst code and never start duplicate writers.

## Live strategy set

| ID | Evaluation |
| --- | --- |
| `failed-break-v3` | 5m with 15m/4h context |
| `bb-rsi-meanrev-v1` | 5m |
| `williams-fractal-scalp-v1` | 1m |
| `ema9-continuation-stochrsi-v1` | 1m trigger, 5m setup |

The live static universe is `symbols/static_universe.json` and currently contains
92 Bybit-compatible bases. Legacy compact strategies retain their strategy-specific
asset restrictions; Dual-Zone strategies iterate the full static universe. Registration is
controlled by `STRATEGY_ENABLED_IDS`; activation is also constrained by
`STRATEGY_ACTIVE_IDS` and `plugin_states`. Registered v2 strategies are not
implicitly live execution strategies.

Trade intents are written to the shared executor inbox at
`/home/ubuntu/bybit-executor/data/intents`. Do not create or use a second local intent
inbox.

## Candidate lifecycle

For each finalized interval cutoff:

1. Run the active plugins once on point-in-time data.
2. Capture every returned candidate in `raw_signals` before admission.
3. Apply hard SL/TP geometry, finite-price, expiry, RR, stop-distance, identity,
   and strategy-local data checks.
4. Score eligible candidates using soft HTF bias, swings, FVG, order block,
   alignment, freshness, evidence, agreement, and contradiction components.
5. Resolve same-direction ranking and opposite-direction clashes immediately.
6. Write only selected intents using the per-strategy route; retain all other outcomes.

Defaults are `RR >= 2.0`, stop distance `0.1%..5%`, and clash margin `2.0`.
Missing soft context is `unavailable`, never automatic rejection. Hard failures
and conflicts are advisory-only and must remain auditable.

## Intent contract

Gate delivery with `INTENT_DELIVERY_ENABLED`. Writes are atomic and idempotent.
The analyst supplies direction, entry condition/reference price, invalidation,
target, expiry, and strategy identity only. It never emits an order-type
instruction. The executor profile selects the entry order type and the executor
decides how to size, place, protect, reconcile, and close the position. A
written intent is not an acceptance, order, or fill.

## PM sidecar

With `PM_SIDECAR_ENABLED=true`, the sidecar reads
`<snapshot-root>/<exchange>/<account>/latest.json` and runs at a 5m cutoff. It
emits `HOLD`, `REDUCE`, or `EXIT` with a short reason. Any missing LLM key,
timeout, exception, or invalid response becomes `HOLD`. PM advice cannot weaken
hard SL/TP or alter deterministic event fields.

## Raw Discord batches

Raw capture is durable and synchronous only for the local ledger. A separate
daemon-thread side effect publishes committed candidates in fixed UTC 30-minute
windows. It never reruns strategies, waits on Discord, changes admission, or
delays the immediate executor intent path. Treat it as observation-only.

## Operations and safety

Use host `oxmgr` definitions for one gateway, one orchestrator, and one PM
sidecar role. The gateway publishes durable completed-5m evaluation triggers and
the orchestrator consumes them; there is no timer-based evaluation fallback. Do
not launch duplicate database writers. Keep intent delivery off until paper
execution and protection checks pass. Keep secrets and runtime artifacts
untracked.

```bash
./venv/bin/python src/research_analyst/config.py
./venv/bin/python src/research_analyst/orchestrator.py --once  # controlled manual run
./venv/bin/python -m pytest -q
git diff --check
```

When diagnosing missing output, inspect static universe and active IDs, fresh
completed observations, trigger spool/claims, cutoff/features, plugin results,
raw-signal statuses, alpha outbox/ledger, executor inbox, snapshots, and PM
decisions. Report advisory, selected, accepted, and filled as distinct states.

## Message contracts

### Discord

- **Alpha entry:** `**ALPHA · LONG|SHORT · ASSET**`, setup family and strategy,
  phase/confidence, trigger, invalidation, targets, validity window, and bounded
  feature context.
- **Research note:** optional `---` section with advisory verdict, thesis, and up
  to two limitations. It never changes deterministic signal fields.
- **Raw signal batch:** exactly `📊 SIGNAL · research-analyst · 30m`, a UTC window,
  a fenced fixed-width `asset / side / strat / desc` table with five rows, then
  `+ N more signal evaluations` and `skipped N symbols (observed)`.
- **OI bar and multi-hour:** `OI ROTATION · Binance USDM` with ranked candidates,
  completion/window metadata, expiry where applicable, and the feed-only footer.
- **Exit/reduce:** `HOLD`, `REDUCE`, and `EXIT` are executor PMDecision values,
  not Discord messages; they cannot alter entry geometry or hard protections.

### Trade intent

The executor envelope is `schema_version: 1` JSON with `delivery_id`, `source`,
`exchange_id`, `account_id`, `asset`, unified perpetual `symbol`, normalized
`direction`, `entry_price`, `stop_loss`, `take_profit`, `take_profit_mode`,
`observed_at`, `entry_valid_until`, and non-sizing metadata. The default entry TTL
is five minutes. Geometry requires `LONG: stop < entry < target` or
`SHORT: target < entry < stop`, with RR at least `2.0` and stop distance `0.1%..5%`.
Files are atomic and idempotent by `delivery_id`. The analyst never emits
`quantity`, `risk_amount`, leverage, or `order_type`; the executor owns those.

Compact strategies are forced to Bybit `hyro`; dual-zone strategies explicitly
route to Bybit `fundamo`. Strategies without an explicit route use the global
default account.
