# Research Analyst

**Last reviewed:** 2026-09-04

Research Analyst is a read-and-decide market research service. It consumes
public market data, evaluates versioned strategy plugins, records auditable
candidates, and publishes validated trade intents when delivery is enabled.
It does not hold exchange credentials, size positions, place orders, or claim
that an intent was filled.

## Current Flow

```text
Bybit public tickers
  -> symbol-rotation worker
  -> versioned performance feed
  -> ws_gateway
       Bybit public WS: completed 1m + 5m bars
       startup/re-entry REST backfill
       local 5m -> 15m/1h/4h resampling
       -> data/market.sqlite3
       -> durable 1m/5m evaluation triggers

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
       hard admission
       deterministic clash resolution
       alpha ledger + publisher
       -> shared SQLite intent bus

executor 1m position snapshots
  -> independent PM sidecar
       HOLD / REDUCE / EXIT / NEAR_TP
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
| `research-analyst-pm-sidecar` | LLM position-management decisions | Executor decision inbox |

All production services are managed by `oxmgr`. Never start a second gateway,
regime worker, orchestrator, or PM sidecar manually.

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

Bybit is the production public source. The gateway streams completed `1m` and
`5m` bars and locally derives strategy-facing `15m`, `1h`, and `4h` bars from
completed `5m` observations. The derived bars are not a separate
live-ingestion dependency. The regime worker separately reads completed direct
Bybit REST `4h` candles into `data/regime.sqlite3`; it does not add a WebSocket
topic or replace the strategy-facing 4h view.

Closed bars may arrive with an end timestamp one millisecond before the
boundary, such as `14:44:59.999` for the `14:45` bar. The resampler normalizes
that representation before exact bucket matching.

The performance rotation feed refreshes every four hours. It contains 30
rotating assets, normally 15 gainers and 15 losers, plus permanent `BTC`,
`ETH`, `PAXG`, and `QQQ`. Fresh open-position assets are carried into the
subscription set when needed.

Canonical asset names are preserved throughout the pipeline. For example,
`ANKRUSDT` maps to `ANKR` and `MARSCOINUSDT` maps to `MARSCOIN`; bare asset
names are never truncated.

## Regime Session

The regime worker runs once per completed `5m` cutoff for the current
subscription feed. For each asset it loads completed `5m` observations for
realized-volatility inputs, reads regime-exclusive direct `1h` and `4h` history
from `data/regime.sqlite3`, computes in-house ADX and regime inputs, then
persists an immutable score and gate decision. Strategy-facing `1h`/`4h` bars
remain the gateway's `5m`-derived market-data views.

The default ADX length and smoothing are both 14. This implementation requires
57 complete `1h` and `4h` bars before the score is data-ready. During warmup,
the score is `insufficient_data` and the reason is
`regime_score_insufficient_data`. This is expected fail-closed behavior. Do not
reduce the requirement or invent higher-timeframe bars. New or re-entering
assets fetch 4 calendar days of completed direct Bybit `1h` history and 15
calendar days of completed direct Bybit `4h` history, retaining at least 3
complete 1h days and 14 complete 4h days. Gaps, duplicates, malformed candles,
stale data, and missing exact-cutoff evidence block only the affected asset.

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

The current production allowlist contains 11 plugins:

| Strategy | Cadence | Family | Account |
| --- | --- | --- | --- |
| `failed-break-v3` | 5m | reversal | Hyro |
| `bb-rsi-meanrev-v1` | 5m | mean_reversion | Hyro |
| `williams-fractal-scalp-v1` | 1m | trend | Hyro |
| `ema9-adx-stochrsi-state-v1` | 1m | trend | Hyro |
| `dual-zone-follower-v2` | 5m | trend | Fundamo |
| `dual-zone-short-follower-v2` | 5m | trend | Fundamo |
| `ema20-pullback-h4-trend-v1` | 5m | trend | Fundamo |
| `gold-trend-ema-bb-stoch-v1` | 5m | trend | Fundamo |
| `mtf-exhaustion-reversal-v1` | 5m | reversal | Fundamo |
| `ema99-double-touch-stochrsi-state-v1` | 5m | trend | Fundamo |
| `ema7-26-cross-hammer-shooting-star-1h-adx-v1` | 5m | reversal | Fundamo |

Compact Hyro strategies are limited to `BTC`, `ETH`, `PAXG`, and `QQQ`.
Fundamo Bybit deliveries route to `bybit/fundamo`. Propr fan-out is an
independent shared-bus target.

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
- structural-stop rules when enabled.

Soft context scores rank candidates but cannot rescue a failed hard gate.
Opposing candidates are resolved deterministically; an unresolved clash emits
no intent. Missing or stale data is rejected by admission, not disguised as a
score.

## Intent Delivery

The analyst publishes only after admission and routing. The shared SQLite bus
requires an explicit absolute `INTENT_BUS_DB`:

- `INTENT_BUS_BYBIT_ENABLED=true` enables Bybit delivery.
- `INTENT_BUS_PROPR_ENABLED=true` enables independent Propr fan-out.
- `INTENT_BUS_LEGACY_INBOX_ENABLED=false` keeps the compatibility JSON inbox off.

The analyst sends thesis and trade-plan fields only. The executor owns
credentials, sizing, leverage, precision, orders, fills, protective stops,
take-profit execution, and receipts. Research Analyst never claims execution
state.

## PM Sidecar

The PM sidecar is independent from the orchestrator and is the sole LLM
position-management authority. It reads executor 1m snapshots, joins the
originating intent and market context, and writes durable decision files under
the executor's `data/position-decisions` directory.

`HOLD` is neutral. `REDUCE`, `EXIT`, and `NEAR_TP` are confidence-gated and
valid for five minutes. The sidecar cannot change entry, stop, target,
direction, sizing, or hard protections. Positions without an originating
intent use `strategy_id=unmanaged` and receive a durable neutral `HOLD` without
an LLM call.

## Operations

```bash
oxmgr list
oxmgr logs research-analyst-symbol-rotation --lines 40
oxmgr logs research-analyst-ws --lines 40
oxmgr logs research-analyst-regime-session --lines 40
oxmgr logs research-analyst-orchestrator --lines 40
oxmgr logs research-analyst-pm-sidecar --lines 40
```

For a deployment of explicitly approved code, restart only services importing
the changed modules. Verify fresh cutoff logs, restart counts, market freshness,
regime persistence, pipeline completion, publisher state, and PM decisions.

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
