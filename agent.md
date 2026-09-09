# Research Analyst Agent Guide

**Last reviewed:** 2026-09-09

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
- `standalone-llm-pm` owns position-management advice; this repository does not
  run a PM loop or write executor position-decision files.

Never add `quantity`, `risk_amount`, leverage, `order_type`, or credentials to an analyst
intent. Never call old multi-target adapters for the live compact path.

## Market data

Bybit public WS supplies confirmed 5m klines plus mark price. The gateway
has one market SQLite writer. It locally resamples completed 5m data into 15m,
1h, and 4h observations. Startup and re-entry REST backfill seeds missing
streamed intervals through that same writer; CoinAnalyze and venue-aggregate
ingestion are not live defaults. Binance is opt-in.

Market and analyst databases are separate. Use read-only market connections from
analyst code and never start duplicate writers.

Structural admission currently has the optional 15m path enabled in the managed
orchestrator. It selects the nearest eligible directional zone across `4h` and
`1h`, plus `15m` when enabled. Distance is normalized by the selected timeframe's
ATR14 and timeframe priority is only a tie-break. The 15m frame is built from
cutoff-bound, completed market-owned 5m bars in memory; it is not a strategy
input, regime-session input, or persisted structural-zone row. The proposed
strategy stop remains authoritative. Disable or enable this behavior only with
`STRUCTURAL_15M_ZONES_ENABLED` through an `oxmgr` restart; it is independent of
`EVAL_INTERVALS` and `LSR_V1_USE_15M_EPHEMERAL_FVG`. See
`specs/structural-sl-admission-v5-nearest-zone.md`.

## Live strategy set

| ID | Cadence | Family | Route |
| --- | --- | --- | --- |
| `failed-break-v3` | 5m | reversal | Bybit Hyro |
| `bb-rsi-meanrev-v1` | 5m | mean reversion | Bybit Hyro |
| `williams-fractal-scalp-v1` | 5m | trend | Bybit Hyro |
| `ema9-adx-stochrsi-state-v1` | 5m | trend | Bybit Hyro |
| `dual-zone-follower-v3` | 5m | trend | Bybit Fundamo |
| `dual-zone-short-follower-v3` | 5m | trend | Bybit Fundamo |
| `ema99-retest-adx-v1` | 5m | trend | downstream router |
| `ema20-pullback-h4-trend-v1` | 5m | trend | Bybit Fundamo |
| `gold-trend-ema-bb-stoch-v1` | 5m | trend | Bybit Fundamo |
| `mtf-exhaustion-reversal-v1` | 5m | reversal | Bybit Fundamo |
| `ema99-double-touch-stochrsi-state-v1` | 5m | trend | Bybit Fundamo |
| `ema7-26-cross-hammer-shooting-star-1h-adx-v1` | 5m | reversal | Bybit Fundamo |

The legacy `symbols/static_universe.json` policy list has been removed. The
upstream rotation feed ranks the valid Bybit linear USDT-perpetual ticker pool,
selects the configured equal split of top 24h gainers and losers at fixed
four-hour UTC boundaries, and maintains a durable sticky watchlist. The effective
universe is the unexpired watchlist plus BTC, ETH, PAXG, and QQQUSDT (canonical
asset `QQQ`). The default watchlist TTL is 72 hours and the hard effective
universe cap is 80 symbols, including permanents. A stale or missing feed may
continue unexpired entries without refreshing them; expired entries are removed
and the universe fails closed to permanents. Compact Hyro strategies are limited
to the four permanent assets; Fundamo strategies may use every effective-universe
asset. Fresh OPEN executor-position assets may extend gateway market-data
subscriptions for lifecycle context, but never extend the evaluator universe.
Registration is controlled by
`STRATEGY_ENABLED_IDS`; activation is also constrained by
`STRATEGY_ACTIVE_IDS` and `plugin_states`. Registered strategy plugins are not
implicitly live execution strategies. The dual-zone v3 plugins use completed 5m
execution bars and direct regime-owned 1h ADX/DI data; see
`specs/strategy-dual-zone-follower-v3.md`.

