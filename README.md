# Research Analyst

**Last reviewed:** 2026-09-22

Research Analyst is a read-and-decide market research service. It consumes
public market data, evaluates versioned strategy plugins, records auditable
candidates, and publishes validated trade intents when delivery is enabled.
It does not hold exchange credentials, size positions, place orders, or claim
that an intent was filled.

## Current Flow

```text
Bybit public tickers
  -> symbol-rotation worker
  -> versioned sticky performance feed
  -> ws_gateway
       Bybit public WS: completed 5m bars
       startup/re-entry REST backfill for missing streamed intervals
       local 5m -> 15m/1h/4h resampling
       -> data/market.sqlite3
        -> durable 5m evaluation triggers

data/market.sqlite3 + completed 5m cutoff
  -> regime-session worker
       direct Bybit REST 1h + 4h regime cache
       per-asset score + session decision
       -> data/regime.sqlite3

completed trigger
  -> orchestrator
       exact preceding 5m regime scope
       full-universe feature materialization
       -> strategy-runner
            strategy plugins
            candidate results
       raw_signals
       scorer/admission
       deterministic clash resolution
       optional LLM thesis review (veto-only, off/shadow/enforce)
       alpha ledger + publisher
       -> shared SQLite intent bus

```

The gateway writes market observations before publishing a trigger. The
orchestrator claims triggers in cutoff order and marks them processed only after
the pipeline succeeds. A publisher failure is recorded separately and does not
turn a successful evaluation into a failed market pipeline.

## Services

| Service | Responsibility | Owned database or files |
| --- | --- | --- |
| `research-analyst-symbol-rotation` | Bybit ticker ranking and feed publication | `data/symbol_rotation_feed.json` |
| `research-analyst-ws` | Public bars, backfill, resampling, triggers | `data/market.sqlite3` |
| `research-analyst-regime-session` | Per-asset score and gate observations | `data/regime.sqlite3` |
| `research-analyst-strategy-runner` | Read-only strategy plugin evaluation | none |
| `research-analyst-orchestrator` | Cutoffs, candidate persistence, scorer/admission, publishing | `data/analyst.sqlite3` |

All production services are managed by `oxmgr`. Never start a second gateway,
regime worker, strategy runner, or orchestrator manually. Position management
is downstream and outside this repository.

## Discord Signal Batch Format

Raw-signal batch messages use a fixed-width table so asset names, sides, and
strategy names remain easy to scan:

```text
asset name  side   strategy                 desc
──────────  ─────  ───────────────────────  ────
ASTR        SHORT  dual-zone-short-foll...  PASS
```

The `strategy` display column is limited to 23 characters; longer strategy
values receive a `...` suffix. This only shortens the notification text. Raw
candidate records and canonical strategy IDs are never truncated.

## Database Ownership

- `data/market.sqlite3` is written only by `ws_gateway`.
- `data/analyst.sqlite3` is written only by the orchestrator.
- `data/regime.sqlite3` is written only by the regime-session worker; the
  orchestrator reads it for exact-cutoff scope data.
- `/home/ubuntu/shared/intent-bus/intent_bus.sqlite3` is the authoritative
  executor handoff and is written through the shared bus publisher.
- `data/binance_oi.db` belongs to the separate `binance-scanner-oi` project and
  must not be opened, pruned, or maintained here.

Retention runs on the owning writer connection. It preserves active, pending,
running, and retryable work. Evaluation coverage is retained for 7 days. `VACUUM`
runs only through the weekly offline compaction job, never in a live worker.

## Market Data

Bybit is the production public source. The gateway streams completed `5m` bars
and locally derives strategy-facing `15m` bars from completed `5m` observations.
Strategy `1h`/`4h` frames come exclusively from the regime worker's direct Bybit
REST cache; see `specs/direct-htf-engine-v1.md`.

