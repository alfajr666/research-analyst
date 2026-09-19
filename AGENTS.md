# Research Analyst Agent Guide

**Last reviewed:** 2026-09-19

This repository is a read-and-decide market research service. It produces
auditable candidates and validated trade intents. It does not hold exchange
credentials, size positions, place orders, or claim that an intent was filled.

## Runtime Contract

```text
Bybit public tickers
  -> symbol-rotation worker
  -> versioned performance feed
  -> ws_gateway subscriptions
        Bybit public WS: completed 5m bars
       startup/re-entry REST backfill
       local 5m -> 15m/1h/4h resampling
       -> data/market.sqlite3
        -> durable 5m evaluation triggers

data/market.sqlite3 + completed 5m cutoff
  -> regime-session worker
       direct Bybit REST 1h + 4h regime history in regime.sqlite3
       per-asset regime score and session decision
       -> data/regime.sqlite3

completed evaluation trigger
  -> orchestrator
       exact preceding 5m regime scope
       full-universe feature materialization
       strategy plugins
       raw candidate ledger
       admission-owned HTF zone context and per-symbol ATR
       deterministic admission and clash resolution
       optional LLM thesis review (veto-only, off/shadow/enforce)
       alpha ledger and publisher
       -> shared SQLite intent bus, when enabled

```

The gateway emits triggers only after market observations are committed. The
orchestrator claims triggers in cutoff order, uses a lease for crash recovery,
and marks a trigger processed only after the pipeline succeeds. Publisher
failures are separate from pipeline failures.

The gateway runtime must use the repository `venv/bin/python` with the declared
`requirements.txt` installed, including `websockets`. `data/ws_health.json` is
the liveness contract: `healthy`/`ready`, a positive `active_connections`, and a
fresh `last_bar_at` are all required. A PM2-online process with `status=stale`
is not live. Provider task exceptions are surfaced as failed health and exit the
gateway for process-manager restart; transient reconnects are allowed only when
fresh bars continue to arrive.

## Database Ownership

- `data/market.sqlite3` is owned and written by `ws_gateway`.
- `data/analyst.sqlite3` is owned and written by the orchestrator.
- `data/regime.sqlite3` is owned and written by the regime-session worker; the
  orchestrator and direct HTF engine read it read-only for the exact evaluation
  cutoff.
- `/home/ubuntu/shared/intent-bus/intent_bus.sqlite3` is the authoritative
  executor handoff and is written through the shared bus publisher.
- `data/binance_oi.db` belongs to the separate `binance-scanner-oi` project.
  Never open, prune, or maintain it from this repository.

Never point two services at one database and never run duplicate database
writers. Gateway retention runs on the gateway writer connection. Analyst
retention runs on the orchestrator connection. Retention must preserve active,
pending, running, and retryable work. Online workers never run `VACUUM`; the
weekly offline compaction job owns checkpointing and file compaction.

## Regime Session

The regime worker runs once per completed 5m cutoff for the current subscription
feed. It loads each asset's completed 5m observations for realized-volatility
inputs, reads direct 1h and 4h history from `regime.sqlite3`, computes ADX and
  regime inputs, and persists an immutable score and gate decision. The strategy
  engine reads the same direct cache for native HTF setup. Strategies remain
  source-blind.

The score is a research ranking input. Data readiness is a hard admission
condition. The in-house ADX implementation requires 57 complete 1h and 4h bars
with the default length and smoothing of 14. During warmup,
`status=insufficient_data` and `regime_score_insufficient_data` are expected and
must fail closed. Do not lower the requirement or fabricate higher-timeframe
data.

The regime worker fetches enough completed Bybit linear-perpetual 1h/4h history
  for the regime contract and configured direct strategy seed depth. The default
  direct target is 240 bars per timeframe, retaining at least 14 complete 1h days
and 45 complete 4h days. It stores them in `regime_1h_bars` and `regime_4h_bars`
in `regime.sqlite3`. It owns the durable interval-specific backfill job state,
validates duplicates and gaps, and retries failed assets independently. The
  engine uses the direct history for strategy HTF frames; it never merges those
  frames with canonical 5m data.

