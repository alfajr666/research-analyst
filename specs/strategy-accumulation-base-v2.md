# Strategy: accumulation-base-v2

## Status

Accepted design (grill locked, including spec stress-pass). Implements the
confluence scoring ADR for a single tracer family. Does not authorize execution
or calibrated sizing.

## Locked identity

| Field | Value |
|-------|--------|
| `strategy_id` | **`accumulation-base-v2`** (immutable for this hypothesis) |
| `setup_class` | `accumulation_base` |
| `plugin_version` | `v2` at first ship; bump only for non-breaking plugin packaging |
| `phase` | `armed_compression_pullback` (stable once shipped) |
| Predecessor | `accumulation-base-v1` — parallel short-term, then disable |

Do not reuse `accumulation-base-v1` for this logic. Outcomes, confidence
observations, and Telegram history must not mix eras under one `strategy_id`.

## Related

- `specs/adr-strategy-confluence-scoring.md` — score, gates, LLM booster
- `specs/data-platform-strategy-plugins.md` — cutoff, zones, plugins
- `specs/alpha-outcome-policy.md` — trigger/target/invalidation outcomes
- `specs/llm-research-agent.md` — advisory LLM only

## Thesis

After **1h compression** (accumulation visible on mid TF), with **4h directional
bias**, arm a **limit at 1h EMA99 context** only when price is still near that
magnet. Rank quality with relative confluence (zones, VP, TA). 15m is trigger
timing only — never the home of the base. 4h-only “accumulation” is rejected as
late; 15m-only base is rejected as noise.

## Timeframe roles

```text
4h   hard bias (EMA48 position) + primary FVG/OB context (nearest zone)
1h   HARD home of accumulation (ATR compression) + EMA99 entry magnet
15m  trigger arming / ref_close / observed_at; no structural zone materialization
```

On zone hierarchy for evidence attachment, existing platform rules apply (4h
primary, 1h refine). Bias structure signal is **not** swing-based.

## Pipeline

```text
finalized 15m cutoff snapshot
        │
        v
  hard gates ──fail──► no event
        │ pass
        v
  confluence_score (soft basket, ATR-relative)
        │
        v
  emit floor: score ≥ S_min AND top-N among those ≥ S_min this cutoff
        │ pass
        v
  re-arm gate: no other active v2 event for asset+direction
        │ pass
        v
  build trigger event (limit_at_ema_context)
        │
        v
  alpha outbox ──► publisher ──► Telegram/Discord
        │
        v  (optional, after emit)
  LLM review booster (stance + delivery priority only)
```

## Hard gates

All must pass. Few by design.

### 1. 1h accumulation (compression + not expanded)

On completed 1h bars ending at or before the cutoff:

- Lookback of **N** 1h bars (config).
- **Range compression (full window):**  
  `high(N) − low(N) ≤ k · ATR(1h, 14)`.
- Record `base_high`, `base_low` = high/low of that N-bar window.
- **Prior-base expansion fail (A′):**  
  Let prior = bars `−N … −2` (exclude last 1h bar).  
  If last 1h **close** breaks outside prior high/low by more than **`g · ATR_1h`**
  **in the trade direction**, fail.  
  (Opposite-direction break is not this fail; contradiction soft-term may apply.)
- Volume dry-up/spike is **not** a hard gate (soft only).

### 2. 4h bias (agree-or-abstain)

Two optional signals, each `long` | `short` | `missing`:

| Signal | Locked definition |
|--------|-------------------|
| `structure_bias` | Last completed **4h close** vs **EMA48 on 4h**. `close > EMA48` → long, `close < EMA48` → short, equal/unavailable → missing. **Not** EMA99_4h (warmup vs 14d backfill). **Not** swing structure. |
| `zone_bias` | Among 4h FVG/OB in states **`active` or `partial`** only: pick **nearest** by midpoint distance to `ref_close` (ATR-normalized). That zone’s direction → bias. None → missing. `filled` / `invalidated` excluded. |

