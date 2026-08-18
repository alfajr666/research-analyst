# Strategy: rsi-reclaim-v1

## Status

Accepted design (operator lock 2026-08-18). Fourth strategy family under the
confluence scoring ADR. Thesis stolen from a Freqtrade equity RSI-reclaim engine
and rewritten as a research-analyst alpha plugin. Does not authorize execution
or calibrated sizing.

## Locked identity

| Field | Value |
|-------|--------|
| `strategy_id` | **`rsi-reclaim-v1`** (immutable for this hypothesis) |
| `setup_class` | `continuation_pullback` |
| `plugin_version` | `v1` at first ship |
| `phase` | **`confirmed_rsi_reclaim`** |
| Predecessor | none (new family; not a rewrite of accumulation-base) |

Do not fold this into `accumulation-base-v2`. Outcomes and delivery history must
not mix under another `strategy_id`.

## Related

- `specs/adr-strategy-confluence-scoring.md` — score, gates, LLM booster
- `specs/strategy-v2-shared-library.md` — shared context + scoring
- `specs/data-platform-strategy-plugins.md` — cutoff, zones, plugins
- `specs/alpha-outcome-policy.md` — outcomes
- Source idea: Equity_CryptoBeta_RSIReclaim (Freqtrade) — 5m reclaim, 15m RSI,
  1h EMA200 sep band; engine ladder/bias-exit **not** ported

## Thesis

In a **4h directional bias**, with **1h price mildly extended** off EMA200 (not
glued, not parabolic), wait for a **15m pullback** (EMA stack still aligned, RSI
still pullback-ish and turning) that **tags the 15m fast EMA and reclaims** with
a quality body. Emit a **confirmed reclaim** trigger (not a limit arm).

Distinct from:

| Family | Difference |
|--------|------------|
| `accumulation-base-v2` | Arms **limit at 1h EMA99** inside 1h compression; no RSI reclaim |
| `impulse-ignition-v2` | Arms **break of 1h base lid** before expansion |
| `continuation-breakout-v2` | **4h trend + 1h flag breakout**, not dip reclaim |

```text
4h   hard bias (EMA48 + zone agree-or-abstain)
1h   mild extension band vs EMA200 (sep_min..sep_max in trade direction)
15m  stack + RSI pullback/turn + touch fast EMA + directional reclaim body
```

## Timeframe roles

```text
4h   hard bias + primary FVG/OB context
1h   extension / slow magnet (EMA200); not the entry home
15m  trigger home: stack, RSI, touch, reclaim, observed_at
```

No 5m bars. The original 5m touch/reclaim is expressed on **completed 15m** only.

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
  re-arm gate: no other active rsi-reclaim-v1 event for asset+direction
        │ pass
        v
  build confirmed-reclaim trigger event
        │
        v
  alpha outbox ──► publisher ──► Telegram/Discord
        │
        v  (optional, after emit)
  LLM review booster (stance + delivery priority only)