`REGIME_SESSION_MODE` has three meanings:

- `off`: bypass regime-session observations and scope filtering.
- `shadow`: persist and expose hypothetical blocks, but evaluate the full
  subscription universe. `blocked_assets` means would-block, not an operational
  block.
- `enforce`: restrict evaluation to allowed assets and route each plugin only
  to its active market-family assets.

Family activation uses hysteresis: ON at `0.35`, OFF at `0.25` by default. The
families are `trend`, `mean_reversion`, and `reversal`. The orchestrator always
materializes features for the full subscription universe; enforcement happens
at plugin invocation. Trend and mean-reversion use hysteresis. Reversal uses
the dedicated boolean gate: completed 1h regular RSI14 divergence on confirmed
5-bar fractals, plus ADX14 >= 25 within the prior 20 readings and a negative
OLS slope over the latest 5 readings. Hidden divergence is excluded, opposing
regular divergences fail closed as ambiguous, and reversal may coexist with
the other families. The gate only controls family scope; it does not produce a
trade, override admission, or affect executor protections.

The direct-history and reversal contracts are specified in
`specs/regime-history-bootstrap-v2.md` and
`specs/reversal-regime-gate-v1.md`. Score and gate persistence versions change
when their provenance or routing semantics change. `REGIME_SESSION_MODE=shadow`
is the default rollout mode; enforcement requires replay, lookahead, and
candidate-admission validation.

Canonical asset names must remain intact. Native symbols such as `ANKRUSDT` are
normalized to `ANKR`; bare names such as `MARSCOIN` must not be truncated.

The production sticky watchlist cap is 160 symbols including the four permanent
assets (`BTC`, `ETH`, `PAXG`, and `QQQ`). Rotation still selects 30 new assets
per four-hour boundary (15 gainers and 15 losers); the 72-hour sticky TTL lets
the effective watchlist fill between refreshes. The evaluator and gateway must
consume the same effective-universe version.

## Direct HTF Engine

The engine owns the strategy `1h`/`4h` warmup path. Each evaluation uses the
exact trigger cutoff for both native direct frames and canonical execution
data. The direct frames are never merged with canonical data.

Each evaluation has one authoritative cutoff: `evaluation_cutoff` is the exact
trigger cutoff and remains authoritative for strategy data, candidate
timestamps, freshness, and replay. Direct bars are eligible
`evaluation_cutoff` is the exact trigger cutoff and direct bars are eligible
only when `source_end <= evaluation_cutoff`.

The engine reads the regime worker's immutable direct Bybit REST bars for
strategy setup and the market worker's committed completed `5m` bars for
execution evaluation:

```text
direct 1h/4h:      source_end <= evaluation_cutoff
execution 5m:      source_end <= evaluation_cutoff
```

The direct target is 240 completed bars per timeframe. Retention must cover
at least 14 complete `1h` days and 45 complete `4h` days, with fetch margin; the
configured seed depth increases those requirements automatically. The regime
worker remains the only writer of the direct cache. The engine opens both
databases read-only and installs a cutoff-bound context for each evaluation
stage. Strategies continue calling `load_bars_for_interval` and must not branch
on the data source. Missing direct history fails closed; there is no canonical
HTF fallback or parity rollout mode.

Every affected frame fails closed on forming/future bars, gaps, unresolvable
duplicates, malformed candles, invalid boundaries, or cutoff mismatch. Closed
exchange representations such as an exact boundary and boundary-minus-one
millisecond are normalized to one logical bar before duplicate validation.
Candidate provenance includes the direct contract version, exact cutoff, direct
bar IDs/versions, availability, source mode, and readiness.

## Live Strategy Set

The default production allowlist currently contains the 7 vectorbt
engine-handoff ports (`specs/strategy-vectorbt-ports-v1.md`; source authority
in the engine-handoff documents of the source repository):