Closed bars may arrive with an end timestamp one millisecond before the
boundary, such as `14:44:59.999` for the `14:45` bar. The resampler normalizes
that representation before exact bucket matching.

The performance rotation feed refreshes at four-hour UTC boundaries and normally
selects 30 rotating assets, 15 gainers and 15 losers, plus permanent `BTC`,
`ETH`, `PAXG`, and `QQQ`. Those selections are persisted in a sticky watchlist:
the default individual TTL is 72 hours and the hard effective-universe cap is 160
symbols including permanents. A stale or missing feed does not refresh entries;
unexpired entries remain usable, while expired entries are removed. If no
non-permanent entries remain, the effective universe contains permanents only.
Fresh open-position assets may be carried into gateway subscriptions for lifecycle
context, but are not silently added to evaluator scopes.

The websocket gateway is live only when `data/ws_health.json` is `healthy` or
`ready`, `active_connections` is positive, and `last_bar_at` is within the
health freshness budget. A PM2-online process with `status=stale` is not a live
market feed. Provider task exits are surfaced as failed health and terminate the
gateway so the process manager can restart it; transient provider reconnects are
expected, but a sustained freshness gap or increasing reconnect churn is an
operational incident.

## Watchlist And Scope Routing

The gateway and evaluator consume the same cutoff-bound effective-universe
version. The gateway reconciles live subscriptions and backfills only newly
added symbols. The evaluator materializes features for the full effective
universe, then the scope router supplies each plugin with an immutable asset
list containing the feed identity, universe version, evaluation cutoff, and
regime-scope provenance.

Strategies remain source-blind: they do not read watchlist configuration,
rotation state, regime state, or account policy. In `REGIME_SESSION_MODE=enforce`,
 the router additionally restricts each plugin to its active market family. The
 account-symbol policy remains a downstream admission gate. The strategy engine
uses a 5m-only market-data contract; downstream position data is outside this
repository and Binance OI rotation remains separate. See
`specs/no-1m-engine-and-strategy-rewrite-v1.md`.

Canonical asset names are preserved throughout the pipeline. For example,
`ANKRUSDT` maps to `ANKR` and `MARSCOINUSDT` maps to `MARSCOIN`; bare asset
names are never truncated.

## Direct HTF Engine

Strategy `1h`/`4h` frames use completed native Bybit REST bars from the
regime-owned history database. The websocket gateway's committed `5m` bars are
the independent execution and evaluation path. No direct bars are merged with
or substituted by resampled `5m` bars.

At an evaluation cutoff, each direct timeframe uses only bars with
`source_end <= evaluation_cutoff`. The engine rejects forming or future bars,
gaps, duplicates, malformed candles, insufficient history, missing direct
history, and cutoff mismatches. A missing direct `1h` or `4h` frame blocks only
strategies requiring that timeframe; a missing `5m` window blocks execution
evaluation.

The default direct seed target is 240 completed bars per timeframe. Retention
expands automatically to at least 14 complete `1h` days and 45 complete `4h`
days, plus fetch margin. The regime worker owns and writes the direct cache;
the engine only reads it. Strategies remain source-blind and keep using
`load_bars_for_interval`.

Candidate events carry the `direct-htf-v1` contract, evaluation cutoff, direct
bar IDs and versions, source mode, availability, and readiness. The full
contract is in `specs/direct-htf-engine-v1.md`.

## Regime Session

The regime worker runs once per completed `5m` cutoff for the current
subscription feed. For each asset it loads completed `5m` observations for
realized-volatility inputs, reads direct `1h` and `4h` history from
`data/regime.sqlite3`, computes in-house ADX and regime inputs, then persists an
immutable score and gate decision. The engine uses the direct cache for strategy
HTF frames. Strategies remain source-blind.