Resolution:

| structure | zone | Result |
|-----------|------|--------|
| D | missing | bias = D |
| missing | D | bias = D |
| D | D | bias = D |
| D | opposite | **fail gate** |
| missing | missing | **fail gate** |

Trade `direction` must equal resolved bias.

### 3. Valid trigger geometry

- **1h EMA99** available and finite (1h series warm enough).
- **`ref_close`** = last completed **15m** close; finite and fresh.
- **Proximity hard gate:**  
  `|ref_close − EMA99_1h| / ATR_1h ≤ d_max` (config). Stretched = no arm.
- Entry, invalidation, target computable per Trigger section.
- **Risk cap:** let `risk = |entry − invalidation|`.  
  If `risk > r_max · ATR_1h`, **fail** (do not clamp the stop). Wide base → no emit.
- Required bars fresh under existing evaluator freshness rules.
- Optional VP/zones may be `unavailable`; they do not fail this gate by absence.

### 4. Confirm bar policy

- Arm only on a **completed** 15m bar at the cutoff.
- No extra hard impulse green/red requirement.
- Candle body/wick quality is soft only.
- Direction consistency with bias remains hard via gate 2.

### 5. Re-arm (anti-spam)

While a non-terminal `accumulation-base-v2` event exists for the same
**asset + direction** with status still live for delivery/outcome
(`active` / not yet terminal outcome), **do not emit** another.

Slot frees when the event is expired, invalidated, target-hit, not_triggered, or
otherwise terminal per outcome policy. No “material change” re-emit in v2
tracer.

## Soft score (`confluence_score`)

Shared ADR basket; weights in config; distances ATR-normalized where applicable.

| Term | Notes |
|------|--------|
| `ltf_inside_htf` | Entry/ref vs 4h/1h zones: same ≤0.25·ATR, near ≤0.75·ATR, else low |
| `zone_stack_tightness` | FVG∩OB cluster tightness in trade direction |
| `vp_proximity` | POC/VA proximity; **0 if unavailable** (never invent native VP; never label approx candle VP as native) |
| `compression_quality` | Tighter 1h range vs ATR → higher |
| `volume_character` | Legacy-style dry-up / spike on 1h (soft) |
| `ema_proximity` | Distance of ref_close to 1h EMA99 (inside `d_max`) |
| `candle_quality` | Body/wick of arming 15m bar |
| `− contradiction_penalty` | Residual 1h vs 4h tension short of hard bias fail |

Optional soft boost: 4h also compressed (not required). Nearest-zone logic is for
hard `zone_bias` only; soft stack may still use multiple zones.

Map to event fields:

```text
confidence          = clamp01(f(confluence_score))   # deterministic heuristic
confidence_status   = "uncalibrated"
feature_snapshot.confidence_components = { term: weighted_value, ... }
feature_snapshot.confluence_score = <raw or normalized>
feature_snapshot.close_15m / close_1h / ema99_1h / ema48_4h / base_* / atr_1h
```

Never treat component count as P(win).

## Emit floor

Among symbols that pass hard gates **1–4** in this cutoff (re-arm applied before
or after rank consistently; recommended: score all gated, apply `S_min`, take
top-N, then drop those blocked by re-arm):

1. `confluence_score ≥ S_min`
2. Rank in **top N absolute** among those ≥ `S_min` (not percentile)

Both required. N is config (traffic control). Small gated sets: if only one name
clears `S_min` and top-N is 3, that one may emit (subject to re-arm).

## Trigger (bot-facing output)

