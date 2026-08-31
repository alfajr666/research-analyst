# LLM Position-Management Sidecar Spec (Locked ADR)

## Status

Locked design. This document supersedes the mechanical-veto design in
`specs/mechanical-exit-sidecar-v1.md`. Runtime changes are a follow-up
implementation task and must preserve this contract.

The detailed implementation blueprint is
`specs/pm-sidecar-llm-only-v1.md`.

## Goal

An optional, emit-only sidecar that advises the executor on how to manage
**already-open** positions, following the originating strategy's direction. It
issues one of four semantic decisions with a one-line reason. It never holds
credentials, never sizes, never selects venue, never places orders.

The four decisions are:

- `hold`: no management action.
- `reduce`: reduce current exposure now.
- `exit`: close the current position now.
- `near_tp`: hold the runner and authorize one executor-owned reduction when
  the venue mark is within five ticks of the immutable original TP.

## Toggle

| Setting | Default | Notes |
| --- | --- | --- |
| `PM_SIDECAR_ENABLED` | `false` | Off by default; when off, evaluators run untouched. |
| `PM_CADENCE_MINUTES` | `5` | One evaluation pass per completed 5m cutoff. Values below 5 are clamped to 5. |
| `PM_LLM_TIMEOUT_S` | `20` | Hard bound per call. |
| `PM_LLM_RETRIES` | `1` | Bounded retry; on failure emit `hold` (safe default). |
| `PM_REASON_MAX_CHARS` | `120` | One-liner only. |
| `PM_DECISION_VALIDITY_MINUTES` | `5` | Decision expiry; prevents stale inbox actions. |
| `PM_ACTION_CONFIDENCE` | `0.70` | Minimum confidence for `reduce`, `exit`, and `near_tp`. |

## Inputs (read-only to PM)

1. **`positions_feed`** — written by the executor, read-only to PM:
   `{ position_id, symbol, side, entry, size, opened_at, strategy_id, current_pnl }`.
2. **Active trade-intent** for that `strategy_id` — original `direction`,
   `invalidation_price`, `targets` (so PM knows the plan it must follow).
3. **HTF bias** — `structure_bias_4h` / `zone_bias_4h` from existing helpers.
4. **Swings** — HTF swing highs/lows (swing detector output on 1h/4h).
5. **RR** — current risk/reward vs entry and invalidation (computed locally).
6. **5m TA** — 5m microstructure (EMA/RSI/structure) as market context for each 5m pass.

## Processing (per 5m tick, per open position)

- Load position + its trade-intent + HTF bias + swings + RR + 5m TA.
- Call LLM with a strict schema prompt constraining output to:
  - `action ∈ {hold, exit, reduce, near_tp}`
  - `confidence` as a finite number in `[0, 1]`
  - `reason` ≤ `PM_REASON_MAX_CHARS`
- `hold` does not require confidence and has no execution effect.
- `reduce`, `exit`, and `near_tp` require confidence at or above
  `PM_ACTION_CONFIDENCE`; otherwise normalize to `hold` and record the
  threshold rejection.
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
  "action": "near_tp",
  "confidence": 0.82,
  "reason": "<=120 chars",
  "observed_at": "2026-08-28T12:15:00Z",
  "htf_bias": "bullish | bearish | neutral",
  "rr": 1.8
}
```

The executor consumes `pm_advice` and makes the actual decision/order. PM output
is advisory only and cannot alter the deterministic trade-intent event.

## Protected Entry Dependency

Every delivered intent must contain a concrete take-profit before it reaches the
executor. The producer preserves an explicit strategy target; when a strategy has
no target, it derives a 2R target from the known entry reference and stop:

```text
LONG:  entry + 2 * abs(entry - stop)
SHORT: entry - 2 * abs(entry - stop)
```

The producer labels the target source as `strategy_target` or
`producer_derived_2r`. The executor validates geometry, attaches the supplied
SL/TP, enforces venue safety, and does not derive strategy policy.

`near_tp` is action-bearing even though it preserves the runner. It requires the
confidence threshold and is valid only when all executor-confirmed conditions
hold:

- `original_take_profit` and venue tick size are known;
- the venue mark is within five ticks of `original_take_profit` without crossing;
- the universal `1.5R` partial is confirmed, when that executor phase is enabled;
- the near-TP reduction has not already been confirmed for this position;
- the reduction uses current venue quantity and executor minimum-size rules.

The executor owns the near-TP trigger check, reduction, protection update, and
one-time lifecycle journal. A normal `hold` must not implicitly invoke this
behavior.

## Why this keeps the repo boundary

- PM **reads** `positions_feed` (executor-owned) and **writes** only `pm_advice`.
- No credentials, no venue selection, no order submission — identical boundary to
  the existing (disabled) execution adapter.
- Four-action vocabulary (`hold`/`exit`/`reduce`/`near_tp`) + one-liner is the minimal
  management surface: pure position *management* following strategy direction.

## Cadence note

The decision validity is five minutes. The implementation should use a five-
minute decision cadence unless the executor explicitly supersedes older
decisions per position. Venue SL/TP remain active independently of the sidecar.