The default ADX length and smoothing are both 14. This implementation requires
57 complete `1h` and `4h` bars before the score is data-ready. During warmup,
the score is `insufficient_data` and the reason is
`regime_score_insufficient_data`. This is expected fail-closed behavior. Do not
reduce the requirement or invent higher-timeframe bars. New or re-entering
assets fetch enough completed direct Bybit `1h`/`4h` history for the regime
contract and configured direct strategy seed depth. The default direct target is
240 bars per timeframe, retaining at least 14 complete `1h` days and 45
complete `4h` days. Gaps, duplicates, malformed candles, stale data, and
missing exact-cutoff evidence block only the affected asset.

`REGIME_SESSION_MODE` controls operational behavior:

- `off` bypasses regime observations and scope filtering.
- `shadow` persists hypothetical blocks and exposes them in logs, but evaluates
  the full subscription universe. `blocked_assets` means would-block.
- `enforce` restricts evaluation to allowed assets and routes each plugin only
  to its active family assets.

Each regime cycle emits compact JSON observability with separate 1h/4h history
readiness, score readiness, gate allow/block counts, active-family counts, and
diagnostics for blocked or insufficient assets. Diagnostics include covered and
missing direct bars, market 5m coverage, missing score inputs, and gate reasons.

Family activation uses hysteresis: ON at `0.35`, OFF at `0.25`. Families are
`trend`, `mean_reversion`, and `reversal`. Feature materialization always covers
the full subscription universe; family filtering occurs only at plugin
invocation under enforcement. Reversal activation is independent: it requires
regular RSI14 divergence on confirmed 5-bar `1h` fractals, recent `1h` ADX14 at
or above 25, and a negative OLS ADX slope over the latest 5 readings. Hidden or
opposing ambiguous divergence fails closed. Reversal can coexist with trend or
mean-reversion and only controls reversal-family scope, never trade geometry,
admission, sizing, or executor protections.

See `specs/regime-history-bootstrap-v2.md`,
`specs/reversal-regime-gate-v1.md`, and
`specs/regime-session-module-v1.md` for the normative contracts. The default rollout remains `REGIME_SESSION_MODE=shadow` until replay, lookahead,
coexistence, and candidate-admission validation are complete. The managed env
maps in `ops/oxfile.toml` pin `REGIME_SESSION_MODE=shadow` explicitly on all
five core targets so every process agrees on the mode.

### Reaction scorer rollout

`REACTION_SCORER_MODE` is the single rollout switch for the reaction-oriented
value-profile, participation, OI, and funding scorer:

- `off` keeps the legacy trade-quality scorer only;
- `shadow` computes and persists reaction evidence while the legacy scorer
  remains operational;
- `enforce` makes the reaction scorer's result operational.

The reaction scorer never publishes directly to a venue. Validated intents still
cross only the shared SQLite intent bus, and bus target switches remain
independent.

The reaction profile uses completed 15m bars with a minimum of 72 bars, a normal
lookback cap of 288 bars, and a 72-hour exponential half-life. Its anchor is the
highest complete hourly-volume anchor and remains eligible while the prior
anchor decays. The v3 weights are value reaction 0.50, participation 0.20, OI
participation 0.15, and direction-aware funding crowding 0.15. OI and funding
therefore affect enforce mode; they are not zero-weight evidence.

### LLM thesis review (implemented)

`specs/llm-thesis-review-v1.md` is implemented as an optional, veto-only LLM
reviewer after deterministic admission, scoring, and clash resolution and
immediately before shared-bus publication. `LLM_THESIS_REVIEW_MODE` controls it:

- `off` (default) keeps the no-LLM runtime;
- `shadow` runs the review and records the result without changing publication;
- `enforce` publishes only `pass` decisions: vetoes, review errors, missing
  review metadata, and any non-`pass` outcome suppress the candidate.

