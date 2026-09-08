# Research Analyst Agent Guide

**Last reviewed:** 2026-09-08

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
       alpha ledger and publisher
       -> shared SQLite intent bus, when enabled

 executor 1m position snapshots
   -> standalone-llm-pm (separate service)
        -> executor decision inbox
```

The gateway emits triggers only after market observations are committed. The
orchestrator claims triggers in cutoff order, uses a lease for crash recovery,
and marks a trigger processed only after the pipeline succeeds. Publisher
failures are separate from pipeline failures.

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
pending, running, and retryable work. `VACUUM` is separately throttled and must
not run from a second writer.

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

The production allowlist currently contains 12 plugins:

| Strategy | Cadence | Family | Route |
| --- | --- | --- | --- |
| `failed-break-v3` | 5m | reversal | Bybit Hyro |
| `bb-rsi-meanrev-v1` | 5m | mean_reversion | Bybit Hyro |
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

Compact Hyro strategies are policy-limited to `BTC`, `ETH`, `PAXG`, and `QQQ`.
The downstream router currently maps the EMA99 delivery to `bybit/fundamo`.
Propr fan-out is independent and enabled only by its shared-bus switch.

Production strategies use the repository's tested in-house EMA, RSI, ATR, ADX,
StochRSI, and Bollinger implementations. A TA library cannot replace them
without numerical parity tests and an explicit strategy-version change.

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

Every plugin candidate is first captured in `raw_signals`. Deterministic hard
admission then checks finite prices, freshness, expiry, trade geometry, reward
to risk, ATR-bounded stop distance, required data, symbol-account policy, and
structural-stop rules. Before scoring, admission reads completed direct 1h/4h
bars from regime-owned history only for assets that emitted candidates, builds
one reusable context per asset/cutoff/timeframe, and selects the newest eligible
4h zone before falling back to 1h. A long entry may be inside a bullish support
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

Strategy snapshots do not expose HTF zone records. Structural context is
constructed only after plugin evaluation, for emitted candidates, and is not
used as a strategy score. Alpha, compatibility, and shared-bus handoffs must
carry and verify a passing admission proof; direct intent writes without one
are rejected.

The normative contract is `specs/structural-sl-admission-v3.md`.

The analyst publishes only after admission and routing. The shared SQLite bus
requires an explicit absolute `INTENT_BUS_DB` and target switches:

- `INTENT_BUS_BYBIT_ENABLED=true` enables Bybit delivery.
- `INTENT_BUS_PROPR_ENABLED=true` additionally enables Propr fan-out.

The executor owns credentials, sizing, leverage, venue precision, orders,
fills, protective stops, take-profit execution, and receipts. Analyst logs must
not claim execution state. The shared SQLite bus is the sole intent handoff.

Alpha outbox events persist the admitted target in the top-level `targets`
field. The publisher can recover that field for legacy events when
`_admission_result.selected_take_profit` is present; events without a
recoverable target remain invalid and are not delivered.

## Position Management

LLM position management is owned by the separate `standalone-llm-pm` service.
Research Analyst publishes validated trade intents only and does not run a PM
loop or write executor position-decision files. The executor remains
authoritative for venue state, protection, hard exits, and execution.

## Operations

Production services are managed by `oxmgr`. Do not start gateway, orchestrator,
regime worker, or rotation worker processes manually. The standalone PM is
managed from its own repository.

```bash
oxmgr list
oxmgr logs research-analyst-ws --lines 40
oxmgr logs research-analyst-symbol-rotation --lines 40
oxmgr logs research-analyst-regime-session --lines 40
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
- `research-analyst-orchestrator`

When a code change is explicitly approved for deployment, restart only the
managed processes that import the changed code, then verify fresh cutoff logs,
restart counts, data freshness, regime persistence, pipeline completion, and
publisher state. Never alter `.env`, databases, executor files, or production
settings without an explicit command.

## Verification

```bash
python3 -m pytest -q
python3 -m compileall -q src tests
```

Do not commit `.env`, API keys, webhooks, databases, or executor credentials.
