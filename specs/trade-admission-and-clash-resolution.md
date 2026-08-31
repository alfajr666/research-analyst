# Trade Admission and Clash Resolution

## Status

Accepted and locked by the operator on 2026-08-29.

This specification defines the boundary between strategy validity, execution
admission, and multi-strategy conflict resolution. It supersedes conflicting
language in earlier confluence documents. It is a design contract; implementation
must follow it without silently adding contextual hard gates.

## Decision Summary

```text
strategy candidates
        |
        v
hard execution admission
  SL / TP / RR / distance / expiry
        |
        v
eligible candidates
        |
        v
context scoring and clash resolution
  HTF bias / swings / FVG / OB / strategy evidence
        |
        v
selected intent, advisory conflict, or no intent
```

The hard gate answers: **Can this candidate be executed safely?**

The scoring layer answers: **Which safe candidate should win when strategies
disagree or agree on the same symbol?**

## Scope

This policy applies to the four live compact strategies:

- `failed-break-v3`
- `bb-rsi-meanrev-v1`
- `williams-fractal-scalp-v1`
- `ema9-continuation-stochrsi-v1`

It applies before intent delivery to the exclusively configured `bybit / hyro`
executor route. Advisory alpha events may retain candidates that do not become
executor intents.

## Candidate Contract

Every strategy candidate must provide:

- `strategy_id`
- `asset` and `direction`
- `observed_at` and `valid_until`
- entry condition and finite entry price
- finite invalidation/stop price
- at least one finite take-profit price
- strategy evidence and feature snapshot

Context evidence is optional for admission. A missing HTF bias, swing, FVG, or
order block is represented as `unavailable`, not as an automatic rejection.

## Hard Execution Gates

Only the following categories can suppress executor intent.

### Directional geometry

For a long candidate:

```text
stop_loss < entry_price < take_profit
```

For a short candidate:

```text
take_profit < entry_price < stop_loss
```

The selected take-profit must be the target used for admission. Multi-target
selection must be deterministic before this gate is evaluated.

### Reward/risk

```text
risk   = abs(entry_price - stop_loss)
reward = abs(take_profit - entry_price)
RR     = reward / risk
```

The candidate passes only when:

```text
RR >= INTENT_MIN_RR
```

The deployment default is `INTENT_MIN_RR=2.0`.

### Stop distance

The absolute stop distance relative to entry must remain inside configured
limits. The deployment defaults are a minimum of the greater of `0.1%` and
`0.25 * ATR14_4H` (configurable) and a `5%` maximum. See
`specs/atr-based-stop-admission.md` for the detailed contract.

### Time and data validity

- Entry expiry must be in the future at admission time.
- Prices must be finite and positive.
- The event identity and deduplication key must be valid and deterministic.
- Required strategy-local datasets must exist and be point-in-time safe.

Strategy-local requirements are allowed when the strategy cannot mathematically
operate without them. Contextual evidence is not a strategy-local hard gate.

### Hard-gate result

The admission result must be explicit:

```json
{
  "hard_gate": "pass",
  "hard_gate_reasons": [],
  "rr": 2.4,
  "stop_distance_pct": 0.008
}
```

Failure remains auditable and advisory-only:

```json
{
  "hard_gate": "fail",
  "hard_gate_reasons": ["reward/risk below minimum"],
  "executor_intent": false
}
```

## Soft Scoring

The following are scoring inputs, never unconditional execution gates:

- HTF directional bias
- confirmed swing structure
- FVG proximity and directional support
- order-block proximity and directional support
- LTF/HTF alignment
- strategy-specific evidence quality
- data freshness
- same-symbol strategy agreement
- contradiction penalties

Each input must produce a bounded component and a status of `support`,
`neutral`, `contradict`, or `unavailable`. `unavailable` must not be converted
into fabricated support or contradiction.

The score is additive and explainable:

```text
score = strategy_component
      + htf_bias_component
      + swing_component
      + fvg_component
      + order_block_component
      + alignment_component
      + freshness_component
      + agreement_component
      - contradiction_penalty
```

Weights are configuration or versioned policy, not ad hoc code branches. The
score ranks eligible candidates; it cannot rescue a failed hard gate.

The score is not a probability, conviction guarantee, sizing input, or order
authorization. Event confidence remains `uncalibrated` unless an offline,
versioned calibrator explicitly changes that status.

## Clash Resolution

Clash resolution operates only on candidates that pass hard admission.

### Same symbol, same direction

- Rank by total score.
- Select the highest score deterministically.
- Break exact ties by strategy priority, then `strategy_id` lexical order.
- Preserve every losing candidate as suppressed evidence.

The default result is one executor intent per symbol and direction. A future
portfolio policy may impose a stricter symbol-level mutex, but it must be
declared separately from this scoring policy.

### Same symbol, opposite directions

- Rank the long and short eligible candidates independently.
- Compute `score_margin = winner_score - loser_score`.
- If `score_margin >= CLASH_MIN_SCORE_MARGIN`, select the winner.
- If the margin is below the threshold, emit an advisory conflict and produce
  no executor intent.

The initial deployment default is `CLASH_MIN_SCORE_MARGIN=2.0`. This threshold
is a clash-resolution policy, not a candidate-validity gate.

### One candidate only

A single candidate that passes hard admission may be delivered even when its
context score is neutral, contradictory, or partially unavailable. Context
affects ranking and annotation, not basic eligibility.

## Persistence and Audit

The analyst ledger must retain:

- every candidate and hard-gate result
- every score component and component status
- selected candidate ID, if any
- suppressed candidate IDs and suppression reason
- conflict group key: `asset + cutoff`
- score policy/version and hard-gate policy/version
- final executor-intent decision

Advisory events must distinguish:

- `hard_gate_failed`
- `eligible_suppressed_by_same_direction_rank`
- `eligible_suppressed_by_opposite_direction_clash`
- `selected_for_executor`
- `advisory_only`

No candidate may be silently discarded because contextual evidence was missing
or unfavorable.

## Safety Boundaries

The following remain outside this resolver:

- quantity and risk sizing
- leverage and venue precision
- account selection beyond the fixed `bybit / hyro` route
- order placement, fills, and position lifecycle
- hard stop-loss and fixed take-profit enforcement
- LLM authority over deterministic event fields

The LLM PM sidecar may manage an active position with `HOLD`, `REDUCE`, or
`EXIT`, but it cannot weaken executor hard-stop or fixed-take-profit protections.

## Test Requirements

Tests must prove:

1. Invalid long and short geometry cannot create executor intents.
2. RR below the configured minimum cannot create executor intents.
3. HTF bias disagreement does not reject an otherwise admitted candidate.
4. Missing swings, FVG, or order blocks do not reject an otherwise admitted
   candidate.
5. Same-direction candidates select the highest score deterministically.
6. Opposite-direction candidates require the configured score margin.
7. Losing candidates and score breakdowns remain persisted.
8. Hard-gate failures remain advisory-only and are not sent to the executor.
9. All selected intents route to `bybit / hyro`.
10. Score and LLM annotations cannot modify entry, stop, target, direction, or
    hard-gate status.

## Related Documents

- `specs/adr-strategy-confluence-scoring.md`
- `specs/research-to-bot-execution-adapter.md`
- `specs/llm-position-sidecar.md`
- `specs/alpha-outcome-policy.md`
- `agent.md`
- `README.md`
