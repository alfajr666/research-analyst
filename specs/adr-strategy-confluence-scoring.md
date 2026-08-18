# ADR: Strategy Confluence Scoring and Confidence

## Status

Accepted (design). Implementation follows in strategy plugins and the existing
LLM research path. Does not authorize execution or calibrated trade sizing.

## Context

Strategies need a clear path from market context to a bot-facing trigger, plus a
way to rank setups when multiple factors (VP, FVG/OB, TA, multi-TF structure)
agree. Operators also want optional LLM judgment without confusing it with
statistical calibration.

## Decision

### 1. Strategy contract: setup then trigger

Each strategy plugin defines:

1. **Setup** — point-in-time context (structure, zones, VP/flow, TA family).
2. **Trigger** — concrete `entry_condition`, invalidation, targets, `valid_until`.

Only the trigger surface is the final plugin output consumed by the outbox,
publisher, Telegram/Discord delivery, and optional execution adapter. Setup
detail lives in the immutable `feature_snapshot`.

```text
finalized cutoff snapshot
        │
        v
  strategy plugin
   setup ──► trigger event ──► alpha outbox ──► bots / channels
```

### 2. Relative scoring over rigid thresholds

Prefer asset-relative and ATR-normalized distances and within-cutoff ranking over
fixed absolute thresholds (e.g. “within 0.3% of POC”).

- **Hard gates** (few): direction, HTF bias consistency, valid trigger geometry,
  liquidity / data freshness requirements.
- **Soft score** (many): VP proximity, FVG/OB stack tightness, TA confirm,
  LTF-inside-HTF alignment, contradiction penalties.
- Emit when gates pass. Score ranks priority and alert weight; it does not replace
  geometry. Avoid pure “best of a bad batch” with no absolute floor.

### 3. Confluence archetype (ideal, not mandatory)

Highest soft scores when **VP + FVG/OB + TA** agree in the **same or nearby
zone** (ATR geometry), and **LTF trigger aligns with HTF structure**.

Hierarchy (unchanged):

```text
4h FVG / OB  = primary structural context
1h FVG / OB  = secondary refinement (4h wins on conflict)
15m          = trigger only; no structural zone materialization
```

Zone proximity (illustrative defaults; tune in config, not ad hoc):

```text
same_zone  distance ≤ 0.25 · ATR
near_zone  0.25 · ATR < d ≤ 0.75 · ATR
far        d > 0.75 · ATR  →  score floor / optional non-emit
```

OpenMarket VP/flow remains optional and venue-scoped. Approximate CoinAnalyze
candle-distributed VP must keep its distinct label and never substitute as native
VP. Missing optional evidence lowers score; it does not invent levels.

### 4. Score shape (shared library, per-plugin weights)

```text
confluence_score =
    w_htf   * htf_structure_align
  + w_ltf   * ltf_inside_htf
  + w_zone  * zone_stack_tightness      # FVG ∩ OB ∩ VP distances
  + w_vp    * vp_level_proximity
  + w_ta    * ta_confirm                # one TA family per strategy
  - w_conflict * contradiction_penalty
```

Normalize / rank per asset and cutoff. Keep strategy families as separate
hypotheses (`accumulation`, `ignition`, `continuation`, …) with a **shared
scoring library**, not one blended mega-strategy.

Map `confluence_score` → event `confidence` in `[0, 1]` as today. Always set:

```text
confidence_status = "uncalibrated"
```

until an offline calibrator (outcomes ledger, walk-forward, no LLM) promotes a
versioned map (`calibrated_vN`). Confluence counts are never treated as win
probability.

### 5. LLM judgment is a booster, not calibration

| Owner | May set | Must not set |
|-------|---------|----------------|
| Strategy plugin | setup, trigger, `confluence_score`, `confidence`, `confidence_status=uncalibrated` | narrative stance |
| Offline calibrator | `confidence_status=calibrated_vN`, optional score→prob remap | live LLM calls |
| LLM research (post-emit) | advisory booster fields only | any deterministic event field |

**Allowed booster (additive, after emit):**

```text
llm_review.stance     = support | caution | oppose
llm_review.boost      = small non-negative priority weight for alert ranking only
llm_review.rationale  = cited, schema-validated prose
llm_review.counter_evidence / data_gaps
```

Rules:

- LLM runs **downstream** of deterministic event generation; never on the 15m
  critical path for emission identity.
- Booster may reorder or annotate delivery priority; it must **not** change
  direction, entry, invalidation, targets, snapshot, `confidence`, or
  `confidence_status`.
- Booster must **not** flip `confidence_status` to calibrated or claim P(win).
- If LLM is disabled, timed out, or over budget, the event stands unchanged
  (`boost = 0`, no stance).
- Telegram/Discord may show the booster as advisory copy; execution adapters
  ignore it unless a future human-reviewed policy explicitly allowlists a
  priority-only use.

**Rejected:** LLM-assigned calibration, LLM-written `confidence`, or LLM as a
hard emit/suppress gate.

## Consequences

- Plugins stay auditable and replayable from cutoff snapshots.
- Operators get richer ranking (geometry score + optional LLM stance) without
  fake probabilities.
- Calibration remains a batch research problem on `alpha_outcomes`.
- Phase-one advisory confluence labels remain valid; this ADR adds an explicit
  relative score path and a bounded LLM booster seam.

## Non-goals

- Sizing, leverage, venue selection, or order placement from score or LLM boost.
- Treating discovery rank, HMM regime, or LLM stance as trade instructions.
- Replacing existing strategy families with a single confluence detector.

## Tracer families

Implementations of this ADR (design-locked; implementation deferred until chosen):

| `strategy_id` | Spec |
|---------------|------|
| **`accumulation-base-v2`** | `specs/strategy-accumulation-base-v2.md` — limit at 1h EMA inside compression |
| **`impulse-ignition-v2`** | `specs/strategy-impulse-ignition-v2.md` — breakout of 1h base lid |

- Predecessors `*-v1` run in parallel briefly, then disable
- **`continuation-breakout`** still pending grill/clone
- Do not merge families into one strategy; share scoring/bias libraries only

## Related

- `specs/strategy-accumulation-base-v2.md` — tracer family rewrite
- `specs/data-platform-strategy-plugins.md` — cutoff, zones, plugin isolation
- `specs/llm-research-agent.md` — advisory LLM boundaries
- `specs/alpha-outcome-policy.md` — descriptive outcomes for future calibration
- `agent.md` — producer contract and research discipline
