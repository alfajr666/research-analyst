# Research Analyst

**Last reviewed:** 2026-09-01

`research-analyst` is a read-and-decide market research service. It consumes
completed Bybit perpetual bars and evaluates configured live strategies across the
currently published subscription feed,
keeps an auditable analyst ledger, publishes advisory alpha, and can hand a
selected intent to Bybit executors through the shared SQLite bus. It does not hold
exchange credentials, size positions, place orders, or claim that an intent was
filled.

## Runtime topology

```text
Bybit public REST ticker snapshot (all linear USDT contracts)
  -> symbol_rotation -> four-hour UTC feed (15 gainers + 15 losers)
  -> ws_gateway subscription supervisor
Bybit public WS: 1m + 5m kline, mark price
  -> ws_gateway -> market.sqlite3/source_observations
       5m -> local 15m/1h/4h resampling
  -> orchestrator -> finalized cutoff snapshots
   -> configured strategies across the published subscription feed
        -> raw_signals (before admission)
        -> hard admission (including freshness) -> soft context scoring -> live clash resolution
            -> analyst.sqlite3 alpha ledger / Discord alpha outbox
               -> producer TP selection -> immediate target-specific TradeIntent fan-out, when selected
                  -> target=bybit -> Fundamo Bybit executor
                  -> target=propr -> Propr executor (when enabled)
  bybit-executor 1m position snapshots
  -> LLM PM sidecar -> HOLD/REDUCE/EXIT/NEAR_TP PMDecision files
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

The approved policy universe is defined in `symbols/static_universe.json` and
currently contains 92 Bybit-compatible bases. The active execution-admission set is the
four Fundamo strategy families:

| Strategy | Primary evaluation |
| --- | --- |
| `dual-zone-follower-v2` | 5m, with confirmed 1h ADX/DI |
| `dual-zone-short-follower-v2` | 5m, with confirmed 1h ADX/DI |
| `ema20-pullback-h4-trend-v1` | universal 5m, with 1h signal and 4h trend context |
| `ema-stack-15m-adx-stochrsi-5m-v1` | 5m, with confirmed 15m/1h context |

`STRATEGY_ENABLED_IDS`, `STRATEGY_ACTIVE_IDS`, and `plugin_states` control
registration and runtime activation.

## Performance Rotation

The gateway subscription feed is refreshed at fixed four-hour UTC boundaries.
At each boundary, the rotation worker fetches Bybit's linear 24-hour ticker
snapshot, ranks every valid USDT contract by `lastPrice / prevPrice24h - 1`,
and publishes 15 gainers plus 15 losers alongside permanent `BTC`, `ETH`,
`PAXG`, and `QQQUSDT`. The resulting feed is valid only until the next UTC
boundary. The approved 92-symbol file remains the policy universe and the
rotation-disabled subscription mode; it does not constrain live ticker ranking.

If the ticker snapshot is unavailable or invalid, the feed records the reason
and subscribes to the four permanent symbols. Fresh `OPEN` position assets from
`EXECUTOR_SNAPSHOT_DIR` are always unioned into the gateway subscriptions.
The feed and gateway health files expose the feed ID, validity window, selected
gainers/losers, fallback state, and subscribed count.

The four active strategies route their Bybit deliveries exclusively to account
`fundamo`. When Propr fan-out is enabled, the same admitted thesis is delivered
independently as `target=propr`; Propr owns its account and execution controls:

| Strategy | Account |
| --- | --- |
| `dual-zone-follower-v2` | `fundamo` |
| `dual-zone-short-follower-v2` | `fundamo` |
| `ema20-pullback-h4-trend-v1` | `fundamo` |
| `ema-stack-15m-adx-stochrsi-5m-v1` | `fundamo` |

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
instruction. The executor profile selects the entry order policy. The producer
preserves an explicit strategy target or derives a 2R target from a valid entry
and stop; otherwise delivery is rejected. The executor owns credentials,
quantity, risk sizing, leverage, venue precision, portfolio gates, orders, fills,
lifecycle, hard protective SL, and the supplied full-close TP.

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

The PM sidecar is an independent process from the orchestrator and is the single
LLM position-management authority. It runs on the configured five-minute
decision cadence and reads executor 1m
snapshots from `EXECUTOR_SNAPSHOT_DIR`, joins the originating intent and market
context, and emits `HOLD`, `REDUCE`, `EXIT`, or `NEAR_TP` to `pm_advice` and
`EXECUTOR_DECISION_DIR`. `HOLD` is a no-op and does not require confidence;
action-bearing decisions require the configured confidence threshold. Missing
credentials, timeout, parse failure, or invalid output fails safe to `HOLD`.
Every decision is valid for five minutes. It is emit-only and cannot change
entry, stop, target, direction, sizing, or executor hard protections.

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
  exit message bodies. PM decisions are executor JSON values `HOLD`, `REDUCE`,
  `EXIT`, or `NEAR_TP`; they do not create a new Discord message format.

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
`symbol_rotation` worker, one `ws_gateway`, one orchestrator, and one independent PM sidecar role. The repository's
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

For rotation observability:

```bash
jq '{feed_id,status,valid_from,valid_until,source_as_of,qualified_count,rotating_symbol_count,symbol_count,fallback_reason}' data/symbol_rotation_feed.json
jq '{status,feed_id,subscribed_count,fallback_state,active_connections,last_error}' data/ws_health.json
oxmgr logs research-analyst-symbol-rotation --lines 20
```

The rotation worker bootstraps the current UTC window when needed and publishes
the next feed only at the next four-hour UTC boundary.

## Shared SQLite Bus

Research Analyst publishes validated schema-v1 intents to `target=bybit` and,
when enabled, adapted schema-v2 intents to `target=propr` at the shared bus path
configured by `INTENT_BUS_DB`. Enable the targets with
`INTENT_BUS_BYBIT_ENABLED=true` and `INTENT_BUS_PROPR_ENABLED=true`. All four
active strategies route their Bybit deliveries to `bybit/fundamo`. The Propr
executor independently consumes `target=propr` deliveries and writes its own
receipts. The legacy filesystem inbox is compatibility-only and disabled by
default. Research Analyst never claims bus deliveries or reads execution
receipts. LLM research review is disabled in the live path.
