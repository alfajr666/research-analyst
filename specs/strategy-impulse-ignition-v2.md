# Strategy: impulse-ignition-v2

## Status

Accepted design from grill (locked). Second tracer family under the confluence
scoring ADR. Does not authorize execution or calibrated sizing.

## Locked identity

| Field | Value |
|-------|--------|
| `strategy_id` | **`impulse-ignition-v2`** (immutable for this hypothesis) |
| `setup_class` | `impulse_ignition` |
| `plugin_version` | `v2` at first ship |
| `phase` | **`armed_base_breakout`** |
| Predecessor | `impulse-ignition-v1` — parallel short-term, then disable |

Do not reuse `impulse-ignition-v1` for this logic. Outcomes and delivery history
must not mix eras under one `strategy_id`.

## Related

- `specs/adr-strategy-confluence-scoring.md` — score, gates, LLM booster
- `specs/strategy-accumulation-base-v2.md` — sibling tracer (shared ops patterns)
- `specs/data-platform-strategy-plugins.md` — cutoff, zones, plugins
- `specs/alpha-outcome-policy.md` — outcomes
- `specs/llm-research-agent.md` — advisory LLM only

## Thesis

After a **1h coil** (compression vs ATR and vs its own prior range), with **4h
bias**, price still **inside** the 1h base and **near the breakout edge** on 15m
→ arm a **stop/breakout** through `base_high` (long) or `base_low` (short).

Distinct from `accumulation-base-v2`: that family limits into EMA inside a base;
this family waits for **break of the base lid** before entry. Already-broken
price is **not** ignition (belongs to continuation).

```text
4h   bias (EMA48) + nearest FVG/OB context
1h   HARD base: compression + base_high/low
15m  inside base, near edge, observed_at / ref_close; trigger arm only
```

## Pipeline

```text
finalized 15m cutoff
    → hard gates
    → confluence_score
    → S_min + top-N
    → re-arm (one active per asset+direction for this strategy_id)
    → breakout entry event → outbox → channels
    → optional LLM booster (order only)
```

## Hard gates

### 1. 1h compression (both)

On completed 1h bars ≤ cutoff:

- Window **N** bars: `range_N = high(N)−low(N) ≤ k · ATR(1h, 14)`.
- **Prior ratio:** prior window of **P** 1h bars immediately before the base:  
  `range_N / range_prior ≤ c_ratio` (config; v1 spirit ≈ 0.90).
- `base_high`, `base_low` = high/low of the N-bar base window.
- **Prior-base expansion fail:** last 1h close must not break the **prior**
  (N−1) range by more than `g · ATR_1h` in the trade direction.

### 2. 15m still inside base, near edge, not breached

- `ref_close` = last completed **15m** close.
- **Inside:** long `base_low ≤ ref_close ≤ base_high`; short mirrored (same band).
- **No breach (hard):** long requires `ref_close ≤ base_high`; short
  `ref_close ≥ base_low` (epsilon for float only). Through the lid → fail
  (not this family).
- **Near edge:** long `base_high − ref_close ≤ e · ATR_1h`; short
  `ref_close − base_low ≤ e · ATR_1h`.

### 3. 4h bias (agree-or-abstain) — same as accumulation-base-v2

| Signal | Definition |
|--------|------------|
| `structure_bias` | 4h close vs **EMA48_4h** |
| `zone_bias` | Nearest 4h FVG/OB in **`active` or `partial`**, midpoint distance to `ref_close` |

Conflict → fail; both missing → fail; else direction = resolved bias.
Long arms breakout above; short arms breakout below — must match bias.

### 4. Trigger geometry + risk

- Entry price = `base_high` (long) or `base_low` (short).
- Invalidation = opposite base extreme (`base_low` / `base_high`).
- `risk = |entry − invalidation|`; if `risk > r_max · ATR_1h` → **fail** (no clamp).
- Single target at **1.5R**; finite prices; bars fresh per platform rules.
- OI/funding/VP absence does not fail gates.

### 5. Re-arm

No second emit while a non-terminal **`impulse-ignition-v2`** event exists for
the same asset+direction. Does **not** mutex with accumulation-base-v2 (separate
hypotheses).

## Soft score (`confluence_score`)