| Strategy | Cadence | Family | Execution |
| --- | --- | --- | --- |
| `bb-tp-race-locked-v1` | 5m cutoff, 15m frame | trend | Bybit executor |
| `bb-squeeze-trend-v1` | 5m cutoff, 30m frame | trend | Bybit executor |
| `kama-trend-following-v1` | 5m cutoff, 30m frame | trend | Bybit executor |
| `macd-ema-v1` | 5m cutoff, 1h frame | trend | Bybit executor |
| `mr-vwap-locked-v1` | 5m cutoff, 15m frame | mean_reversion | Bybit executor |
| `trend-pullback-vwap-v1` | 5m cutoff, 15m frame | trend | Bybit executor |
| `trend-wall-v5` | 5m cutoff, 30m frame | trend | Bybit executor |

The legacy 12-plugin production set remains registered and is disabled by
default; enable it explicitly through `STRATEGY_ENABLED_IDS`:

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

The retired research plugins (`accumulation-base-v2`, `impulse-ignition-v2`,
`continuation-breakout-v2`, `rsi-reclaim-v1`, `liquidity-sweep-reversal-v1`,
`ema9-continuation-stochrsi-v1`, `ema-stack-15m-adx-stochrsi-5m-v1`,
`trend-wall-v1`) remain registered for replay and research use only.

Account capability is executor-owned: `bybit/hyro` allows `BTC`, `ETH`, `PAXG`,
and `QQQ` for any strategy, while `bybit/fundamo` allows every strategy and
venue-admitted symbol. RA publishes strategy and symbol evidence; it does not
own account-specific routing or capability allowlists.
Propr fan-out is independent and enabled only by its shared-bus switch.

Production strategies use the repository's tested in-house EMA, RSI, ATR, ADX,
StochRSI, and Bollinger implementations. A TA library cannot replace them
without numerical parity tests and an explicit strategy-version change.

Dual-Zone v3 evaluates on completed `5m` bars but computes EMA7, EMA26, and
EMA99 from the separate cutoff-bounded completed `15m` frame. The 5m frame
provides execution close, timestamp, entry, and five-minute validity; the 15m
frame provides EMA trend, channel, stop-anchor, and target-anchor values. Its
ADX/DI filter remains on the direct regime-owned `1h` frame. Do not collapse
these inputs into one timeframe or change the 5m execution cadence.

## Discord Signal Batch Table

Raw-signal batch messages use a fixed-width table with explicit `asset name` and
`strategy` columns:

```text
asset name  side   strategy                 desc
──────────  ─────  ───────────────────────  ────
ASTR        SHORT  dual-zone-short-foll...  PASS
```

Strategy values longer than the 23-character display column are shortened with
`...` for readability. This is display-only; the raw and canonical strategy
IDs remain unchanged.

## Admission And Delivery

### Reaction scorer rollout

`REACTION_SCORER_MODE` is the single `off`/`shadow`/`enforce` switch. Enforce
uses the reaction scorer operationally; shadow persists evidence while the
legacy scorer remains operational. The profile is cutoff-bound and uses a
minimum of 72 completed 15m bars, a 288-bar cap, and a 72-hour exponential
half-life. The highest complete hourly-volume anchor remains eligible while its
strength decays. Weights are value reaction 0.50, participation 0.20, OI
participation 0.15, and direction-aware funding crowding 0.15. OI and funding
must affect enforce mode; neither is a diagnostic-only zero-weight field.

The scorer is pure research evidence. It never writes a venue adapter or
executor inbox; validated intents cross only the shared SQLite intent bus.

### LLM thesis review (implemented)

