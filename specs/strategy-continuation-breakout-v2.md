# Strategy: continuation-breakout-v2

## Status

Accepted design from grill (locked). Third tracer family under the confluence
scoring ADR. Does not authorize execution or calibrated sizing.

## Locked identity

| Field | Value |
|-------|--------|
| `strategy_id` | **`continuation-breakout-v2`** (immutable for this hypothesis) |
| `setup_class` | `continuation_breakout` |
| `plugin_version` | `v2` at first ship |
| `phase` | **`armed_flag_breakout`** |
| Predecessor | `continuation-breakout-balanced-v1` — parallel short-term, then disable |

Do not put preset names (`early` / `balanced` / `confirmed`) in `strategy_id`.
Optional weight profiles may appear in config and `feature_snapshot` only.

## Related

- `specs/adr-strategy-confluence-scoring.md`
- `specs/strategy-accumulation-base-v2.md`
- `specs/strategy-impulse-ignition-v2.md`
- `specs/data-platform-strategy-plugins.md`
- `specs/alpha-outcome-policy.md`
- `specs/llm-research-agent.md`

## Thesis

An **established 4h trend** pauses into a **1h flag** (compressed, shallow
retrace). While 15m price is still **inside** the flag and **near the
continuation edge**, arm a **breakout** through `flag_high` (long) or
`flag_low` (short).

Not ignition: ignition is a first coil without required prior trend.  
Not accumulation: accumulation limits at 1h EMA; this breaks the flag lid.  
Not chase: already-through lid fails; excessive extension fails.

```text
4h   established trend (EMA48 side + min signed return) + zone bias
1h   flag: compression + retrace cap + flag_high/low
15m  inside flag, near edge, not breached; observed_at / ref_close
```

## Pipeline

```text
finalized 15m cutoff
  → hard gates
  → confluence_score
  → S_min + top-N
  → re-arm (one active per asset+direction for this strategy_id)
  → breakout event → outbox → channels
  → optional LLM booster (delivery order only)
```

## Hard gates

### 1. 4h established trend

- `structure_bias` from last completed **4h close vs EMA48_4h** (same definition
  as sibling families).
- **Min trend print:** signed return over **P** completed 4h bars in bias
  direction ≥ `t_min` when ATR-normalized (exact norm in impl/config).
- `zone_bias` = nearest 4h FVG/OB in **`active` or `partial`** (midpoint to
  `ref_close`), same agree-or-abstain table as siblings:
  - conflict structure vs zone → **fail**
  - both missing → **fail**
  - else `direction` = resolved bias

### 2. 1h flag (pause, not reversal)

On completed 1h bars ≤ cutoff:

- Window **N** bars forms the flag:  
  `flag_high − flag_low ≤ k · ATR_1h`.
- `flag_high` / `flag_low` = window high/low.
- **Retrace cap:** pullback against the 4h trend (from prior impulse extreme into
  the flag) ≤ `retr_max` fraction of that impulse (config). Deeper → fail.
- **Prior-base expansion fail:** last 1h close must not break the prior (N−1)
  range by more than `g · ATR_1h` **against** the trade direction in a way that
  negates the flag (same spirit as siblings; document edge in impl).
- Optional: flag must lean with trend (e.g. long flag holds higher lows) as soft
  only unless impl finds a clean hard rule later.

### 3. 15m location: inside, near edge, not breached

- `ref_close` = last completed **15m** close.
- **Inside** `[flag_low, flag_high]`.
- **No breach:** long `ref_close ≤ flag_high`; short `ref_close ≥ flag_low`
  (float epsilon only). Through the lid → **fail** (not this arm; not ignition
  either if 4h trend gates differ — simply no continuation event).
- **Near edge:** long `flag_high − ref_close ≤ e · ATR_1h`; short mirrored.

### 4. Extension cap (anti-parabolic)

- If signed move over the extension window (config: e.g. 1d of 15m or last N 4h)
  already exceeds **`x_max · ATR`** in trade direction → **fail**.
- Mild extension below cap is soft-penalized only.

### 5. Trigger geometry + risk

- Entry = `flag_high` (long) / `flag_low` (short).
- Invalidation = opposite flag extreme.
- `risk = |entry − inv|`; if `risk > r_max · ATR_1h` → **fail** (no clamp).
- Single target **1.5R**; bars fresh per platform rules.
- OI/funding/VP absence does not fail gates.

### 6. Re-arm

No second emit while a non-terminal **`continuation-breakout-v2`** event exists
for the same asset+direction. **No mutex** with accumulation or ignition
(separate hypotheses).

## Soft score (`confluence_score`)