The reviewer is blinded: its input carries point-in-time evidence and the
repository-owned strategy thesis, never the deterministic verdict, score, clash
conclusion, or publisher state. Application code — never the model — derives
pass/veto at thesis score 70. Any provider failure (timeout past the 3s
deadline, invalid output, provider outage) fails open to publication and is
recorded as `unavailable`; a veto can never be rescued and unavailable never
becomes a veto. Repeated failures open a 15-minute circuit breaker that
fast-fails subsequent reviews until the cooldown expires. Every attempt is
persisted as a compact `thesis_reviews` row with a 120-day bounded retention;
passing provenance travels on the intent as versioned `metadata.thesis_review`
and is re-validated at the shared-bus handoff. The reviewer never mutates
intent geometry, never sizes, and never executes. Research Analyst sends no
LLM-review Discord messages; an executor may show the review only on a
venue-confirmed entry message.

The HTTP provider is configured by `THESIS_REVIEW_PROVIDER` (default `zai`),
`THESIS_REVIEW_MODEL`, `THESIS_REVIEW_API_KEY`, and optional
`THESIS_REVIEW_BASE_URL`; see `.env.example`.

## Live Strategy Set

The default production allowlist contains the 7 vectorbt engine-handoff ports
(`specs/strategy-vectorbt-ports-v1.md`) plus the UTC-session MR retiree.
Each port is a self-contained plugin built on the repository's native
indicator engines — no shared port module and no vectorbt, pandas, or numpy
dependency:

| Strategy | Cadence | Family | Execution |
| --- | --- | --- | --- |
| `bb-tp-race-locked-v1` | 5m cutoff, 15m frame | trend | Bybit executor |
| `bb-squeeze-trend-v1` | 5m cutoff, 30m frame | trend | Bybit executor |
| `kama-trend-following-v1` | 5m cutoff, 30m frame | trend | Bybit executor |
| `macd-ema-v1` | 5m cutoff, 1h frame | trend | Bybit executor |
| `mr-vwap-locked-v1` | 5m cutoff, 15m frame | mean_reversion | Bybit executor |
| `mr-vwap-utc-session-v3` | 5m cutoff, 15m frame | mean_reversion | Bybit executor |
| `trend-pullback-vwap-v1` | 5m cutoff, 15m frame | trend | Bybit executor |
| `trend-wall-v5` | 5m cutoff, 30m frame | trend | Bybit executor |

`mr-vwap-utc-session-v3` (active since 2026-09-22, fundamo-routed like its
siblings) shares its strategy ID with the RAHL producer: UTC-midnight-session
VWAP mean reversion with a 1.5-ATR stop. It coexists with `mr-vwap-locked-v1`
(distinct anchor); same-direction clashes resolve deterministically.

All ported plugins evaluate on completed `5m` cutoffs; the `15m`, `30m`, and
`1h` signal frames are derived causally from completed bars and never merge
with canonical execution data. `bb-tp-race-locked-v1` preserves its intrabar
TP-race lifecycle by emitting an executor `bracket_spec` in candidate metadata;
the executor owns the arming, break-even latch, and TP-race behavior.

The legacy 12-plugin production set remains registered but is disabled by
default (`LEGACY_PRODUCTION_STRATEGY_IDS`); re-enable it explicitly through
`STRATEGY_ENABLED_IDS`:

| Strategy | Cadence | Family | Execution |
| --- | --- | --- | --- |
| `failed-break-v3` | 5m | reversal | Bybit executor |
| `bb-rsi-meanrev-v1` | 5m | mean_reversion | Bybit executor |
| `williams-fractal-scalp-v1` | 5m | trend | Bybit executor |
| `ema9-adx-stochrsi-state-v1` | 5m | trend | Bybit executor |
| `dual-zone-follower-v3` | 5m | trend | Bybit executor |
| `dual-zone-short-follower-v3` | 5m | trend | Bybit executor |
| `ema99-retest-adx-v1` | 5m | trend | Bybit executor |
| `ema20-pullback-h4-trend-v1` | 5m | trend | Bybit executor |
| `gold-trend-ema-bb-stoch-v1` | 5m | trend | Bybit executor |
| `mtf-exhaustion-reversal-v1` | 5m | reversal | Bybit executor |
| `ema99-double-touch-stochrsi-state-v1` | 5m | trend | Bybit executor |
| `ema7-26-cross-hammer-shooting-star-1h-adx-v1` | 5m | reversal | Bybit executor |

