# LLM Position-Management Sidecar Spec (ADR)

## Goal

An optional, emit-only sidecar that advises the executor on how to manage
**already-open** positions, following the originating strategy's direction. It
issues exactly three actions with a one-line reason. It never holds credentials,
never sizes, never selects venue, never places orders.

## Toggle

| Setting | Default | Notes |
| --- | --- | --- |
| `PM_SIDECAR_ENABLED` | `false` | Off by default; when off, evaluators run untouched. |
| `PM_CADENCE_MINUTES` | `5` | One evaluation pass per 5m bar. |
| `PM_LLM_TIMEOUT_S` | `20` | Hard bound per call. |
| `PM_LLM_RETRIES` | `1` | Bounded retry; on failure emit `hold` (safe default). |
| `PM_REASON_MAX_CHARS` | `120` | One-liner only. |

## Inputs (read-only to PM)

1. **`positions_feed`** — written by the executor, read-only to PM:
   `{ position_id, symbol, side, entry, size, opened_at, strategy_id, current_pnl }`.
2. **Active trade-intent** for that `strategy_id` — original `direction`,
   `invalidation_price`, `targets` (so PM knows the plan it must follow).
3. **HTF bias** — `structure_bias_4h` / `zone_bias_4h` from existing helpers.
4. **Swings** — HTF swing highs/lows (swing detector output on 1h/4h).
5. **RR** — current risk/reward vs entry and invalidation (computed locally).
6. **5m TA** — 5m microstructure (EMA/RSI/structure) at the 5m cadence.

## Processing (per 5m tick, per open position)

- Load position + its trade-intent + HTF bias + swings + RR + 5m TA.
- Call LLM with a strict schema prompt constraining output to:
  - `action ∈ {hold, exit, reduce}`
  - `reason` ≤ `PM_REASON_MAX_CHARS`
- On timeout/error/parse-fail → emit `hold` (do no harm).
- One advice per position per tick (no spam).

## Output contract

`pm_advice` (outbox → executor channel):

```json
{
  "schema_version": 1,
  "advice_id": "deterministic-uuid",
  "position_id": "executor-position-id",
  "strategy_id": "rsi-reclaim-v1",
  "action": "hold | exit | reduce",
  "reason": "<=120 chars",
  "observed_at": "2026-08-28T12:15:00Z",
  "htf_bias": "bullish | bearish | neutral",
  "rr": 1.8
}
```

The executor consumes `pm_advice` and makes the actual decision/order. PM output
is advisory only and cannot alter the deterministic trade-intent event.

## Why this keeps the repo boundary

- PM **reads** `positions_feed` (executor-owned) and **writes** only `pm_advice`.
- No credentials, no venue selection, no order submission — identical boundary to
  the existing (disabled) execution adapter.
- Three-action vocabulary (`hold`/`exit`/`reduce`) + one-liner is the minimal
  management surface: pure position *management* following strategy direction.

## Cadence note

5m cadence is deliberate: it is slow enough to avoid LLM cost/thrash, fast enough
to react within the strategy horizon, and aligns with the resampled 5m feed that
the ingestion layer already produces.
