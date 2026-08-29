# Research Analyst

A discovery + strategy-evaluation engine for crypto USDT perpetuals. It turns warmed
market data into **advisory alpha events** (Discord) and **executor trade-intent
envelopes** (`schema_version=1` JSON consumed by `bybit-executor`). It never holds
exchange credentials, sizes, or places orders — those are the executor's domain.

```text
Analyst:  market data -> discovery -> strategy eval -> alpha event  (Discord, advisory)
                                                 \-> trade intent (shared inbox -> bybit-executor)
Executor:                                         intent -> risk -> sizing -> order -> position lifecycle
```

## Architecture

```text
Bybit WS (on) / Binance WS (off): 1m + 5m kline + markPrice
                               |
                               v
                     ws_gateway  (resamples 15m/1h/4h from 5m)
                               |   source_observations  (single writer)
                               v
                  orchestrator  (sole writer of DB_PATH)
       strategy plugins -> alpha_outbox (advisory) -- Discord
                        \-> intent_outbox -- INTENT_INBOX -- bybit-executor
       + pruning (tiered per-interval), rotation feed, PM sidecar (emit-only)
```

- **Universe:** static 97-symbol list (`symbols/static_universe.json`); optional
  Binance OI rotation members via `WS_SYMBOL_SOURCE=rotated|both`.
- **Ingestion:** Bybit WebSocket only for live evaluation; Binance and CoinAnalyze
  live ingestion are off. `1m`+`5m` are streamed and `15m/1h/4h` are resampled
  locally. CoinAnalyze remains available only for historical tooling when explicitly
  enabled with `COINANALYZE_EVAL_ENABLED=true`.
- **Strategies:** the four compact ports (`failed-break-v3`, `bb-rsi-meanrev-v1`,
  `williams-fractal-scalp-v1`, `ema9-continuation-stochrsi-v1`) are the default
  enabled set, restricted to BTC/ETH/PAXG/QQQ. Enabled via `STRATEGY_ENABLED_IDS`
  and runtime-controlled via `STRATEGY_ACTIVE_IDS`.
- **Outboxes:** advisory alpha event → `data/alpha_outbox/` → Discord; trade intent
  → `INTENT_INBOX` → `bybit-executor`.

Reference notes and historical design material live under `docs/reference/`; runtime
Python entrypoints intentionally remain at the repository root because PM2 and the
current import graph execute them as top-level scripts.

## Trade-intent handoff (no guessing)

The analyst writes `INTENT_INBOX`; `bybit-executor` reads it. One knob resolves the
shared path to the executor's own default inbox:

```bash
BYBIT_EXECUTOR_DIR=/home/ubuntu/bybit-executor
INTENT_DELIVERY_ENABLED=true
```

`INTENT_INBOX` then defaults to `<BYBIT_EXECUTOR_DIR>/data/intents`. If the executor
overrides its `INTENT_INBOX`, set the analyst's `INTENT_INBOX` to the same absolute
path. **The intent carries no sizing** — the executor sizes from its account profile
(`risk.risk_amount`). Geometry (`stop_loss < entry_price < take_profit` for LONG) is
validated before delivery; invalid events are skipped.

## Setup

```bash
cp .env.example .env
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python config.py          # create tables / apply migrations
```

Set `DISCORD_ALPHA_WEBHOOK_URL` for advisory signals. For executor delivery, set
`BYBIT_EXECUTOR_DIR` and `INTENT_DELIVERY_ENABLED=true`.

## Running

```bash
./venv/bin/python ws_gateway.py &     # ingestion (single writer of source_observations)
./venv/bin/python orchestrator.py     # eval + delivery loop (single writer of DB_PATH)
```

One-off evaluation cycle:

```bash
./venv/bin/python orchestrator.py --once
```

## Live Setup Checklist

Run the following in order, starting in shadow/paper mode:

1. Configure `/home/ubuntu/bybit-executor` with the production Bybit profile,
   credentials, leverage, minimum quantity, and reconciliation settings.
2. Set `BYBIT_EXECUTOR_DIR`, `INTENT_DELIVERY_ENABLED=true`, and verify the shared
   `data/intents` directory.
3. Keep `STRATEGY_ACTIVE_IDS` limited to the selected compact strategy IDs. Compact
   ports only evaluate `BTC`, `ETH`, `PAXG`, and `QQQ`.
4. Confirm `EVAL_INTERVALS=1m,5m,15m`; execution cadence is strategy-specific.
5. Configure 9router with `LLM_BASE_URL`, `LLM_MODEL`, and `LLM_API_KEY`; enable
   `PM_SIDECAR_ENABLED=true` only after snapshot/decision handoff is verified.
6. Start the executor, then `ws_gateway.py`, then `orchestrator.py`.
7. Verify: fresh snapshots, accepted intents, protective SL/TP, PM decisions,
   idempotent replay, and expired-intent rejection.