Account routing is venue-owned, not strategy-owned. The Bybit executor applies
the profile capability policy: `bybit/hyro` allows only `BTC`, `ETH`, `PAXG`,
and `QQQ` for any strategy, while `bybit/fundamo` allows every strategy and
venue-admitted symbol. RA does not define an account-specific asset or strategy
allowlist, and invalid profile combinations are rejected before an order call.
Propr fan-out is an independent shared-bus target.

Production indicators use the tested in-house EMA, RSI, ATR, ADX, StochRSI, and
Bollinger implementations. Replacing one with a TA library requires numerical
parity tests and an explicit strategy-version change.

Dual-zone v3 uses completed `5m` execution bars, completed cutoff-bounded `15m`
EMA7/EMA26/EMA99 inputs, and direct regime-owned `1h` ADX/DI history. Its
execution cadence remains `5m`; the 15m frame changes only the EMA inputs and
does not change candidate expiry or execution timing. Candidates enter the
normal structural admission and shared SQLite intent-bus pipeline; the retired
v2 IDs are historical metadata only. See
`specs/strategy-dual-zone-follower-v3.md`.

## Evaluation And Admission

Every plugin candidate is first written to `raw_signals`, including candidates
that later fail. Hard admission checks:

- finite, positive prices and valid expiry;
- completed market-data freshness within `DATA_FRESHNESS_MAX_SECONDS`;
- correct long or short entry, stop, and target geometry;
- minimum reward/risk;
- ATR-bounded stop distance;
- required strategy-local data;
- symbol-account policy;
- admission-owned HTF structure and per-symbol ATR proximity.

### Engine-owned venue TP (mechanical exit fallback)

Strategies own exit levels (`mechanical_ta` or `level`: TP1, TP2, …); the
engine owns venue placement. Before any gate, admission converts every
candidate to the N-R venue TP (`INTENT_MECHANICAL_EXIT_FALLBACK_R`, default
2.0): the envelope `take_profit`/`targets` are always entry ± N×risk, with
`target_source=engine_fallback_2r` and the natives preserved in
`metadata.exit_rule` (`{kind, native_levels}`) for the standalone PM. Gates —
geometry, reward/risk, fingerprint — evaluate placed values; the admission
fingerprint binds `exit_rule` natives so pipeline and handoff proofs match.
`take_profit_mode` keeps naming the native rule (`vwap_target`,
`bracket_tp1_tp2_race`, `symmetric_atr_bracket`, default `fixed_full_close`).
Normative contract: `specs/mechanical-exit-fallback-v1.pointer.md`.

Structural admission reads completed direct regime-owned `1h`/`4h` bars only for
assets that emitted candidates. It calculates one reusable Wilder ATR14 context
per asset, cutoff, and timeframe, then selects the nearest eligible directional
zone across `4h` and `1h`. When enabled, cutoff-bound market-owned `5m` bars are
resampled in memory to `15m` and included in the same nearest-zone comparison.
Distance is normalized by the selected timeframe's ATR14; timeframe priority is
only a tie-break. It evaluates both sides of the trade against the selected zone:

- Long entry: inside a bullish support zone, or `0.5-3.0 ATR` above its high;
  SL: `0.5-3.0 ATR` below the zone low.
- Short entry: inside a bearish resistance zone, or `0.5-3.0 ATR` below its
  low; SL: `0.5-3.0 ATR` above the zone high.

Missing, stale, invalid, opposing, cross-asset, incomplete, or out-of-band
structure fails closed before scoring. The strategy's proposed stop remains
authoritative and is never changed. Admission records the selected zone,
entry/SL buffers, ATR provenance, exact cutoff, and source bar IDs. The old
global `INTENT_MAX_STOP_DISTANCE_PCT` cap is removed; the structural `3.0 ATR`
maximum is the relevant proximity maximum.

