# Research Analyst Agent Guide

**Last reviewed:** 2026-09-04

This repository is a read-and-decide market research service. It produces
auditable candidates and validated trade intents. It does not hold exchange
credentials, size positions, place orders, or claim that an intent was filled.

## Runtime Contract

```text
Bybit public tickers
  -> symbol-rotation worker
  -> versioned performance feed
  -> ws_gateway subscriptions
       Bybit public WS: completed 1m + 5m bars
       startup/re-entry REST backfill
       local 5m -> 15m/1h/4h resampling
       -> data/market.sqlite3
       -> durable 1m/5m evaluation triggers

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
       deterministic admission and clash resolution
       alpha ledger and publisher
       -> shared SQLite intent bus, when enabled

executor 1m position snapshots
  -> independent PM sidecar
       HOLD / REDUCE / EXIT / NEAR_TP
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
  orchestrator reads it for the exact evaluation cutoff.
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
inputs, reads regime-exclusive direct 1h and 4h history from `regime.sqlite3`,
computes ADX and regime inputs, and persists an immutable score and gate
decision. Strategy-facing 1h/4h bars remain the gateway's 5m-derived
market-data view.

The score is a research ranking input. Data readiness is a hard admission
condition. The in-house ADX implementation requires 57 complete 1h and 4h bars
with the default length and smoothing of 14. During warmup,
`status=insufficient_data` and `regime_score_insufficient_data` are expected and
must fail closed. Do not lower the requirement or fabricate higher-timeframe
data.

The regime worker fetches 4 calendar days of completed Bybit linear-perpetual 1h
candles and 15 calendar days of completed 4h candles for a new or re-entering
asset, retaining at least 3 complete 1h days and 14 complete 4h days. It stores
them in `regime_1h_bars` and `regime_4h_bars` in `regime.sqlite3`. It owns the
durable interval-specific backfill job state, validates duplicates and gaps,
and retries failed assets independently. Direct 1h/4h history is regime-
exclusive; strategy plugins continue to use the gateway's 5m-derived views.

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

## Live Strategy Set

The production allowlist currently contains 11 plugins:

| Strategy | Cadence | Family | Route |
| --- | --- | --- | --- |
| `failed-break-v3` | 5m | reversal | Bybit Hyro |
| `bb-rsi-meanrev-v1` | 5m | mean_reversion | Bybit Hyro |
| `williams-fractal-scalp-v1` | 1m | trend | Bybit Hyro |
| `ema9-adx-stochrsi-state-v1` | 1m | trend | Bybit Hyro |
| `dual-zone-follower-v2` | 5m | trend | Bybit Fundamo |
| `dual-zone-short-follower-v2` | 5m | trend | Bybit Fundamo |
| `ema20-pullback-h4-trend-v1` | 5m | trend | Bybit Fundamo |
| `gold-trend-ema-bb-stoch-v1` | 5m | trend | Bybit Fundamo |
| `mtf-exhaustion-reversal-v1` | 5m | reversal | Bybit Fundamo |
| `ema99-double-touch-stochrsi-state-v1` | 5m | trend | Bybit Fundamo |
| `ema7-26-cross-hammer-shooting-star-1h-adx-v1` | 5m | reversal | Bybit Fundamo |

Compact Hyro strategies are policy-limited to `BTC`, `ETH`, `PAXG`, and `QQQ`.
The seven Fundamo strategies route to `bybit/fundamo`. Propr fan-out is
independent and enabled only by its shared-bus switch.

Production strategies use the repository's tested in-house EMA, RSI, ATR, ADX,
StochRSI, and Bollinger implementations. A TA library cannot replace them
without numerical parity tests and an explicit strategy-version change.

## Admission And Delivery

Every plugin candidate is first captured in `raw_signals`. Deterministic hard
admission then checks finite prices, freshness, expiry, trade geometry, reward
to risk, ATR-bounded stop distance, required data, symbol-account policy, and
structural-stop rules when enabled. Soft context scores rank candidates but
cannot rescue a failed hard gate. Clash resolution is deterministic and
unresolved opposing signals produce no intent.

The analyst publishes only after admission and routing. The shared SQLite bus
requires an explicit absolute `INTENT_BUS_DB` and target switches:

- `INTENT_BUS_BYBIT_ENABLED=true` enables Bybit delivery.
- `INTENT_BUS_PROPR_ENABLED=true` additionally enables Propr fan-out.
- `INTENT_BUS_LEGACY_INBOX_ENABLED=false` keeps legacy JSON inbox writing off.

The executor owns credentials, sizing, leverage, venue precision, orders,
fills, protective stops, take-profit execution, and receipts. Analyst logs must
not claim execution state. The legacy filesystem inbox is compatibility-only.

## PM Sidecar

The PM sidecar is a separate managed process and the sole LLM position manager.
It reads executor 1m snapshots from paths under `BYBIT_EXECUTOR_DIR`, joins
originating intent and market context, and writes durable decision files under
the executor's `data/position-decisions` directory. `HOLD` is neutral. Action
decisions are confidence-gated and valid for five minutes. The sidecar cannot
change entry, stop, target, direction, sizing, or hard protections.

Positions without an originating intent are `strategy_id=unmanaged`; they
receive a durable neutral `HOLD` without an LLM call or an automated exit.

## Operations

Production services are managed by `oxmgr`. Do not start gateway, orchestrator,
regime worker, rotation worker, or PM sidecar processes manually.

```bash
oxmgr list
oxmgr logs research-analyst-ws --lines 40
oxmgr logs research-analyst-symbol-rotation --lines 40
oxmgr logs research-analyst-regime-session --lines 40
oxmgr logs research-analyst-orchestrator --lines 40
oxmgr logs research-analyst-pm-sidecar --lines 40
```

The core managed targets are:

- `research-analyst-symbol-rotation`
- `research-analyst-ws`
- `research-analyst-regime-session`
- `research-analyst-orchestrator`
- `research-analyst-pm-sidecar`

When a code change is explicitly approved for deployment, restart only the
managed processes that import the changed code, then verify fresh cutoff logs,
restart counts, data freshness, regime persistence, pipeline completion, and
PM decisions. Never alter `.env`, databases, executor files, or production
settings without an explicit command.

## Verification

```bash
python3 -m pytest -q
python3 -m compileall -q src tests
```

Do not commit `.env`, API keys, webhooks, databases, or executor credentials.