8. Enable live orders only after the complete paper path is stable.

The analyst never stores exchange credentials, sizes positions, or overrides the
executor's protective SL/TP. Every delivered limit intent must pass valid geometry,
minimum 2R, and SL-distance admission.

## Key Configuration (see `.env.example`)

### Compact Strategy Ports

The compact-mode ports are restricted to `BTC`, `ETH`, `PAXG`, and `QQQ` only:

| Strategy | Execution timeframe | Context |
| --- | --- | --- |
| `failed-break-v3` | 5m | 15m resampled to 4h |
| `bb-rsi-meanrev-v1` | 5m | none |
| `williams-fractal-scalp-v1` | 1m | none |
| `ema9-continuation-stochrsi-v1` | 1m trigger | 5m setup |

These IDs are opt-in through `STRATEGY_ENABLED_IDS`. Their alpha events use the
unchanged TradeIntent contract and still require valid SL geometry and minimum 2R.
TA-derived targets are preserved when they meet 2R; otherwise the event remains
advisory-only and is not delivered to the executor.

| Variable | Purpose | Default |
| --- | --- | --- |
| `WS_BYBIT_ENABLED` / `WS_BINANCE_ENABLED` | WS sources | `true` / `false` |
| `WS_STREAM_TIMEFRAMES` | Bars streamed (5m base for resampling) | `1m,5m` |
| `WS_SYMBOL_SOURCE` | `static` / `rotated` / `both` | `static` |
| `EVAL_INTERVALS` | Strategy eval cutoffs | `1m,5m,15m` |
| `STRATEGY_ENABLED_IDS` | Registered plugins | v1+v2+rsi (+lsr opt-in) |
| `STRATEGY_ACTIVE_IDS` | Runtime-active subset (empty = all) | *(empty)* |
| `BYBIT_EXECUTOR_DIR` | Executor repo root → resolves `INTENT_INBOX` | *(empty)* |
| `INTENT_DELIVERY_ENABLED` | Emit executor trade intents | `false` |
| `INTENT_EXCHANGE_ID` / `INTENT_ACCOUNT_ID` | Executor profile | `bybit` / `account_a` |
| `INTENT_ORDER_TYPE` | `limit` (IOC) / `market` | `limit` |
| `INTENT_VALIDITY_MINUTES` | Entry validity window | `5` |
| `INTENT_MIN_RR` | Minimum limit-entry reward/risk | `2.0` |
| `INTENT_MIN_STOP_DISTANCE_PCT` / `INTENT_MAX_STOP_DISTANCE_PCT` | Stop distance admission bounds | `0.001` / `0.05` |
| `INTENT_ROUTING` | Per-strategy `exchange_id`/`account_id` overrides (JSON) | *(empty)* |
| `PM_SIDECAR_ENABLED` | Emit-only PM advice | `false` |
| `EXECUTOR_SNAPSHOT_DIR` | Executor 1m position snapshots consumed by PM | derived from `BYBIT_EXECUTOR_DIR` |
| `EXECUTOR_DECISION_DIR` | PMDecision files consumed by executor | derived from `BYBIT_EXECUTOR_DIR` |
| `PM_REDUCE_FRACTION` | Fraction used for `REDUCE` decisions | `0.5` |
| `PM_DECISION_VALIDITY_MINUTES` | Executor PM decision validity window | `30` |
| `PRUNE_1M_DAYS`…`PRUNE_4H_DAYS` | Tiered `source_observations` retention | `7/30/90/365/365` |
| `ROTATION_FEED_ENABLED` | Export OI rotation members to universe | `false` |

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No intents delivered | `INTENT_DELIVERY_ENABLED`, `BYBIT_EXECUTOR_DIR`/`INTENT_INBOX` matches executor, `STRATEGY_ACTIVE_IDS`, geometry valid |
| Intent skipped for admission | Check entry/SL/TP geometry, minimum 2R, and SL distance bounds |
| PM has no positions/decisions | Executor snapshot and decision directories match `POSITION_SNAPSHOT_DIR`/`POSITION_DECISION_DIR`; `PM_SIDECAR_ENABLED=true` |
| No advisory signal | `DISCORD_ALPHA_WEBHOOK_URL`, `alpha_events`, `INTENT_INBOX` not relevant |
| Stale/empty bars | `ws_gateway` running & sole writer; fresh completed `source_observations` |
| Duplicate writers | Only one `ws_gateway`, one `orchestrator` per DB |

## Research Constraints

Treat each setup class and plugin version as an independent hypothesis. Evaluate with
point-in-time universes, immutable candidate records, walk-forward splits,
asset-relative normalization, liquidity-tier/regime reporting, conservative
fees/spread/slippage/funding, and matched baselines. Discovery ranks, confidence
scores, LLM commentary, and delivery records are **not** evidence of alpha or
execution authorization.
