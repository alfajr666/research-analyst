# Research Analyst

**Last reviewed:** 2026-08-30

`research-analyst` is a read-and-decide market research service. It consumes
completed Bybit perpetual bars and evaluates configured live strategies across the
92-symbol Bybit-compatible static universe,
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
        -> configured strategies across the static universe
        -> raw_signals (before admission)
        -> hard admission (including freshness) -> soft context scoring -> live clash resolution
            -> analyst.sqlite3 alpha ledger / Discord alpha outbox
             -> immediate routed TradeIntent, when selected
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

The live static universe is defined in `symbols/static_universe.json` and currently
contains 92 Bybit-compatible bases. The active execution-admission set is:

| Strategy | Primary evaluation |
| --- | --- |
| `failed-break-v3` | 5m, with 15m and 4h context |
| `bb-rsi-meanrev-v1` | 5m |
| `williams-fractal-scalp-v1` | 1m |
| `ema9-continuation-stochrsi-v1` | 1m trigger with 5m setup |

`STRATEGY_ENABLED_IDS`, `STRATEGY_ACTIVE_IDS`, and `plugin_states` control
registration and runtime activation. Other registered v2 plugins are research
capabilities, not part of the four-strategy live compact admission path.

The four legacy compact strategies are forced to Bybit account `hyro`. The dual-zone
strategies explicitly route to Bybit account `fundamo`:

| Strategy | Account |
| --- | --- |
| `failed-break-v3` | `hyro` |
| `bb-rsi-meanrev-v1` | `hyro` |
| `williams-fractal-scalp-v1` | `hyro` |
| `ema9-continuation-stochrsi-v1` | `hyro` |
| `dual-zone-follower-v1` | `fundamo` |
| `dual-zone-short-follower-v1` | `fundamo` |

## Decision pipeline

Each plugin runs against a finalized point-in-time cutoff. Every returned
candidate is first captured in append-only `raw_signals`, including candidates
that later fail or are suppressed. Admission then applies only deterministic
hard safety rules:

- valid finite positive prices and future expiry;
- market data freshness within `DATA_FRESHNESS_MAX_SECONDS` (default `600`);
- long: `stop < entry < target`; short: `target < entry < stop`;
- reward/risk at least `INTENT_MIN_RR` (default `2.0`);
- stop distance from the greater of `0.1%` and configurable `0.25 * ATR14_4h` through `5%` of entry;
- deterministic candidate identity and required strategy-local data.

HTF bias, confirmed swings, FVGs, order blocks, alignment, strategy evidence,
agreement, and contradictions are soft bounded score components. Freshness is
not a score component for admission: it is a hard gate. Missing or stale market
data rejects the candidate entirely. Health metrics use completed 5m observations
only and exclude open/future bars.
Missing context is `unavailable`, not fabricated support and not an automatic
rejection. Scores rank candidates but cannot rescue a failed hard gate.

Live clash resolution ranks same-direction candidates by score with deterministic
priority and lexical tie breaks. Opposite directions require a score margin of
`CLASH_MIN_SCORE_MARGIN` (default `2.0`); an unresolved clash remains advisory
and produces no intent. Losing and failed candidates remain auditable.

## Intent and execution safety

When `INTENT_DELIVERY_ENABLED=true`, a selected candidate is atomically
published to the shared executor bus. Compact intents are forcibly routed to
`exchange_id=bybit`, `account_id=hyro`; dual-zone intents use their explicit
`fundamo` route. The analyst sends thesis fields only and never emits an `order_type`
instruction. The executor profile selects the entry order policy. The executor
owns credentials, quantity, risk sizing, leverage, venue precision, portfolio
gates, orders, fills, lifecycle, hard protective SL, and fixed full-close TP.

Pipeline failures remain retryable and do not acknowledge their gateway trigger.
After a successful pipeline cycle, the daemon invokes the same signal publisher
used by the one-shot path; publisher failures are isolated from ingestion and
remain visible in logs and delivery state.

Each intent is `schema_version: 1` JSON containing `delivery_id`, `source`,
`exchange_id`, `account_id`, `asset`, unified perpetual `symbol`, normalized
`direction`, `entry_price`, `stop_loss`, `take_profit`, `take_profit_mode`,
`observed_at`, `entry_valid_until`, and non-sizing metadata. The default entry TTL
is five minutes. Geometry requires `LONG: stop < entry < target` or
`SHORT: target < entry < stop`, with RR at least `2.0` and stop distance bounded
by configurable `0.25 * ATR14_4h` plus the `0.1%` absolute floor, up to `5%`.
Bus deliveries are idempotent by `delivery_id`; expired deliveries are not
valid execution opportunities.

Keep delivery disabled until the executor paper path, account, symbol universe,
and protection behavior are verified. A Discord or LLM failure must not create,
delay, or suppress an executor intent.

## PM sidecar