HTF zone records are not included in strategy snapshots. They are constructed
only after strategy evaluation for emitted candidates. Alpha, compatibility,
and shared-bus handoffs require a passing admission proof; direct intent writes
without that proof are rejected.

Independent soft context scores rank candidates but cannot rescue a failed hard
gate. They use 4h/1h HTF bias, FVG and order-block proximity, cross-timeframe
alignment, freshness, same-symbol agreement, and contradiction penalties.
Strategy confluence and swing components are not admission-score inputs.
Opposing candidates are resolved deterministically; an unresolved clash emits no
intent. Missing or stale data is rejected by admission, not disguised as a score.

See `specs/structural-sl-admission-v5-nearest-zone.md` and
`specs/trade-admission-and-clash-resolution.md` for the normative contracts.
The current managed deployment has `STRUCTURAL_15M_ZONES_ENABLED=true`; change it
only through a managed orchestrator restart. It is independent of evaluation
cadence and LSR's ephemeral 15m FVG setting.

### Raw Discord Batch Status

The raw-signal Discord batch is an observation-only view of strategy candidates.
The publisher runs at the next evaluation after each fixed UTC 30-minute boundary.
For example, the `06:00` message covers the completed `[05:30, 06:00)` window
and includes every unbatched raw evaluation from that window, not only the latest
cutoff. Older late-arriving candidates that are still unbatched are carried into
the next available message; a candidate is never re-evaluated for Discord.

The message template is:

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

The batch includes every raw candidate emitted by the evaluated strategy
plugins. `PASS` means only that the strategy emitted the signal; it does not mean
admission passed, scoring selected it, delivery succeeded, or execution occurred.
Only the first five emitted candidates are shown as table rows; `+ N more signal
evaluations` counts the remaining emitted candidates. `skipped N symbols
(observed)` counts symbols actually evaluated by a raw-signal plugin in that
30-minute window that emitted no candidate. Symbols excluded by cadence, scope,
or missing required datasets are not counted as failed evaluations. If a completed
window has no raw candidates, it is skipped. Admission, score, clash, and
executor-delivery states remain in `raw_signal_status_history`.

## Intent Delivery

The analyst publishes only after admission and routing. The shared SQLite bus
requires an explicit absolute `INTENT_BUS_DB`:

- `INTENT_BUS_BYBIT_ENABLED=true` enables Bybit delivery.
- `INTENT_BUS_PROPR_ENABLED=true` enables independent Propr fan-out.

The analyst sends thesis and trade-plan fields only. The executor owns
credentials, sizing, leverage, precision, orders, fills, protective stops,
take-profit execution, and receipts. Research Analyst never claims execution
state.

The alpha outbox stores admitted targets in the top-level `targets` field.
Since the engine fallback, those are the placed N-R targets; strategy natives
travel in `metadata.exit_rule`. Publisher compatibility handling can recover
that field from `_admission_result.selected_take_profit` for legacy events;
events without a recoverable target remain invalid and are not delivered.

The intent-bus publisher converts nested Python `datetime` values to UTC
ISO-8601 strings before handing an envelope to the JSON-only shared bus. This
keeps admission proofs and source metadata serializable without changing the
trade intent contract.

The active publisher scans only JSON files directly under `data/alpha_outbox/`.
Files under `data/alpha_outbox/quarantine/` are unused archival artifacts and
may be deleted only after verifying that every file is expired and belongs to a
retired strategy or invalid legacy schema. Preserve active top-level events.

## Downstream Boundary

Research Analyst stops at validated shared-bus publication. It contains no
position-management or venue-adapter path.

## Operations

