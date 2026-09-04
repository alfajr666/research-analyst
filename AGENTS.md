# Agent Notes

**Last reviewed:** 2026-09-04

## Runtime Contract

- The WebSocket gateway owns `data/market.sqlite3` and publishes completed 5m
  evaluation triggers.
- The orchestrator consumes one trigger per completed 5m cutoff and writes
  selected intents to the shared executor inbox.
- The PM sidecar is a separate managed process from the orchestrator. It reads
  executor 1m snapshots and is the single LLM PM authority on a five-minute
  decision cadence, writing `HOLD`, `REDUCE`, `EXIT`, or `NEAR_TP` decisions to
  the executor's shared decision inbox.
- Shared handoff paths are anchored to `BYBIT_EXECUTOR_DIR`: `data/intents`,
  `data/position-snapshots`, and `data/position-decisions` are the same paths
  used by the executor. Do not create analyst-local copies.
- Market freshness is a hard admission gate. Missing data or data older than
  `DATA_FRESHNESS_MAX_SECONDS` (default 600) rejects the candidate.
- Health freshness queries must exclude open/future bars with `source_end <= now`.
- The scorer ranks candidates that pass all hard gates; it is not another gate.
- Never point both services at one database or run duplicate database writers.
- The gateway and orchestrator run periodic retention through
  `src/research_analyst/db_maintenance.py`. Market cleanup runs on the gateway
  writer connection; analyst cleanup runs on the orchestrator-owned connection.
- Retention may remove recomputable snapshots and terminal audit rows, but must
  retain active, pending, running, and retryable work. SQLite `VACUUM` is
  throttled separately from row deletion and must never run from a second
  writer process.
- `data/binance_oi.db` is a DuckDB file owned by the separate
  `binance-scanner-oi` project. Do not open, prune, or schedule maintenance for
  it from this repository; its owner must perform its own retention.
- Production strategies use the repository's tested in-house EMA, RSI, ATR,
  ADX, StochRSI, and Bollinger implementations. Do not replace them with a TA
  library without numerical parity tests and an explicit strategy-version
  change.

- The shared SQLite bus at `/home/ubuntu/shared/intent-bus/intent_bus.sqlite3`
  is the authoritative executor handoff. Publish only after admission and
  routing; never claim receipts or execution state in this repository.
- `INTENT_BUS_DB` must be an explicit absolute path and
  `INTENT_BUS_BYBIT_ENABLED` must be enabled for Bybit delivery. Live Propr
  fan-out additionally requires `INTENT_BUS_PROPR_ENABLED=true`. Legacy JSON
  inbox writing is compatibility-only and defaults off.
- Confidence calibration is outside the shared intent bus. A separate
  read-only calibrator may consume cross-producer event and market ledgers and
  publish versioned model artifacts without entering the 5m evaluation path.
- The seven Fundamo strategies route Bybit deliveries exclusively to
  `bybit/fundamo`; admitted events are independently fanned out to the Propr
  bus target when enabled. Propr owns its account routing, sizing, risk gates,
  and receipts.

## Production audit state (2026-08-30)

The daemon consumes completed gateway triggers, runs the pipeline, and invokes
the signal publisher after successful evaluation. Publisher failures are kept
separate from ingestion failures; the trigger is only processed after the
pipeline itself succeeds. `raw_signals` remains an observation ledger and the
30-minute Discord batch is non-blocking.

Snapshot positions without an originating intent are classified as
`strategy_id=unmanaged`. The PM sidecar emits a durable neutral `HOLD` advice
and decision for those positions without calling the LLM or creating an exit.
Normal positions retain their originating strategy metadata. First-write bus
delivery and execution handoff remain consumer-owned and must be verified from
the publisher/execution-delivery tables before enabling additional routes.
- LLM research review is disabled in the live analyst path; `research_context`
  is not required for strategy evaluation or intent delivery.
- Public alpha messages use the structured strategy/setup, evidence, trade-plan,
  risk, validity, and execution-disclaimer layout. Provisional confidence is
  retained internally for audit but is deliberately omitted from Discord and
  Telegram until an out-of-sample calibration model is approved.

## Production rollout state (2026-09-01)

- The live evaluation allowlist contains 11 strategies: four compact Hyro
  strategies and seven Fundamo strategies.
- The three newly enabled Fundamo strategies are
  `gold-trend-ema-bb-stoch-v1`, `mtf-exhaustion-reversal-v1`, and
  `trend-wall-v1`. They are registered, cadence-controlled at 5m, and
  forcibly routed downstream to `bybit/fundamo`.
- After restarting `research-analyst-ws`,
  `research-analyst-orchestrator`, and `research-analyst-pm-sidecar` through
  `oxmgr`, the 05:20 and 05:25 UTC 5m cutoffs completed across 34 subscribed
  symbols. All three new strategies completed evaluation in both cutoffs.
- The services were healthy with zero new restarts after verification. The
  new strategies emitted no candidates during those two cutoffs, so their
  actual executor delivery was not exercised by a live signal.
- The signal publisher still reports one invalid event per observed cycle from
  malformed pre-existing alpha outbox files missing internal required fields
  such as `confidence`, `entry_condition`, and `horizon_minutes`. This is
  isolated from pipeline completion; confidence is not a public message field,
  and publisher state is not a successful delivery receipt.

## Production diagnosis and verification (2026-09-01)

- A no-intent period was traced to 5m WebSocket bars stamped one millisecond
  before their closed-bar boundary, for example `14:44:59.999`. The local
  resampler matched exact timestamps and discarded every derived 4h bucket, so
  `ATR14_4h` was unavailable and permitted candidates failed hard admission.
- `resample_ohlcv` now normalizes those timestamps before bucket matching, with
  a regression test covering the feed encoding. The orchestrator was restarted
  through `oxmgr` and loaded the fix; the 14:45 UTC cycle wrote an `SPX long`
  Fundamo intent and Bybit accepted its delivery.
- Raw candidates from compact Hyro strategies can still be captured for any
  subscribed symbol, but only `BTC`, `ETH`, `PAXG`, and `QQQ` may pass the Hyro
  symbol-account policy. QQQ also requires 14 complete 4h bars before its ATR
  risk input is available.
- Propr delivery and order recovery remain executor-owned. A Propr `RETRY`
  receipt is not evidence of an analyst pipeline failure or a new analyst
  intent.

## Verification

Run `python3 -m pytest -q` and `git diff --check` before committing. Production
services are managed with `oxmgr`; do not start duplicate gateway, orchestrator,
or PM sidecar processes manually.