| Field | Rule |
|-------|------|
| `entry_condition` | `{ "type": "limit_at_ema_context", "price": entry }` |
| EMA source | **EMA99 on 1h** completed bars |
| `ref_close` | Last completed **15m** close |
| `entry` | long: `max(ema99_1h, ref_close)`; short: `min(ema99_1h, ref_close)` |
| `invalidation_price` | **Worse-of** (further from entry in risk direction): (a) EMA band ±1.5% of EMA99_1h, (b) beyond 1h `base_low` (long) / `base_high` (short). Then enforce **risk ≤ r_max·ATR_1h** or fail gate. |
| `targets` | Single target at **1.5R** from `|entry − invalidation|` |
| `observed_at` | End timestamp of last completed **15m** bar |
| `valid_until` | `observed_at + 4h` |
| `horizon_minutes` | `240` |
| `phase` | `armed_compression_pullback` |
| `direction` | `long` \| `short` from bias gate |

Setup internals stay in `feature_snapshot` only.

## LLM booster

After deterministic emit, existing research path may attach:

```text
llm_review.stance    = support | caution | oppose
llm_review.boost     = non-negative priority delta for delivery order only
llm_review.rationale = schema-validated, cited
```

Rules:

- No change to direction, entry, invalidation, targets, snapshot, `confidence`,
  or `confidence_status`.
- Delivery may sort by `confluence_score + llm_boost` when review present.
- LLM off / timeout / budget → event unchanged, boost 0.
- Not a hard emit/suppress gate.

## Lifecycle vs v1

1. Register plugin id **`accumulation-base-v2`** in `KNOWN_STRATEGIES` /
   `STRATEGY_ENABLED_IDS`.
2. **Parallel:** enable v1 and v2 briefly; messages must show full `strategy_id`.
3. **Then:** disable v1 in allowlist; keep code until outcome comparison is done.
4. Do not rewrite v1 rows or reuse v1 dedupe keys.

Ignition and continuation stay on legacy scoring until a later clone of this
pattern.

## Config knobs (defaults at implement/ship)

Locked multiples: EMA invalidation band **1.5%**, target **1.5R**, horizon **4h**,
zone proximity bins **0.25 / 0.75 ATR**, structure EMA **48 on 4h**, entry EMA
**99 on 1h**, zone states **active+partial**, nearest-zone bias, top-**N** not %.

| Knob | Role |
|------|------|
| `N`, `k` | 1h compression window and ATR multiple |
| `g` | prior-base expansion grace (ATR) |
| `d_max` | max \|ref_close − EMA99_1h\| / ATR_1h |
| `r_max` | max risk / ATR_1h before geometry fail |
| score weights | soft basket |
| `S_min`, `N_top` | emit floor |
| `llm_boost` cap | max delivery priority delta |

## Non-goals

- Calibrated probability or LLM-set confidence
- Zone-aware entry type (deferred v2.1)
- Swing-based 4h structure bias
- Clamped/fake stops inside a wide base
- Percentile-only emit ranking
- Merging accumulation with ignition/continuation
- Changing global outcome policy or exec adapter beyond `limit_at_ema_context`
- Sizing, venue, or order placement

## Acceptance (implementation)

- [ ] Emits only as `strategy_id = accumulation-base-v2`
- [ ] `structure_bias` from EMA48_4h only; warmup-safe vs 14d backfill
- [ ] Hard fail without 1h compression, on prior-base expansion, or without resolved 4h bias
- [ ] Bias conflict (structure vs nearest zone) fails closed
- [ ] `zone_bias` nearest among active+partial 4h FVG/OB
- [ ] Hard `d_max` proximity; hard `r_max` risk; no stop clamp
- [ ] Entry uses 1h EMA99 + 15m `ref_close`; `observed_at` = 15m bar end
- [ ] Invalidation worse-of EMA band vs 1h base; single 1.5R target; 4h validity
- [ ] `phase = armed_compression_pullback`
- [ ] `confluence_score` components in snapshot; `confidence_status=uncalibrated`
- [ ] Emit requires `S_min` and top-N absolute among scorers
- [ ] One active armed event per asset+direction
- [ ] VP missing → term 0, not fabricated native VP
- [ ] LLM path cannot mutate deterministic fields; boost order-only
- [ ] v1 independently enableable until intentionally disabled
- [ ] Point-in-time replay from finalized cutoff snapshots only