`specs/llm-thesis-review-v1.md` is implemented. The reviewer is one optional,
veto-only LLM call per selected candidate, after deterministic
admission/scoring/clash and outside the publisher, gated by
`LLM_THESIS_REVIEW_MODE=off|shadow|enforce` (default `off`). It is blind to
scorer and clash conclusions: its input is the blinding-contract
`ThesisReviewInputV1` (point-in-time evidence plus repository-owned strategy
thesis only). Application code derives binary pass/veto at thesis score 70.
Unavailable review fails open to publication (`unavailable`, never a veto); a
veto can never be rescued; one review attempt per candidate with a 3s deadline;
repeated failures open a 15-minute circuit breaker. Every attempt is persisted
as a compact `thesis_reviews` row (120-day bounded retention in
`db_maintenance`). Passing provenance crosses the bus only as versioned
`metadata.thesis_review`, re-validated at the intent handoff. The reviewer
never mutates intent geometry, never sizes, never executes, and RA sends no
LLM-review Discord messages. Promote off -> shadow -> enforce; enforce requires
a soak in shadow first.

Every plugin candidate is first captured in `raw_signals`. Deterministic hard
admission then checks finite prices, freshness, expiry, trade geometry, reward
to risk, ATR-bounded stop distance, required data, symbol-account policy, and
structural-stop rules. Before scoring, admission reads completed direct 1h/4h
bars from regime-owned history only for assets that emitted candidates, builds
one reusable context per asset/cutoff/timeframe, and selects the nearest eligible
4h/1h zone, or a covered 15m zone when enabled. Distances are normalized by the
selected timeframe's ATR14; timeframe priority is used only for ties. A long entry may be inside a bullish support
zone or above its high; an outside entry must be 0.5-3.0 ATR above zone high,
and its SL must be 0.5-3.0 ATR below zone low. Shorts mirror those rules.
Missing, stale, invalid, opposing, cross-asset, incomplete, or out-of-band
structure fails closed. Structural failure occurs before scoring and cannot be
rescued by a soft context score. Clash resolution is deterministic and
unresolved opposing signals produce no intent.

The proposed strategy stop remains authoritative and is never mutated. The
admission result records the selected zone, entry/SL buffers, ATR method and
period, exact cutoff, and source bar IDs for auditability. The global
`INTENT_MAX_STOP_DISTANCE_PCT` cap is removed; the structural 3.0 ATR maximum
is the maximum zone-to-entry and zone-to-SL distance policy.

The independent admission scorer ranks only hard-admitted candidates. It uses
4h/1h HTF bias, FVG and order-block proximity, cross-timeframe alignment, data
freshness, same-symbol agreement, and contradiction penalties. Strategy-specific
confluence and swing components are not admission-score inputs. Missing soft
context remains `unavailable`; it never becomes fabricated support.

Strategy snapshots do not expose HTF zone records. Structural context is
constructed only after plugin evaluation, for emitted candidates, and is not
used as a strategy score. Alpha, compatibility, and shared-bus handoffs must
carry and verify a passing admission proof; direct intent writes without one
are rejected.

The normative contracts are `specs/structural-sl-admission-v5-nearest-zone.md`
and `specs/trade-admission-and-clash-resolution.md`.

The analyst publishes only after admission and routing. The shared SQLite bus
requires an explicit absolute `INTENT_BUS_DB` and target switches:

- `INTENT_BUS_BYBIT_ENABLED=true` enables Bybit delivery.
- `INTENT_BUS_PROPR_ENABLED=true` additionally enables Propr fan-out.

The executor owns credentials, sizing, leverage, venue precision, orders,
fills, protective stops, take-profit execution, and receipts. Analyst logs must
not claim execution state. The shared SQLite bus is the sole intent handoff.
Research Analyst contains no venue adapter, venue inbox writer, Telegram trade
signal publisher, or analyst-local LLM workflow. Those are outside this
repository's scope.

Alpha outbox events persist the admitted target in the top-level `targets`
field. The publisher can recover that field for legacy events when
`_admission_result.selected_take_profit` is present; events without a
recoverable target remain invalid and are not delivered.

The shared intent bus accepts JSON values only. The analyst bus publisher
normalizes nested `datetime` values to UTC ISO-8601 strings before validation,
delivery construction, and source persistence. Do not pass raw database or
Python datetime objects across that boundary.

`data/alpha_outbox/quarantine/` is not consumed by the active publisher. Clear
it only after auditing that every file is expired and belongs to a retired
strategy or invalid legacy schema. Preserve active top-level outbox events;
never clear the whole `data/alpha_outbox/` directory.