The evaluator always materializes features for the cutoff-bound effective
universe, then builds one immutable scope per plugin. Regime enforcement may
restrict a scope by family, but strategies do not inspect rotation, watchlist,
regime, or account policy. Account-symbol admission remains a downstream hard
gate. The strategy engine evaluates on completed 5m cutoffs; direct 1h/4h setup
frames remain separate regime-owned inputs. Binance OI rotation and executor
position snapshots remain separate; the latter is a 1m PM handoff contract.

The effective structural admission contract is v6 while 15m is enabled and v5
otherwise. Selected 15m proofs include the market source, 5m-to-15m resampling,
detector, frame evidence, ATR evidence, nearest-zone policy, and exact cutoff.
Final intent verification rehydrates the cutoff-bound context before accepting
the proof.

Trade intents are published through the shared SQLite intent bus configured by
the absolute `INTENT_BUS_DB`. The shared bus is the sole intent handoff. Do not
create or use a second local intent inbox.

## Candidate lifecycle

For each finalized interval cutoff:

1. Run the active plugins once on point-in-time data.
2. Capture every returned candidate in `raw_signals` before admission.
3. Apply hard SL/TP geometry, finite-price, expiry, RR, stop-distance, identity,
   and strategy-local data checks.
4. Score eligible candidates using independently calculated 4h/1h HTF bias,
   FVG and order-block proximity, alignment, freshness, agreement, and
   contradiction components.
5. Resolve same-direction ranking and opposite-direction clashes immediately.
6. Write only selected intents using the per-strategy route; retain all other outcomes.

Defaults are `RR >= 2.0`, stop distance from the greater of `0.1%` and
configurable `0.25 * ATR14_4h` through `5%`, and clash margin `2.0`.
Missing soft context is `unavailable`, never automatic rejection. Strategy-local
confluence is not consumed by admission scoring. Hard failures
and conflicts are advisory-only and must remain auditable.

## Intent contract

Gate delivery with `INTENT_DELIVERY_ENABLED`. Writes are atomic and idempotent.
The analyst supplies direction, entry condition/reference price, invalidation,
target, expiry, and strategy identity only. If the strategy target is missing,
the producer derives `2R` from a valid entry reference and stop and records
`metadata.target_source=producer_derived_2r`; missing or invalid price inputs
fail closed. It never emits an order-type instruction. The executor profile
selects the entry order type and the executor decides how to size, place,
protect, reconcile, and close the position. A written intent is not an
acceptance, order, or fill.

## Position management

LLM position management is owned by the separate `standalone-llm-pm` service.
This repository publishes validated trade intents only; it does not run a PM
loop or write executor position-decision files. The executor remains
authoritative for venue state, protection, hard exits, and execution.

## Raw Discord batches

Raw capture is durable and synchronous only for the local ledger. A separate
daemon-thread side effect publishes committed candidates in fixed UTC 30-minute
windows. The next boundary publishes the preceding completed window: a `06:00`
message covers `[05:30, 06:00)` and includes all unbatched evaluations in that
window, not only the latest cutoff. Older late-arriving unbatched candidates are
carried into the next available message. It never reruns strategies, waits on
Discord, changes admission, or delays the immediate executor intent path. Treat
it as observation-only.

The compact batch table contains `asset`, `side`, `strat`, and `desc`. It includes
every raw candidate emitted by the evaluated strategy plugins, and each emitted
row is displayed as `PASS` because it passed that strategy's own signal
conditions. This is not an admission, score, clash, selection, delivery, fill,
or execution result. The `+ N more signal evaluations` suffix counts additional
emitted candidates. `skipped N symbols (observed)` counts symbols actually
evaluated by a raw-signal plugin that emitted no candidate; symbols excluded by
cadence, scope, or unavailable required datasets are not counted as failed.
Admission, score, clash, and executor-delivery states remain in
`raw_signal_status_history`.

The exact message shape is:

````text
📊 SIGNAL · research-analyst · 30m
window HH:MM–HH:MM UTC
```
asset  side   strat                    desc
─────  ─────  ───────────────────────  ────
ASSET  LONG   strategy-id              PASS
ASSET  SHORT  strategy-id              PASS
```
+ N more signal evaluations
Research-only observation; no execution, fills, or orders are implied.
skipped N symbols (observed)
````

