# Research Analyst

**Last reviewed:** 2026-09-07

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
       strategy plugins
       raw_signals
       admission-owned HTF zone context and per-symbol ATR
       hard admission
       deterministic clash resolution
       alpha ledger + publisher
       -> shared SQLite intent bus

 executor 1m position snapshots
   -> standalone-llm-pm (separate service)
        -> executor decision inbox
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
| `research-analyst-orchestrator` | Features, strategies, admission, publishing | `data/analyst.sqlite3` |

All production services are managed by `oxmgr`. Never start a second gateway,
regime worker, or orchestrator manually. Position management is owned by the
separate `standalone-llm-pm` service.

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
running, and retryable work. `VACUUM` is throttled and must never run from a
second writer.

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
the default individual TTL is 72 hours and the hard effective-universe cap is 80
symbols including permanents. A stale or missing feed does not refresh entries;
unexpired entries remain usable, while expired entries are removed. If no
non-permanent entries remain, the effective universe contains permanents only.
Fresh open-position assets may be carried into gateway subscriptions for lifecycle
context, but are not silently added to evaluator scopes.

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
The account-symbol policy remains a downstream admission gate. The strategy engine
uses a 5m-only market-data contract; executor PM snapshots remain 1m and
Binance OI rotation remains separate. See
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
`specs/regime-session-module-v1.md` for the normative contracts. The default
rollout remains `REGIME_SESSION_MODE=shadow` until replay, lookahead,
coexistence, and candidate-admission validation are complete.

## Live Strategy Set

The current production allowlist contains 10 plugins:

| Strategy | Cadence | Family | Account |
| --- | --- | --- | --- |
| `failed-break-v3` | 5m | reversal | Hyro |
| `bb-rsi-meanrev-v1` | 5m | mean_reversion | Hyro |
| `williams-fractal-scalp-v1` | 5m | trend | Hyro |
| `ema9-adx-stochrsi-state-v1` | 5m | trend | Hyro |
| `ema99-retest-adx-v1` | 5m | trend | downstream router |
| `ema20-pullback-h4-trend-v1` | 5m | trend | Fundamo |
| `gold-trend-ema-bb-stoch-v1` | 5m | trend | Fundamo |
| `mtf-exhaustion-reversal-v1` | 5m | reversal | Fundamo |
| `ema99-double-touch-stochrsi-state-v1` | 5m | trend | Fundamo |
| `ema7-26-cross-hammer-shooting-star-1h-adx-v1` | 5m | reversal | Fundamo |

Compact Hyro strategies are limited to `BTC`, `ETH`, `PAXG`, and `QQQ`.
The downstream router currently maps the EMA99 delivery to `bybit/fundamo`.
Propr fan-out is an independent shared-bus target.

Production indicators use the tested in-house EMA, RSI, ATR, ADX, StochRSI, and
Bollinger implementations. Replacing one with a TA library requires numerical
parity tests and an explicit strategy-version change.

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

Structural admission reads completed direct regime-owned `1h`/`4h` bars only for
assets that emitted candidates. It calculates one reusable Wilder ATR14 context
per asset, cutoff, and timeframe, selects the newest eligible `4h` zone before
falling back to `1h`, and evaluates both sides of the trade against that zone:

- Long entry: `0.5-3.0 ATR` above the zone high; SL: `0.5-3.0 ATR` below the zone low.
- Short entry: `0.5-3.0 ATR` below the zone low; SL: `0.5-3.0 ATR` above the zone high.

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

Soft context scores rank candidates but cannot rescue a failed hard gate.
Opposing candidates are resolved deterministically; an unresolved clash emits
no intent. Missing or stale data is rejected by admission, not disguised as a
score.

See `specs/structural-sl-admission-v2.md` for the normative contract.

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
Publisher compatibility handling can reconstruct that field from
`_admission_result.selected_take_profit` for legacy events; events without a
recoverable target remain invalid and are not delivered.

## Position Management

LLM position management is outside this repository and is owned by the
`standalone-llm-pm` service. Research Analyst publishes validated trade intents;
the executor and standalone PM own position lifecycle decisions.

## Operations

```bash
oxmgr list
oxmgr logs research-analyst-symbol-rotation --lines 40
oxmgr logs research-analyst-ws --lines 40
oxmgr logs research-analyst-regime-session --lines 40
oxmgr logs research-analyst-orchestrator --lines 40
```

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