| Term | Notes |
|------|--------|
| `ltf_inside_htf` | ref/entry vs 4h/1h zones (0.25 / 0.75 ATR bins) |
| `zone_stack_tightness` | FVG∩OB in trade direction |
| `vp_proximity` | 0 if unavailable; never fake native VP |
| `compression_quality` | tighter ATR range + better vs-prior ratio |
| `edge_proximity` | closer to lid inside `e` |
| `volume_dryup` | base vs prior volume (v1 spirit) |
| `oi_pressure` | OI vs price; **0 if missing** |
| `funding_neutral` | not extreme vs recent; **0 if missing** |
| `relative_strength` | vs BTC over base window |
| `prior_impulse_quality` | modest prior impulse sweet spot; parabolic → low/0 |
| `candle_quality` | arming 15m bar |
| `− contradiction_penalty` | residual TF tension |

```text
confidence        = clamp01(f(confluence_score))
confidence_status = "uncalibrated"
```

Snapshot stores base bounds, ATR, ratio, bias sources, component weights, closes.

## Emit floor

1. `confluence_score ≥ S_min`
2. **Top N absolute** among those ≥ `S_min` this cutoff  
Then apply re-arm drops.

## Trigger (bot-facing)

| Field | Rule |
|-------|------|
| `entry_condition` | long: `{ "type": "breakout_above", "price": base_high }` · short: `{ "type": "breakout_below", "price": base_low }` |
| `invalidation_price` | opposite 1h base extreme |
| `targets` | `[ entry ± 1.5 * risk ]` single |
| `observed_at` | last completed **15m** bar end |
| `valid_until` | `observed_at + 4h` |
| `horizon_minutes` | `240` |
| `phase` | `armed_base_breakout` |
| `direction` | from bias gate |

No synthetic `close * 1.005` entry. No dual %-targets from v1.

## LLM booster

Same ADR rules as accumulation-base-v2: post-emit `stance` + `boost` for
**delivery order only**; never mutates deterministic fields; not an emit gate.

## Lifecycle vs v1

1. Register **`impulse-ignition-v2`** in known/enabled strategy ids.
2. Parallel with v1 briefly; messages show full `strategy_id`.
3. Disable v1 in allowlist after comparison; keep code until then.
4. No row rewrites; no shared dedupe keys with v1.

## Config knobs (defaults at ship)

Locked: EMA48_4h bias, nearest active+partial zone, inside+edge+no breach,
breakout at base extreme, opposite-base inv, r_max fail, 1.5R, 4h horizon,
S_min+top-N, one active re-arm, phase name, dual direction.

| Knob | Role |
|------|------|
| `N`, `k` | 1h base window, ATR compression |
| `P`, `c_ratio` | prior window length, max base/prior range ratio |
| `g` | prior-base expansion grace (ATR) |
| `e` | max distance to breakout edge (ATR) |
| `r_max` | max base risk / ATR |
| `S_min`, `N_top` | emit floor |
| score weights | soft basket |
| `llm_boost` cap | delivery priority |

## Non-goals

- Chasing after lid breach (continuation family)
- Limit-at-EMA entries (accumulation family)
- Family mutex with accumulation
- Calibrated confidence or LLM-set confidence
- Clamped stops inside a wide base
- Long-only restriction
- Sizing / venue / orders

## Separation from accumulation-base-v2

| | accumulation-base-v2 | impulse-ignition-v2 |
|--|----------------------|---------------------|
| Setup | 1h compression | 1h compression + vs prior ratio |
| Location in base | near 1h EMA99 (`d_max`) | near breakout **edge** (`e`), not through lid |
| Entry | `limit_at_ema_context` | `breakout_above` / `breakout_below` |
| Invalidation | worse-of EMA band vs base | opposite base extreme |
| Signature soft | ema_proximity, volume_character | edge, OI, funding, RS, prior_impulse |

Both may emit the same asset under different `strategy_id`s for research.

## Acceptance (implementation)

- [ ] Emits only as `strategy_id = impulse-ignition-v2`
- [ ] Dual direction from agree-or-abstain 4h bias
- [ ] Hard: ATR compression **and** prior ratio; prior-base expansion fail
- [ ] Hard: 15m inside base, near edge, **not** through lid
- [ ] Entry at base_high/low; inv opposite base; r_max fail; 1.5R; 4h
- [ ] `phase = armed_base_breakout`
- [ ] Soft basket includes OI/funding/RS/prior impulse with missing→0
- [ ] S_min + top-N; one active per asset+direction for this id
- [ ] No mutex with accumulation-base-v2
- [ ] LLM booster order-only
- [ ] v1 parallel then disable; PIT replay only