At most five emitted candidates are shown as rows. The `+ N more signal
evaluations` line counts additional emitted candidates. The skipped count uses
only durable coverage for the completed 30-minute window. If that window has no
raw candidates, the publisher skips it. Late candidates are included once in the
next unclaimed batch and do not cause a second strategy evaluation.

## Operations and safety

Use host `oxmgr` definitions for one gateway and one orchestrator role. The
standalone-llm-pm service is managed separately. The gateway publishes durable
completed-5m evaluation triggers and
the orchestrator consumes them; there is no timer-based evaluation fallback. Do
not launch duplicate database writers. The current deployment has
`STRUCTURAL_15M_ZONES_ENABLED=true`; changing it requires a managed restart and
fresh-cutoff log verification. Keep intent delivery off until paper execution
and protection checks pass. Keep secrets and runtime artifacts untracked.

```bash
./venv/bin/python src/research_analyst/config.py
./venv/bin/python src/research_analyst/orchestrator.py --once  # controlled manual run
./venv/bin/python -m pytest -q
git diff --check
```

When diagnosing missing output, inspect the rotation feed and active IDs, fresh
completed observations, trigger spool/claims, cutoff/features, plugin results,
raw-signal statuses, alpha outbox/ledger, executor inbox, snapshots, and PM
decisions. Report advisory, selected, accepted, and filled as distinct states.

For rotation-specific checks, inspect `data/symbol_rotation_feed.json` for the
feed ID, UTC validity window, source timestamp, selected gainers/losers, sticky
entries, effective-universe version, freshness state, cap, and fallback reason.
Inspect `data/ws_health.json` for the feed ID, effective-universe version,
subscribed symbols and count, topic count, backfill summary, fallback state,
active connections, and last error. A normal fresh rotation feed is typically
34 symbols; the sticky effective universe may grow to the configured cap of 80
until individual entries expire. Feed publication remains boundary-driven, but
the evaluator and gateway use the same cutoff-bound effective state.

## Message contracts

### Discord

- **Alpha entry:** `**ALPHA · LONG|SHORT · ASSET**`, setup family and strategy,
  phase/confidence, trigger, invalidation, targets, validity window, and bounded
  feature context.
- **Research note:** optional `---` section with advisory verdict, thesis, and up
  to two limitations. It never changes deterministic signal fields.
- **Raw signal batch:** exactly `📊 SIGNAL · research-analyst · 30m`, a UTC window,
  a fenced fixed-width `asset / side / strat / desc` table with five raw emitted
  rows, then `+ N more signal evaluations` and `skipped N symbols (observed)`.
- **OI bar and multi-hour:** `OI ROTATION · Binance USDM` with ranked candidates,
  completion/window metadata, expiry where applicable, and the feed-only footer.
- **Exit/reduce:** `HOLD`, `REDUCE`, `EXIT`, and `NEAR_TP` are executor PMDecision
  values, not Discord messages; they cannot alter entry geometry or hard
  protections.

### Trade intent

The executor envelope is `schema_version: 1` JSON with `delivery_id`, `source`,
`exchange_id`, `account_id`, `asset`, unified perpetual `symbol`, normalized
`direction`, `entry_price`, `stop_loss`, `take_profit`, `take_profit_mode`,
`observed_at`, `entry_valid_until`, and non-sizing metadata. The default entry TTL
is five minutes. Geometry requires `LONG: stop < entry < target` or
`SHORT: target < entry < stop`, with RR at least `2.0` and stop distance `0.1%..5%`.
Shared-bus deliveries are idempotent by `delivery_id`. PM decision files include
`confidence` for action-bearing decisions, `reduce_fraction` for `REDUCE` and
`NEAR_TP`, and `decision_scope=NEAR_TP` for `NEAR_TP`. The analyst never emits
`quantity`, `risk_amount`, leverage, or `order_type`; the executor owns those.

Compact strategies are forced to Bybit `hyro` and may trade only the four
permanent assets. They are never delivered to Fundamo, and candidate metadata or
caller arguments cannot override that route. Strategies without an explicit
route use the global default account.