```bash
oxmgr list
oxmgr logs research-analyst-symbol-rotation --lines 40
oxmgr logs research-analyst-ws --lines 40
oxmgr logs research-analyst-regime-session --lines 40
oxmgr logs research-analyst-strategy-runner --lines 40
oxmgr logs research-analyst-orchestrator --lines 40
```

For a local observation-only smoke run when `oxmgr` is unavailable, a
task-local PM2 home may supervise the same entrypoints, but this is not the
production supervisor. Set both `INTENT_BUS_BYBIT_ENABLED=false` and
`INTENT_BUS_PROPR_ENABLED=false` explicitly; verify cycle health and logs before
enabling any delivery target. A stale `data/ws_health.json` or absent fresh
market bars means the pipeline is not live even if the process is online.

The safe local PM2 pattern is:

```bash
PM2_HOME=/tmp/research-analyst-pm2 pm2 status
PM2_HOME=/tmp/research-analyst-pm2 pm2 restart research-analyst-ws research-analyst-hl-producer
PM2_HOME=/tmp/research-analyst-pm2 pm2 stop all
```

Use the repository virtualenv (`venv/bin/python`); it must satisfy
`venv/bin/python -m pip check` and import `websockets`. Stop the task-local PM2
processes after an observation window; do not leave a dry-run supervisor
running as a production service.

`research-analyst-regime-session` is health-checked by
`scripts/regime_session_healthcheck.py`, which verifies the worker process and
the recency and shape of its latest completed cycle. The probe handles both
timestamp-prefixed output and long JSON records persisted without a timestamp
prefix by `oxmgr`, using `cutoff_at` as the cycle freshness value. The tracked
oxmgr definition is `ops/oxfile.toml`, and the full worker invocation is kept in
the app's `command` field.

`research-analyst-symbol-rotation` is health-checked by
`scripts/symbol_rotation_healthcheck.py`, which verifies the worker process and
that the performance feed is currently `ready` or `fallback`. Its tracked
definition is also in `ops/oxfile.toml`.

`research-analyst-strategy-runner` is health-checked by
`scripts/strategy_runner_healthcheck.py`, which verifies the runner socket
responds with a ready status. Its tracked definition is also in
`ops/oxfile.toml`.

For a deployment of explicitly approved code, restart only services importing
the changed modules. Verify fresh cutoff logs, restart counts, market freshness,
regime persistence, pipeline completion, and publisher state.

Database retention runs online every six hours on each database owner's writer
connection. It deletes in small committed batches and never runs `VACUUM` in a
worker. Schedule the offline compaction job during a low-activity UTC window:

```cron
30 4 * * 0 /home/ubuntu/research-analyst/scripts/compact_databases.sh
```

The compaction job stops the research-analyst services, verifies they are down,
backs up and compacts `market.sqlite3`, `analyst.sqlite3`, and `regime.sqlite3`,
runs integrity checks, and restarts only services that were active before the
job. It does not touch the Binance OI database or executor databases.

## Setup And Verification

The root `cli.py` provides the local, JSON-only operator interface described in
`specs/LOCAL_CLI_SPEC.md`. It is enabled by default; set
`RESEARCH_ANALYST_LOCAL_CLI_ENABLED=false` to refuse all CLI commands. The CLI
uses read-only database connections for observations and only calls `oxmgr` for
the four allowlisted service targets.
Service stop/restart operations require the boolean `--confirm` acknowledgement.
This flag is an operator-intent check, not an authentication token; the CLI does
not place orders or report execution state.

Examples:

```bash
./cli.py status
./cli.py research candidates --asset BTC --limit 20 --pretty
./cli.py bus deliveries --limit 20
./cli.py service logs research-analyst-ws --lines 40
```

```bash
cp .env.example .env
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python src/research_analyst/config.py
python3 -m pytest -q
python3 -m compileall -q src tests
```

Use `--once` only for controlled local or replay runs. Do not run production
daemons manually. Never commit `.env`, API keys, webhooks, databases, or
executor credentials.