The PM sidecar is an independent process from the orchestrator and runs both
LLM and mechanical management on the configured one-minute cadence by default.
It reads executor 1m
snapshots from `EXECUTOR_SNAPSHOT_DIR`, joins the originating intent and market
context, and emits `HOLD`, `REDUCE`, or `EXIT` to `pm_advice` and
`EXECUTOR_DECISION_DIR`. Missing credentials, timeout, parse failure, or invalid
output fails safe to `HOLD`. It is emit-only and cannot change entry, stop,
target, direction, sizing, or executor hard protections.

Positions promoted from an executor snapshot without an originating intent use
the explicit `unmanaged` strategy identity. The sidecar emits a neutral,
durable `HOLD` advice and decision for them without an LLM call; it never lets
an unmanaged position receive an arbitrary `REDUCE` or `EXIT`.

PM logs include a cycle summary and one correlated event per existing LLM request:
`llm_request_succeeded`, `llm_request_failed`, and `llm_management_decision`.
Failure events classify sanitized causes such as `rate_limit`, `timeout`,
`server_error`, or `invalid_response`; prompts, responses, and credentials are
not logged.

## Raw Discord batch

After a completed daemon cycle, raw candidates are published by a daemon thread
to fixed UTC 30-minute windows. The batch reads committed ledger state and is
non-blocking: webhook latency, retry, or outage cannot affect evaluation or
immediate Bybit/Hyro intent delivery. It is observation-only and disabled unless
`RAW_SIGNAL_DISCORD_BATCH_ENABLED=true` and a webhook is configured.

The locked raw format is:

```text
📊 SIGNAL · research-analyst · 30m
window 01:15–01:45 UTC
asset  side   strat                    desc
─────  ─────  ───────────────────────  ────
ASTER  SHORT  LongVCP                  signal
HOOD   SHORT  GoldTrendEMA_BB_Stoch    signal
POL    SHORT  LongVCP                  signal
MSFT   LONG   TrendSwingTrader         signal
ONDO   SHORT  TrendSwingTrader         signal
+ 91 more signal evaluations
skipped 152 symbols (observed)
```

Only the first five rows are expanded; the remainder is summarized. Raw batches
are observation-only and never go to Telegram or the executor.

## Discord message contracts

- **Entry alpha:** bold `ALPHA · DIRECTION · ASSET`, setup family/strategy,
  phase/confidence, trigger, invalidation, targets, validity window, and bounded
  context.
- **Research note:** optional `---` section with advisory verdict, thesis, and up
  to two limitations.
- **Raw signals:** the exact fixed-width 30-minute format above.
- **OI bar:** `OI ROTATION · Binance USDM · 1h|Nm`, ranked candidates, completion
  and expiry metadata, plus `_Feed only — not an alpha entry signal_`.
- **OI multi-hour:** OI window/generated metadata, repeat hits, latest hour, and
  a by-hour top-1 timeline with the same footer.
- **Trade entry/exit:** executor notifications use the locked legacy entry and
  exit message bodies. PM decisions are executor JSON values `HOLD`, `REDUCE`, or
  `EXIT`; they do not create a new Discord message format.

The locked entry body is:

```text
🚀 Bybit Trade Executed

Symbol: HANA/USDT:USDT
Side: 🔴 SHORT
Amount: 2140.0000
Entry Price: 0.030580
Lev: 25.0x
Strategy: LowFloatBreakoutStrategy
```

The locked exit body is:

```text
❌ [Position Closed]

Symbol: PIEVERSE/USDT:USDT
Side: 🟢 LONG
Entry: 0.925200
Exit: 0.870500
PnL: -5.58 USD (-5.91%)
Outcome: LOSS
Reason: breakout_invalidated
Strategy: LowFloatBreakoutStrategy
```

## Service operation

Production services are managed by the host's `oxmgr` definitions: one
`ws_gateway`, one orchestrator, and one independent PM sidecar role. The repository's
orchestrator consumes durable completed-5m triggers from the WebSocket gateway.
Run only one owner for each database.

The production evaluation path is event-driven: the gateway publishes one
deduplicated trigger after each completed 5m bar is committed, and the
orchestrator evaluates that explicit cutoff. There is no timer-based evaluation
fallback. Trigger claims have a lease, failed work is retried up to the configured
limit, and stranded claims are recovered after restart.

## Setup and verification

```bash
cp .env.example .env
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python src/research_analyst/config.py
./venv/bin/python src/research_analyst/orchestrator.py --once  # controlled manual run
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

## Shared SQLite Bus

Research Analyst publishes validated schema-v1 intents to `target=bybit` at the
shared bus path configured by `INTENT_BUS_DB`. Enable delivery with
`INTENT_BUS_BYBIT_ENABLED=true`; compact strategies route to `bybit/hyro` and
dual-zone strategies to `bybit/fundamo`. The legacy filesystem inbox is
compatibility-only and disabled by default. Research Analyst never claims bus
deliveries or reads execution receipts.