```

## Hard gates

All must pass. Few by design.

### 1. Fresh completed 15m

- `ref_*` from last completed 15m bar strictly before cutoff.
- Freshness: existing `last_completed_bar_fresh` rule.
- Series warm enough for 1h EMA200 (≈200 completed 1h bars) and 4h EMA48.

### 2. 4h bias (agree-or-abstain)

Same as accumulation-base-v2 / impulse-ignition-v2:

| Signal | Definition |
|--------|------------|
| `structure_bias` | Last completed 4h close vs EMA48_4h |
| `zone_bias` | Nearest active\|partial 4h FVG/OB by midpoint distance |

Agree-or-abstain via `resolve_bias`. Conflict or both missing → fail.
Both **long and short** are allowed.

### 3. 1h mild extension band (stolen sep)

On last completed 1h bar:

- `ema200_1h` = EMA(200) of 1h closes.
- Directed separation:
  - long: `sep = (close_1h − ema200_1h) / ema200_1h`
  - short: `sep = (ema200_1h − close_1h) / ema200_1h`
- Require `sep_min ≤ sep ≤ sep_max` (config).
- Negative or below `sep_min` → not extended enough / wrong side.
- Above `sep_max` → chase / parabolic; fail.

### 4. 15m stack aligned with bias

- `ema_fast_15m` = EMA(ema_fast) on 15m (default 20).
- `ema_mid_15m` = EMA(ema_mid) on 15m (default 50).
- long: `ema_fast > ema_mid`; short: `ema_fast < ema_mid`.

### 5. 15m RSI pullback + turn

- RSI length default 14 (Wilder).
- long: `rsi ≤ rsi_max` **and** `rsi ≥ rsi_prev` (still pullback-ish, turning up).
- short: `rsi ≥ rsi_min` **and** `rsi ≤ rsi_prev` (still pullback-ish, turning down).
- Missing RSI → fail.

### 6. Touch + confirmed reclaim on last 15m

With pullback tolerance `pullback_tol` (fraction of price, default 0.0008):

- **Touch (long):** `low ≤ ema_fast * (1 + pullback_tol)` **or** `close ≤ ema_fast`.
- **Touch (short):** `high ≥ ema_fast * (1 − pullback_tol)` **or** `close ≥ ema_fast`.
- **Reclaim (long):** `close > ema_fast` **and** `close > open`.
- **Reclaim (short):** `close < ema_fast` **and** `close < open`.

### 7. Body quality floor

- `body_atr = |close − open| / ATR_15m(14)`.
- Require `body_atr ≥ body_atr_min` (default 0.20). Weak doji reclaim → fail.

### 8. Valid trigger geometry

- Entry, invalidation, single target computable (Trigger section).
- `risk = |entry − invalidation| > 0`.
- If `risk > r_max · ATR_15m`, **fail** (do not clamp the stop).
- Optional VP/zones may be unavailable; absence does not fail gates 1–7.

### 9. Re-arm (anti-spam)

While a non-terminal `rsi-reclaim-v1` event exists for the same **asset +
direction**, do not emit another. Slot frees on terminal outcome / expiry.

## Soft score (`confluence_score`)

| Term | Notes |
|------|--------|
| `ltf_inside_htf` | Entry/ref vs 4h/1h zones (shared helper) |
| `zone_stack_tightness` | FVG∩OB cluster in trade direction |
| `vp_proximity` | 0 if unavailable; never invent native VP |
| `rsi_quality` | Depth inside pullback band + turn magnitude |
| `reclaim_body` | body/ATR above floor (stronger body → higher) |
| `ema_touch_quality` | How cleanly price tagged fast EMA (ATR distance) |
| `extension_quality` | Sweet spot inside sep band (mid-band preferred) |
| `stack_spread` | \|ema_fast − ema_mid\| / ATR_15m (healthy stack, not flat) |
| `candle_quality` | Body/wick of reclaim 15m bar |
| `− contradiction_penalty` | Residual structure/zone tension short of hard fail |

Map:

```text
confidence        = clamp01(confluence_score)
confidence_status = "uncalibrated"
feature_snapshot.confidence_components = { ... }
feature_snapshot.confluence_score = <score>
```

Never treat component count as P(win).

## Emit floor

1. `confluence_score ≥ S_min`
2. Rank in **top N absolute** among those ≥ `S_min` this cutoff
3. Then drop re-arm blocked names

## Trigger (bot-facing output)

| Field | Rule |
|-------|------|
| `entry_condition` | long: `{ "type": "breakout_above", "price": entry }`; short: `breakout_below` |
| `entry` | Reclaim bar **close** (confirmed reclaim already printed) |
| `invalidation_price` | **Worse-of** (further from entry in risk direction): (a) reclaim bar extreme (`low` long / `high` short), (b) mid EMA band: long `ema_mid * (1 − inv_band)`, short `ema_mid * (1 + inv_band)` with `inv_band = 0.015` |
| `targets` | Single target at **1.5R** from `\|entry − invalidation\|` |
| `observed_at` | End timestamp of last completed **15m** bar |
| `valid_until` | `observed_at + 4h` |
| `horizon_minutes` | `240` |
| `phase` | `confirmed_rsi_reclaim` |
| `direction` | `long` \| `short` from bias gate |

Engine-owned (not emitted as lifecycle): ATR ladder partials, 1h bias-flip exit,
fixed percent stop. Stretch levels may appear in `feature_snapshot` only for
research (`stretch_atr`, `core_atr` distances) without multi-target outcomes.

### Execution adapter

Current adapter only forwards `limit_at_ema_context`. This family emits
**breakout_*** confirmed reclaim → **Telegram/Discord research path** first.
Do not force-fit exec adapter without a separate adapter change.

## Feature snapshot (minimum)

```text
source_symbol, confluence_score, confidence_components, component_raw,
close_15m, close_1h, ema_fast_15m, ema_mid_15m, ema200_1h, ema48_4h,
atr_15m, atr_1h, rsi_15m, rsi_prev_15m, sep_1h, body_atr,
structure_bias, zone_bias, nearest_zone, risk, completed_bar_at
```

## LLM booster

Same rules as other v2 families: post-emit stance/boost only; no mutation of
deterministic fields.

## Config knobs

Locked multiples: invalidation mid-EMA band **1.5%**, target **1.5R**, horizon
**4h**, zone bins **0.25 / 0.75 ATR**, structure EMA **48 on 4h**, slow EMA
**200 on 1h**, RSI Wilder **14**.

| Knob | Default | Role |
|------|---------|------|
| `EMA_FAST` | 20 | 15m fast EMA |
| `EMA_MID` | 50 | 15m mid EMA / stack |
| `RSI_LEN` | 14 | RSI length |
| `RSI_MAX` | 45 | long RSI ceiling |
| `RSI_MIN` | 55 | short RSI floor |
| `PULLBACK_TOL` | 0.0008 | touch band vs fast EMA |
| `BODY_ATR_MIN` | 0.20 | hard reclaim body floor |
| `SEP_MIN` | 0.003 | min directed 1h sep vs EMA200 |
| `SEP_MAX` | 0.04 | max directed 1h sep |
| `R_MAX` | 2.5 | max risk / ATR_15m |
| `S_MIN` | 0.55 | confluence floor |
| `N_TOP` | 3 | top-N absolute emit cap |

Env prefix: `RSI_RECLAIM_*`.

## Non-goals

- 5m bar ingestion or 5m cutoff
- Position ladder, trailing, or live 1h bias-exit in the producer
- Calibrated probability or LLM-set confidence
- Merging with accumulation / ignition / continuation IDs
- Exec-adapter support in v1 (breakout confirmed reclaim)
- Sizing, venue, or order placement
- Equity-TradFi parameter transfer without crypto walk-forward

## Acceptance (implementation)

- [x] Emits only as `strategy_id = rsi-reclaim-v1`
- [x] `setup_class = continuation_pullback`, `phase = confirmed_rsi_reclaim`
- [x] 4h bias agree-or-abstain; both directions
- [x] Hard 1h sep band vs EMA200
- [x] Hard 15m stack + RSI pullback/turn + touch/reclaim + body floor
- [x] Entry = reclaim close via `breakout_above` / `breakout_below`
- [x] Invalidation worse-of bar extreme vs mid-EMA band; single 1.5R; 4h validity
- [x] `confluence_score` in snapshot; `confidence_status=uncalibrated`
- [x] `S_min` + top-N + re-arm
- [x] Registered in `KNOWN_STRATEGIES`; enable via `STRATEGY_ENABLED_IDS`
- [x] Point-in-time replay from finalized cutoff snapshots only
- [x] Unit tests for emit identity, hard fails (sep, no reclaim, short history), re-arm