## Downstream Boundary

Research Analyst stops at validated shared-bus publication. Position
management, venue state, protection, hard exits, and execution are downstream
concerns and are not modeled or configured in this repository.

## Notification Ownership

Research Analyst never sends fills, positions, or execution confirmations.
Its Discord surface is advisory raw-signal batches only (fixed-width table,
display-truncated strategy names, canonical IDs unchanged). Trade entry/exit
cards belong exclusively to the venue executors and are sent only from
venue-confirmed state. Strategy evaluation is allowlist-gated
(`STRATEGY_ENABLED_IDS`, default the 7 vectorbt engine-handoff ports);
legacy/retired plugins stay registered for replay/research and can only emit
by explicit opt-in.

## Operations

Production services are managed by `oxmgr`. Do not start gateway, orchestrator,
strategy runner, regime worker, or rotation worker processes manually.

```bash
oxmgr list
oxmgr logs research-analyst-ws --lines 40
oxmgr logs research-analyst-symbol-rotation --lines 40
oxmgr logs research-analyst-regime-session --lines 40
oxmgr logs research-analyst-strategy-runner --lines 40
oxmgr logs research-analyst-orchestrator --lines 40
```

The symbol-rotation target uses `scripts/symbol_rotation_healthcheck.py`; the
probe requires the worker process and a `ready` or `fallback` performance feed.
The regime-session target uses `scripts/regime_session_healthcheck.py`; the
probe requires a running worker and a recent completed cycle with valid 1h/4h
readiness and gate summary fields. It accepts both timestamp-prefixed records
and long JSON records persisted without a prefix by `oxmgr`, using the cycle's
`cutoff_at` for freshness. Both tracked definitions are in `ops/oxfile.toml`;
the full worker invocations belong in each app's `command` field.

The core managed targets are:

- `research-analyst-symbol-rotation`
- `research-analyst-ws`
- `research-analyst-regime-session`
- `research-analyst-strategy-runner`
- `research-analyst-orchestrator`

Online database retention runs every six hours in bounded batches. Evaluation
coverage is retained for 7 days. Full compaction runs offline on Sunday at
04:30 UTC through `scripts/compact_databases.sh`; it stops the managed writers,
runs `wal_checkpoint(TRUNCATE)`, `VACUUM`, `PRAGMA optimize`, and integrity
checks, then restarts only services that were active before compaction.

When a code change is explicitly approved for deployment, restart only the
managed processes that import the changed code, then verify fresh cutoff logs,
restart counts, data freshness, regime persistence, pipeline completion, and
publisher state. Never alter `.env`, databases, executor files, or production
settings without an explicit command.

For a local observation-only PM2 run, set both intent-bus target switches to
`false`, verify `data/ws_health.json` and the cycle health files, and stop the
task-local PM2 home after the observation window. `pm2` is not the production
supervisor; production uses `oxmgr`.

## Verification

```bash
python3 -m pytest -q
python3 -m compileall -q src tests
```

Do not commit `.env`, API keys, webhooks, databases, or executor credentials.

## Local Agent CLI

The root `./cli.py` is the repository-local, JSON-only inspection interface;
its locked contract is `specs/LOCAL_CLI_SPEC.md`. It reads Research Analyst
databases and health artifacts read-only and never becomes an executor,
portfolio manager, or second worker.

- The CLI is enabled by default. Set `RESEARCH_ANALYST_LOCAL_CLI_ENABLED=false`
  to refuse every command.
- Use `./cli.py status`, `./cli.py health`, `./cli.py research ...`, and
  `./cli.py bus ...` for bounded machine-readable inspection.
- Service control is restricted to the allowlisted Research Analyst oxmgr
  targets; it never starts a daemon with a direct Python command.
- Research reports and delivery state must not be described as fills, open
  positions, or execution confirmation.
- `--pretty` changes indentation only. Timestamps are UTC `Z` values and
  sensitive values are redacted.