| Term | Notes |
|------|--------|
| `ltf_inside_htf` | zones vs ref/entry (0.25 / 0.75 ATR) |
| `zone_stack_tightness` | FVG∩OB with trend |
| `vp_proximity` | 0 if unavailable; never fake native VP |
| `flag_compression_quality` | tighter 1h flag |
| `edge_proximity` | closer to lid inside `e` |
| `trend_quality` | 4h return strength beyond `t_min` |
| `retrace_quality` | shallower pullback inside `retr_max` |
| `acceptance` | recent partial acceptance beyond flag (soft only) |
| `participation` | volume / OI; 0 if missing |
| `relative_strength` | vs BTC |
| `funding_neutral` | soft; 0 if missing |
| `− extension_penalty` | approaches `x_max` |
| `candle_quality` | arming 15m |
| `− contradiction_penalty` | residual TF tension |

```text
confidence        = clamp01(f(confluence_score))
confidence_status = "uncalibrated"
```

Optional `weight_profile` name in snapshot if multiple config profiles exist.

## Emit floor

1. `confluence_score ≥ S_min`
2. **Top N absolute** among ≥ `S_min` this cutoff  
Then re-arm filter.

## Trigger (bot-facing)

| Field | Rule |
|-------|------|
| `entry_condition` | long `{ "type": "breakout_above", "price": flag_high }` · short `{ "type": "breakout_below", "price": flag_low }` |
| `invalidation_price` | opposite 1h flag extreme |
| `targets` | single 1.5R |
| `observed_at` | last completed **15m** bar end |
| `valid_until` | `observed_at + 4h` |
| `horizon_minutes` | `240` |
| `phase` | `armed_flag_breakout` |
| `direction` | from bias + trend gates |

## LLM booster

Post-emit only. `stance` + `boost` for **delivery order**. Never mutates
direction, entry, inv, targets, snapshot, `confidence`, or `confidence_status`.
Not an emit gate.

## Lifecycle vs v1

1. Register **`continuation-breakout-v2`**.
2. Parallel with `continuation-breakout-balanced-v1` briefly.
3. Disable v1 after comparison; keep code until then.
4. No shared dedupe keys; no row rewrites.

## Config knobs (defaults at ship)

Locked: id/phase, dual direction, 4h trend+EMA48, 1h flag+retrace cap, pre-break
edge, no breach, x_max hard, breakout at flag extreme, opposite inv, r_max fail,
1.5R, 4h horizon, S_min+top-N, one-active re-arm, sibling bias/zone rules,
presets not in strategy_id.

| Knob | Role |
|------|------|
| `P`, `t_min` | 4h trend lookback and min ATR-norm return |
| `N`, `k` | 1h flag window and ATR compression |
| `retr_max` | max countertrend retrace into flag |
| `g` | expansion grace (ATR) |
| `e` | max distance to flag edge (ATR) |
| `x_max` | max extension (ATR) before hard fail |
| `r_max` | max flag risk / ATR |
| `S_min`, `N_top` | emit floor |
| weight profiles | optional soft weights in config/snapshot |
| `llm_boost` cap | delivery priority |

## Family separation

| | accumulation-base-v2 | impulse-ignition-v2 | continuation-breakout-v2 |
|--|----------------------|---------------------|----------------------------|
| Prior trend | not required | not required | **required (4h)** |
| Mid structure | 1h compression near EMA | 1h coil near lid | **1h flag after trend** |
| Entry | limit @ EMA99_1h | break base lid | **break flag lid** |
| Chase control | `d_max` to EMA | no breach + edge | **no breach + edge + x_max** |
| Phase | `armed_compression_pullback` | `armed_base_breakout` | **`armed_flag_breakout`** |

## Non-goals

- Embedding early/balanced/confirmed in `strategy_id`
- Post-break chase entries as the primary arm
- Family mutex across strategy ids
- Calibrated or LLM-written confidence
- Clamped stops; dual %-targets from v1
- Sizing / venue / orders

## Acceptance (implementation)

- [ ] Emits only as `strategy_id = continuation-breakout-v2`
- [ ] Dual direction; 4h EMA48 + min return; zone agree-or-abstain
- [ ] 1h flag: ATR compression + retrace cap
- [ ] 15m inside, near edge, not through lid
- [ ] Hard fail on extension > x_max·ATR
- [ ] Entry at flag extreme; inv opposite; r_max; 1.5R; 4h
- [ ] `phase = armed_flag_breakout`
- [ ] Soft basket includes acceptance/participation/RS/extension penalty
- [ ] S_min + top-N; one active per asset+direction for this id
- [ ] No mutex with other v2 families
- [ ] Preset names only in snapshot/config if used
- [ ] LLM booster order-only
- [ ] v1 parallel then disable; PIT replay only
