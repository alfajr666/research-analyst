# Strategy: liquidity-sweep-reversal-v1

## Status

Accepted design (operator-facing research lock). **Pre-implementation audit
2026-08-18** against shipped plugin platform (`rsi_reclaim_v1` pattern,
`strategy_plugins.invoke_plugins_for_cutoff`, `alpha_outbox`, purity sets).
Fifth strategy family under the confluence scoring ADR.

Thesis originates from a mechanical crypto liquidity-sweep reversal model
(PDH/PDL sweep → reclaim → 15m BOS → 50% impulse limit). Rewritten as a
**research-analyst alpha plugin**. Does not authorize execution, calibrated
sizing, or Freqtrade live trading.

**Code status at lock:** M1–M3 helpers and plugin **not yet in tree**. Spec is
the build contract.

## Locked identity

| Field | Value |
|-------|--------|
| `strategy_id` | **`liquidity-sweep-reversal-v1`** (immutable for this hypothesis) |
| `setup_class` | `liquidity_reversal` |
| `plugin_version` | `v1` at first ship |
| `phase` | **`armed_impulse_retracement`** (limit after confirmed BOS) |
| Predecessor | none (new family; not a rewrite of ignition / rsi-reclaim) |
| Default enable | **opt-in** (not in default `STRATEGY_ENABLED_IDS`) |
| Data purity | **PRICE_STRUCTURE** (OHLCV-only hard path; OI/funding/VP soft only) |

Do not fold this into `impulse-ignition-v2`, `rsi-reclaim-v1`, or accumulation.
Outcomes and delivery history must not mix under another `strategy_id`.

## Related

- `specs/adr-strategy-confluence-scoring.md` — score, gates, LLM booster
- `specs/strategy-v2-shared-library.md` — shared context + scoring
- `specs/data-platform-strategy-plugins.md` — cutoff, FVG/OB hierarchy, plugins
- `specs/alpha-outcome-policy.md` — outcomes
- `specs/research-to-bot-execution-adapter.md` — inbox shapes (adapter extension needed)
- `structure_zones.py` — FVG/OB detect + state machine
- `strategy_v2_context.py` — bars, bias, ATR, zone soft scores
- `confluence_scoring.py` — proximity bins + weighted confluence
- Source idea: Crypto Liquidity Sweep Reversal (Freqtrade-style mechanical model) —
  risk sizing, leverage, and venue order placement **not** ported (engine-owned)

## Implementation contract (repo-aligned)

Mirror **`rsi_reclaim_v1.py`** structure. Do not invent a parallel plugin shape.

### Identity constants

```python
STRATEGY_ID = "liquidity-sweep-reversal-v1"
SETUP_CLASS = "liquidity_reversal"
PHASE = "armed_impulse_retracement"
PLUGIN_VERSION = "v1"
```

### Public entrypoints

```text
LsrV1Config (frozen dataclass) + load_config()   # reads config.LSR_V1_*
evaluate_symbol(bars_15m, *, asset, symbol, cutoff, zones=None, cfg=None, ...) -> dict | None
evaluate(conn, cutoff=None, *, cfg=None, snapshot=None, alpha_db_path=None, outbox_dir=None) -> list[dict]
run_plugin(cutoff_id: str, snapshot: dict) -> list[dict]
```

### `run_plugin` (exact platform contract)

```text
run_plugin(cutoff_id, snapshot):
  conn = config.get_db_connection(read_only=True, db_path=snapshot["db_path"])
  cutoff = completed_cycle(snapshot.get("now") or utc_now)
  events = evaluate(conn, cutoff, snapshot=snapshot)
  for each event:
    stamp input_snapshot_id=cutoff_id, plugin_version
    write_event(ev)   # alpha_outbox; only append if created
  return newly written events
```

Orchestrator path (already shipped):

```text
orchestrator → invoke_plugins_for_cutoff(db_path, cutoff_id, now=...)
  snapshot = {db_path, now, cutoff_id, feature_snapshots, zones?}
  → plugin.run(cutoff_id, snapshot)
```

### Bars and candidates (must use shared loaders)

```text
list_candidate_symbols(conn, cutoff) -> [(native_symbol, asset)]
load_15m_bars(conn, symbol, cutoff)   # source_end < cutoff; CA prefer; LOOKBACK_DAYS=16
last_completed_bar_fresh(bars_15m, cutoff)
resample_ohlcv(bars_15m, "1h"|"4h")
atr_last / ema_last / structure_bias_4h / zone_bias_4h / resolve_bias
compute_htf_zones or snapshot_zones_for_asset(snapshot, asset)
zone_stack_and_ltf_scores
has_active_event(strategy_id, asset, direction, ...)
weighted_confluence / confidence_from_confluence
```

Do **not** open raw SQL for OHLCV in the plugin. Do **not** require a durable
`SweepState` table in v1 — rebuild state each cutoff from bars only.

### Registration and purity (required wiring)

| Touch | Action |
|-------|--------|
| `strategy_plugins.KNOWN_STRATEGIES` | add `"liquidity-sweep-reversal-v1"` |
| `register(StrategyPlugin(...))` | `("bars_15m",)`, optional `("fvg_1h","fvg_4h","vp")`, `run=run_plugin` |
| `config.PRICE_STRUCTURE_STRATEGY_IDS` | **must include** this id (else non-pure write blocked / unknown) |
| `config.STRATEGY_ENABLED_IDS` | **opt-in only** — not in default allowlist |
| `config.LSR_V1_*` | knobs (see Config) |
| `.env.example` | document knobs + enable comment |

### Publisher event shape (before `write_event`)

`write_event` adds `alpha_id` + `dedupe_key` from
`strategy_id|asset|direction|observed_at`.

Plugin must supply all of `signal_publisher.REQUIRED_FIELDS` except those two:

```text
schema_version: 1
strategy_id, asset, direction, setup_class, phase
observed_at, valid_until, horizon_minutes
confidence, confidence_status="uncalibrated"
entry_condition: { "type": "limit_at_impulse_mid", "price": <float> }
  # optional extra keys on entry_condition OK (impulse_low/high) if JSON-safe
invalidation_price: float
targets: [ single 2.0R float ]
feature_snapshot: { source_symbol, confluence_score, confidence_components,
                    component_raw, ... thesis fields ..., completed_bar_at }
plugin_version: "v1"
# internal only, strip before write: _confluence_score
```

### Outcomes and exec

| Path | Behavior |
|------|----------|
| `outcome_evaluator` | Non-`breakout_*` types already use **limit fill** semantics (`low<=entry` long / `high>=entry` short). **No change required** for v1. |
| `execution_adapter` | Only `limit_at_ema_context` today. LSR stays Telegram/Discord until separate adapter ticket. |

### Delivery labels (required touch)

Today `discord_format._family` / `signal_publisher` family ternary fall through to
**"Impulse ignition"** for unknown `setup_class` (also mislabels
`continuation_pullback`).

Must add:

```text
liquidity_reversal     → "Liquidity reversal"
continuation_pullback  → "Continuation"   # already true via startswith in discord;
                         # signal_publisher must match (startswith continuation)
```

Snapshot context strings should surface PDH/PDL, sweep depth ATR, FVG magnet when
present in `feature_snapshot`.

## Thesis

After **HTF directional context is against the eventual trade** (bearish context
for longs, bullish for shorts), price **sweeps previous-UTC-day liquidity**
(PDL for long / PDH for short), **reclaims** back through that level on the same
15m candle, then **breaks 15m market structure** (close through a frozen pivot).
Arm a **limit at the 50% retracement of the sweep→BOS impulse**, preferably
where that midpoint **intersects an unfilled FVG/OB magnet** in trade direction.

The producer emits a falsifiable **armed limit** thesis. It does not place
orders, size stakes, or manage 2R exits in live venues.

### Distinct from existing families

| Family | Difference |
|--------|------------|
| `impulse-ignition-v2` | Arms **break of 1h compression lid** (expansion *out of* base). No PDH/PDL sweep, no BOS freeze. |
| `continuation-breakout-v2` | **With-trend** 4h + 1h flag breakout. This family is a **counter-context reclaim** after stop-run liquidity. |
| `accumulation-base-v2` | Limit at **1h EMA99** inside coil. No liquidity sweep geometry. |
| `rsi-reclaim-v1` | **Confirmed** 15m EMA/RSI reclaim at bar close. No PDH/PDL, no pivot BOS, no impulse midpoint limit. |

```text
4h   hard bias shell (structure EMA48 + nearest FVG/OB agree-or-abstain)
     → trade is a REVERSAL relative to that context
1h   FVG/OB refinement + optional displacement quality; not entry home
15m  PDH/PDL sweep + reclaim + pivot freeze + BOS + impulse midpoint limit
```

### Core hypothesis (directional)

**Long**

1. HTF context bearish (see bias gate).
2. 15m sweeps **PDL**, closes back **above** PDL, depth in ATR band.
3. Freeze last confirmed 15m **pivot high** as structure level.
4. Within validity window, 15m **closes above** that level (BOS).
5. Limit at **50%** of sweep-low → impulse-high range (FVG-aware adjustment below).

**Short** — mirror with PDH, pivot low, close below, 50% of impulse-high → impulse-low.

---

## Time and candle conventions

All day boundaries and cutoffs use **UTC**.

```text
UTC day D:  00:00:00 UTC → 23:59:59.999 UTC
Cutoff:     finalized completed 15m bar only (source_end < cutoff)
```

| TF | Role |
|----|------|
| **4h** | Hard bias + primary FVG/OB structural context |
| **1h** | Secondary FVG/OB refinement; soft displacement / zone stack |
| **15m** | Trigger home: PDH/PDL, pivots, sweep, BOS, impulse, entry, observed_at |

No 5m bars. No weekly liquidity levels in v1.

Platform zone hierarchy (unchanged):

```text
4h FVG / OB  = primary structural context
1h FVG / OB  = secondary refinement (4h wins on conflict)
15m          = trigger geometry only
             = may compute ephemeral 15m FVG for soft score + entry refine
             = MUST NOT materialize 15m zones into structure_zones table
```

---

## Component map (maximize analyst stack)

### Reuse as-is

| Component | Module | Use in this family |
|-----------|--------|--------------------|
| PIT 15m bars + resample | `strategy_v2_context` | All geometry |
| ATR(14) | `atr_last` / `structure_zones.compute_atr` | Sweep depth, stop buffer, proximity, r_max |
| `structure_bias_4h` (EMA48) | `strategy_v2_context` | HTF context leg |
| `zone_bias_4h` + `resolve_bias` | shared | Agree-or-abstain with nearest 4h FVG/OB |
| `compute_htf_zones` / `detect_fvg` / `detect_order_blocks` | `structure_zones` | Hard zone bias + soft stack + entry magnets |
| `zone_stack_and_ltf_scores` | shared | Soft `ltf_inside_htf`, `zone_stack_tightness` |
| Proximity bins (0.25 / 0.75 ATR) | `confluence_scoring` | Zone distance scoring |
| `weighted_confluence` + `confidence_from_confluence` | `confluence_scoring` | Soft rank → uncalibrated confidence |
| `has_active_event` re-arm | shared | Anti-spam per asset+direction |
| `S_min` + top-N emit floor | plugin pattern | Traffic control |
| Alpha outbox + publisher | `alpha_outbox`, `signal_publisher` | Delivery |
| Optional approx/native VP | feature snapshots | Soft `vp_proximity` only |
| OI / funding on bars | CA payload | Soft pressure terms only |
| Optional LLM booster | research path | Post-emit stance only |
| Outcome evaluator | `outcome_evaluator` | Post-expiry descriptive outcomes |

### Must build (shared modules — not optional)

These are **first-class deliverables** of this strategy workstream. Geometry
rules already live in this spec (sections below); the modules implement them.
The plugin **must not** inline PDH/PDL, pivot, or sweep/BOS math.

| Ship order | Module file | Owns | Plugin may not reimplement |
|------------|-------------|------|----------------------------|
| 1 | **`session_levels.py`** | UTC PDH/PDL from completed 15m | session high/low aggregation |
| 2 | **`market_structure.py`** | Confirmed 2-left/2-right pivots | swing freeze for BOS |
| 3 | **`liquidity_sweep.py`** | Sweep+reclaim, freeze, pre-BOS invalidate, BOS close, impulse range | stop-run state machine |
| 4 | **`liquidity_sweep_reversal_v1.py`** | Bias reverse, FVG refine, score, emit | — (family only) |

**Gate rule:** plugin registration / enable is blocked until modules 1–3 exist,
are unit-tested, and are imported by the plugin. “Plugin-only prototype” that
duplicates helper math is out of scope.

Plugin-local only (not shared modules):

| Concern | Home |
|---------|------|
| Impulse 50% entry + FVG/OB snap | plugin |
| Soft confluence weight map | plugin |
| Optional displacement / close-location hard toggles | plugin config calling helpers |
| Outbox event shape | plugin |

Do **not** overload `impulse-ignition` compression helpers as the sweep detector.
Do **not** reuse OB’s 20-bar swing as the BOS pivot series.

---

## Shared modules — locked contracts (must implement)

Point-in-time only: every function takes **completed** 15m bars with
`source_end < cutoff` (or equivalent already filtered frame). No future bars.

### M1 — `session_levels.py` (PDH / PDL)

**Purpose:** previous UTC calendar day extremes for the bar’s current day.

```text
pdh_pdl(bars_15m, asof_ts) -> { pdh, pdl, prior_utc_day, bar_count } | None
pdh_pdl_series(bars_15m) -> DataFrame columns [bar_ts, utc_day, pdh, pdl]
  # optional vectorized helper for replay tests
```

Rules (normative — same as “Previous-day liquidity levels”):

```text
For bar with UTC day D:
  PDH(D) = max(high) of completed 15m with UTC day D-1
  PDL(D) = min(low)  of completed 15m with UTC day D-1
```

- Developing day-D high/low is never PDH/PDL.
- Return `None` / fail if prior day has zero bars or incomplete warmup policy
  (document minimum: at least one completed bar on D−1; prefer full session).
- Timezone: **UTC only**.
- Inputs: OHLCV frame sorted by time; no network I/O.

**Unit tests required:**

- [ ] Known two-day fixture → exact PDH/PDL
- [ ] First bar of new UTC day still uses prior day (not developing day)
- [ ] Incomplete / empty prior day → None
- [ ] No lookahead: dropping last N bars of day D does not change PDH/PDL(D)

### M2 — `market_structure.py` (2/2 pivots)

**Purpose:** confirmed swing highs/lows for BOS structure freeze.

```text
confirmed_pivot_highs(bars_15m, left=2, right=2) -> list[{index, ts, price}]
confirmed_pivot_lows(bars_15m, left=2, right=2) -> list[{index, ts, price}]
latest_confirmed_pivot_high(bars_15m, asof_index, left=2, right=2) -> pivot | None
latest_confirmed_pivot_low(bars_15m, asof_index, left=2, right=2) -> pivot | None
```

Normative confirm (defaults left=right=2): at completed bar index `t`, bar
`t−right` is a pivot high iff its high is strictly greater than highs of
`t−right−left .. t−right−1` and `t−right+1 .. t` (same for lows with strict
less). See “15m swing definition”.

- Pivot is unknown until right-hand bars complete.
- `latest_confirmed_*` only returns pivots whose confirm bar index ≤ `asof_index`.
- Defaults locked to 2/2 for this strategy; params exist for tests only.
- Separate from `structure_zones.detect_order_blocks` 20-bar extreme.

**Unit tests required:**

- [ ] Synthetic peak confirms exactly two bars later, not earlier
- [ ] Forming swing not returned at `asof` before right bars exist
- [ ] `latest_confirmed_pivot_high` at sweep bar ignores later pivots
- [ ] left/right=2 golden vectors

### M3 — `liquidity_sweep.py` (sweep / reclaim / BOS / impulse)

**Purpose:** stop-run state geometry used by the plugin hard gates.

```text
# ATR at bar index from caller (plugin uses atr series / atr_last pattern)
qualify_bullish_sweep(bar, pdl, atr, min_atr=0.10, max_atr=1.00) -> SweepQual | None
qualify_bearish_sweep(bar, pdh, atr, min_atr=0.10, max_atr=1.00) -> SweepQual | None
  # SweepQual: depth, depth_atr, extreme (low|high), close_location

arm_long_sweep(bars, sweep_index, structure_level, sweep_atr, ...) -> SweepState
arm_short_sweep(...) -> SweepState
  # freezes: structure_level, sweep_extreme, sweep_atr, sweep_index, direction

advance_sweep_state(state, bars, through_index, bos_window=8) -> SweepState
  # transitions: armed | cancelled_extreme | cancelled_expiry | bos_confirmed

bos_long(close, structure_level) -> bool   # close > level
bos_short(close, structure_level) -> bool  # close < level

impulse_long(bars, sweep_index, bos_index, sweep_low) -> {impulse_low, impulse_high}
impulse_short(bars, sweep_index, bos_index, sweep_high) -> {impulse_low, impulse_high}

entry_mid(impulse_low, impulse_high, retrace_pct=0.50) -> float
invalidation_long(sweep_low, sweep_atr, buf=0.15) -> float
invalidation_short(sweep_high, sweep_atr, buf=0.15) -> float

optional:
  displacement_ok(bar, avg_body_20, mult=1.50, direction) -> bool
  close_location(bar) -> float | None
```

State machine (must match “State machine” section):

```text
idle → (qualify + freeze pivot) → sweep_armed
sweep_armed → extreme broken | window > bos_window → cancelled → idle
sweep_armed → BOS close through structure → bos_confirmed
```

- Wick beyond structure without close → **not** BOS.
- Long cancel if any bar low < SweepLow before BOS; short mirror.
- Impulse high/low inclusive of sweep bar through BOS bar.
- Pure functions preferred; plugin owns per-asset day-cap and emit.

**Unit tests required:**

- [ ] Depth below min / above max → no qualify
- [ ] Reclaim fail (close still beyond PD level) → no qualify
- [ ] Armed then new low below SweepLow → cancelled_extreme
- [ ] No BOS within 8 bars → cancelled_expiry
- [ ] Close through structure → bos_confirmed; wick-only → still armed
- [ ] Impulse range and 50% mid golden values
- [ ] Invalidation buffer 0.15 ATR
- [ ] Replay: state rebuild from bars alone matches step-by-step advance

### Composition (plugin wires M1–M3)

M3 APIs take **already-resolved** `pdh`/`pdl` and `structure_level` as arguments.
M3 does **not** import M1/M2. The plugin composes:

```text
session_levels.pdh_pdl(bars, asof)
market_structure.latest_confirmed_pivot_*(bars, asof_index)
liquidity_sweep.qualify_* / arm_* / advance_* / impulse_* / entry_mid / invalidation_*
structure_zones.detect_fvg(..., tf="15m")   # ephemeral only; never persist
strategy_v2_context + confluence_scoring     # bias, zones HTF, score, re-arm
```

```text
session_levels.py     market_structure.py     structure_zones.py
        \                    |                      |
         \                   |                      |
          v                  v                      v
              liquidity_sweep_reversal_v1.py
                    ^
                    |
            liquidity_sweep.py   (pure geometry; no M1/M2 import)
```

### State rebuild (no durable sweep table)

Each `evaluate_symbol` call at cutoff:

1. Load PIT 15m bars (`source_end < cutoff`).
2. Walk completed bars with enough history for PDH/PDL (prior UTC day) plus
   pivot right-hand and `bos_window` (full `load_15m_bars` lookback is enough)
   and reconstruct whether a sweep is armed and whether the **last completed
   bar** is a fresh BOS for emit.
3. Emit only when BOS confirms on the **last completed** 15m bar (same cadence as
   other confirmed/armed plugins that fire on the bar that completes the thesis).
   Do not re-emit historical BOS from earlier in the day on later cutoffs
   (day-cap + dedupe + “BOS bar == last bar” rule).

### Explicit non-reuse (wrong concept)

| Name in stack | Why not |
|---------------|---------|
| `liquidity_tier` (core/emerging) | Universe quality, not stop-run liquidity pools |
| Ignition “base lid” | Compression breakout, opposite narrative |
| RSI reclaim touch/reclaim | Different trigger; optional soft only later |
| `limit_at_ema_context` exec path | Entry is impulse midpoint, not EMA99 |

---

## Pipeline

```text
finalized 15m cutoff snapshot
        │
        v
  load PIT 15m → resample 1h/4h → ATR / EMA48_4h
        │
        v
  HTF zones (4h/1h FVG+OB) → structure_bias + zone_bias → resolve_bias
        │ fail closed
        v
  session levels: PDH/PDL for current UTC day
        │
        v
  maintain / update active sweep state (per asset+direction)
        │  — new qualifying sweep freezes pivot + SweepExtreme + SweepATR
        │  — invalidate if extreme broken before BOS
        │  — expire if no BOS within 8×15m
        v
  BOS on last completed 15m? (close through frozen structure level)
        │ no → no emit (state may still be armed)
        v
  build impulse + entry limit (+ optional FVG refine)
        │
        v
  hard geometry (risk, r_max, validity)
        │
        v
  soft confluence_score (FVG/OB/VP/sweep quality/…)
        │
        v
  emit floor: score ≥ S_min AND top-N this cutoff
        │
        v
  re-arm: no other active liquidity-sweep-reversal-v1 for asset+direction
        │  (+ UTC-day side cap: max 1 BOS emit per side per day)
        v
  write alpha event (armed limit) → outbox → publisher
        │
        v (optional)
  LLM booster (stance / delivery priority only)
```

---

## Previous-day liquidity levels

For every completed 15m bar whose UTC calendar day is `D`:

```text
PDH(D) = max(high) of all completed 15m bars with UTC day = D−1
PDL(D) = min(low)  of all completed 15m bars with UTC day = D−1
```

Rules:

- Developing day-D high/low is **never** PDH/PDL.
- If day D−1 has insufficient bars (warmup / listing), hard-fail that symbol.
- No weekly / monthly session levels in v1.
- Levels are pure price structure (no volume clustering required).

---

## 15m swing definition (BOS reference)

Confirmed **2-left / 2-right** pivot (same intent as source model).

### Pivot high

At completed bar `t`, bar `t−2` is a confirmed pivot high iff:

```text
High[t-2] > High[t-4]
High[t-2] > High[t-3]
High[t-2] > High[t-1]
High[t-2] > High[t]
```

### Pivot low

```text
Low[t-2] < Low[t-4]
Low[t-2] < Low[t-3]
Low[t-2] < Low[t-1]
Low[t-2] < Low[t]
```

Only confirmed pivots may freeze as structure levels. Never use a forming swing.

Note: `detect_order_blocks` already uses a **20-bar swing** for displacement
through prior extremes. That remains the OB definition. **BOS pivots are a
separate 2/2 series** and must not silently share OB’s 20-bar extreme.

---

## Hard gates

All must pass. Few by design.

### 1. Fresh completed 15m

- Last completed 15m bar strictly before cutoff.
- `last_completed_bar_fresh` (existing rule).
- Series warm enough for: 4h EMA48, ATR14 on 15m, ≥1 full prior UTC day of 15m bars, pivot right-hand confirmation.

### 2. 4h bias (agree-or-abstain) — counter-context trade

Same primitives as other v2 families:

| Signal | Definition |
|--------|------------|
| `structure_bias` | Last completed 4h close vs EMA48_4h |
| `zone_bias` | Nearest `active\|partial` 4h FVG/OB by midpoint distance |

`resolve_bias` → `context_direction` or fail.

**Trade direction is the reverse of context:**

| `context_direction` | Allowed trade |
|---------------------|---------------|
| `long` (bullish HTF) | **short** reversal only |
| `short` (bearish HTF) | **long** reversal only |
| missing / conflict | fail |

Rationale: source model used 1h EMA20/50 as “context against the reverse.” This
repo’s locked HTF shell is **4h EMA48 + zone**. Do not invent a second hard
structure-bias system in v1; optional soft term may still score 1h EMA20/50
alignment with the source model for research A/B.

Both long and short emissions are allowed across the universe (each on its own
counter-context).

### 3. Qualifying sweep + reclaim (15m)

#### Long sweep

On some completed 15m bar `s` still inside the active window:

```text
Low[s]  <  PDL
Close[s] > PDL
SweepDepth = PDL - Low[s]
0.10 * ATR_15m[s]  ≤  SweepDepth  ≤  1.00 * ATR_15m[s]
```

Freeze at `s`:

```text
LongStructureLevel = most recently confirmed 15m pivot high at or before s
SweepLow           = Low[s]
SweepATR           = ATR_15m[s]
SweepBarAt         = end time of s
```

If no confirmed pivot high exists → fail (cannot arm structure).

#### Short sweep

```text
High[s]  >  PDH
Close[s] <  PDH
SweepDepth = High[s] - PDH
0.10 * ATR_15m[s]  ≤  SweepDepth  ≤  1.00 * ATR_15m[s]
```

Freeze:

```text
ShortStructureLevel = most recently confirmed 15m pivot low at or before s
SweepHigh / SweepATR / SweepBarAt
```

#### Optional reclaim close-location (baseline OFF as hard gate)

```text
CloseLocation = (Close - Low) / (High - Low)   # 0 if High==Low → fail soft only
```

If config `REQUIRE_CLOSE_LOCATION=true`:

- long: `CloseLocation > 0.50`
- short: `CloseLocation < 0.50`

Default **OFF** (soft score still uses close location).

### 4. Sweep validity window (pre-BOS)

```text
max life = 8 × 15m = 120 minutes from SweepBarAt
```

Cancel active sweep state if:

- no BOS within 8 completed 15m bars after `s`, or
- long: any bar makes `Low < SweepLow` before BOS, or
- short: any bar makes `High > SweepHigh` before BOS

A new qualifying sweep may replace cancelled state if the UTC-day side emit cap
has not been consumed.

### 5. Break of structure (15m close)

**Long BOS** on completed bar `b` with `s ≤ b ≤ s+8`:

```text
Close[b] > LongStructureLevel
```

Wick alone is insufficient.

**Short BOS:**

```text
Close[b] < ShortStructureLevel
```

#### Optional displacement (baseline OFF as hard gate)

```text
Body = |Close - Open|
AverageBody20 = SMA(|Close - Open|, 20) on 15m
```

If `REQUIRE_DISPLACEMENT=true`:

- long: `Body > 1.50 * AverageBody20` and `Close > Open`
- short: `Body > 1.50 * AverageBody20` and `Close < Open`

Default **OFF** (soft `displacement_quality` still scores this).

### 6. Impulse + entry geometry

#### Long

```text
ImpulseLow  = SweepLow
ImpulseHigh = max(High) from bar s through bar b inclusive
EntryMid    = ImpulseLow + 0.50 * (ImpulseHigh - ImpulseLow)
```

#### Short

```text
ImpulseHigh = SweepHigh
ImpulseLow  = min(Low) from bar s through bar b inclusive
EntryMid    = ImpulseHigh - 0.50 * (ImpulseHigh - ImpulseLow)
```

Require `ImpulseHigh > ImpulseLow` and finite prices.

### 7. FVG / OB entry refinement (hard-safe, soft-preferred)

Goal: **use FVG/OB as much as possible** without inventing levels.

On BOS bar `b`, collect unfilled (`active|partial`) zones in **trade direction**:

- primary: 4h FVG/OB
- secondary: 1h FVG/OB
- ephemeral (not persisted): 15m FVG formed on/after sweep bar `s` through `b`
  in trade direction (same `detect_fvg` math, trigger-only)

**Refinement rule (deterministic):**

1. Start with `entry = EntryMid`.
2. Among candidate zones whose price range **overlaps the impulse range**
   `[ImpulseLow, ImpulseHigh]` and lies on the correct side of invalidation:
   - Prefer 4h over 1h over 15m-ephemeral.
   - Prefer FVG over OB when both same TF and both overlap midpoint band.
3. If a preferred zone’s **midpoint** lies within `0.25 * ATR_15m` of `EntryMid`,
   set:

   ```text
   entry = zone_midpoint
   ```

   (snap to magnet — still a limit, not a chase).
4. Else if the zone range contains `EntryMid`, keep `EntryMid` but tag
   `entry_inside_fvg=true` for soft score.
5. Else keep `EntryMid`; missing zones never fail this gate.

Never move entry **beyond** the impulse range. Never reprice toward live mid
after emit.

### 8. Invalidation + risk envelope

ATR frozen from sweep:

```text
# Long
invalidation = SweepLow - 0.15 * SweepATR

# Short
invalidation = SweepHigh + 0.15 * SweepATR
```

```text
risk = |entry - invalidation|
require risk > 0
if risk > r_max * ATR_15m[b]: fail   # do not clamp stop
```

Optional worse-of with nearest opposing HTF zone extreme may be added only as a
**soft research field** in v1; hard invalidation stays sweep extreme + buffer so
the thesis remains falsifiable against the stop-run.

### 9. Target (producer thesis)

Single target at **2.0R** (source model; differs from other families’ 1.5R):

```text
# Long
targets = [ entry + 2.0 * risk ]

# Short
targets = [ entry - 2.0 * risk ]
```

Stretch / opposite-liquidity / next FVG levels may appear in `feature_snapshot`
only (research), not as multi-target outcome contracts in v1.

### 10. Valid trigger types + horizon

```text
entry_condition.type = limit_at_impulse_mid
# price = refined entry
# optional metadata: impulse_low, impulse_high, retrace_pct=0.50
```

```text
observed_at   = end of BOS 15m bar b
valid_until   = observed_at + 120 minutes   # 8×15m entry expiry (source model)
horizon_minutes = 120
```

If the limit is not filled by the engine within validity, the alpha expires;
producer does not reprice.

### 11. UTC-day side cap + re-arm

Two independent blocks (both must pass):

#### A. Live re-arm (`has_active_event`)

While a non-terminal `liquidity-sweep-reversal-v1` event exists for the same
**asset + direction** (alpha_events `status=active` **or** outbox
`valid_until > now`), do not emit another. Uses shared
`strategy_v2_context.has_active_event`.

#### B. UTC-day side cap (plugin-local; **beyond** re-arm)

For each **asset + UTC calendar day of `observed_at` + direction**:

```text
maximum 1 BOS emit ever that day for that side
```

Even if the first limit later **expires unfilled**, no second emit that UTC day.

**Algorithm (v1 — no new DB table):**

```text
emitted_today(strategy_id, asset, direction, utc_day, *, alpha_db_path, outbox_dir) -> bool
  True if any of:
    1. alpha_events row with matching strategy_id, asset, direction
       and UTC date of observed_at == utc_day
       (any status: active, expired, outcome-closed — count all)
    2. outbox JSON with same identity fields and observed_at UTC day == utc_day
```

Implement as a small helper in the plugin file (or later lift to
`strategy_v2_context` if a second family needs it). Unit-test with temp outbox
+ temp alpha DB like `test_rsi_reclaim_v1` re-arm tests.

**Emit-bar rule:** only consider BOS when the BOS bar is the **last completed**
15m bar in the PIT frame (prevents replaying older BOS every cutoff).

---

## Soft score (`confluence_score`)

Hard gates decide eligibility. Soft basket ranks quality and uses **as many
analyst components as possible**.

Component **keys must match** `weighted_confluence` conventions used by shipped
plugins (`zone_stack_tightness`, penalty key `contradiction_penalty`). Do not use
ad-hoc `w_*` names as component keys.

| Component key | Source | Notes |
|---------------|--------|-------|
| `ltf_inside_htf` | `zone_stack_and_ltf_scores` | Entry/ref vs 4h/1h zones |
| `zone_stack_tightness` | shared | FVG∩OB cluster in **trade** direction |
| `fvg_entry_magnet` | zones + refine | 1.0 snap/inside preferred FVG; partial if near; 0 if none |
| `ob_alignment` | zones | Trade-direction OB near entry or impulse origin |
| `vp_proximity` | optional VP from `snapshot["feature_snapshots"]` | 0 if unavailable; never invent native VP; never treat approx candle VP as native |
| `sweep_depth_quality` | geometry | Prefer mid-band depth (~0.25–0.60 ATR); tails score lower |
| `reclaim_close_location` | 15m sweep bar | Long high close location; short low |
| `displacement_quality` | BOS bar body vs SMA20 body | Scores even when hard displacement OFF |
| `bos_clarity` | close distance beyond structure / ATR | Strong close-through > barely tag |
| `impulse_quality` | impulse range / ATR | Neither micro nor monstrous |
| `htf_zone_oppose_context` | 4h zones | Bonus when context-side zones sat at the swept PD level |
| `structure_context_strength` | \|close_4h − EMA48\| / ATR_4h | Clearer HTF lean (still counter-traded) |
| `session_level_freshness` | bars into UTC day | Early-day first touch of PDH/PDL can score higher |
| `oi_funding_soft` | bar columns if present | Mild soft only — **never hard** |
| `candle_quality` | BOS 15m body/wick | Same style as other plugins |
| `contradiction_penalty` | residual | `weighted_confluence` treats `*_penalty` as negative |

Map (same as other families):

```text
score, parts = weighted_confluence(components, weights)
confidence, confidence_status = confidence_from_confluence(score)
# confidence_status always "uncalibrated"
feature_snapshot.confidence_components = parts
feature_snapshot.confluence_score = score
feature_snapshot.component_raw = components
```

Never treat component count as P(win).

### Default weights (keys == component keys)

```text
ltf_inside_htf:              0.12
zone_stack_tightness:        0.12
fvg_entry_magnet:            0.14
ob_alignment:                0.08
vp_proximity:                0.06
sweep_depth_quality:         0.10
reclaim_close_location:      0.06
displacement_quality:        0.08
bos_clarity:                 0.06
impulse_quality:             0.06
htf_zone_oppose_context:     0.04
structure_context_strength:  0.04
session_level_freshness:     0.02
oi_funding_soft:             0.02
candle_quality:              0.04
contradiction_penalty:       0.10
```

Override via `LSR_V1_WEIGHT_<KEY>` only if needed; defaults live in plugin
`load_config` / frozen dataclass like other families.

---

## Emit floor

Same batch pattern as `rsi_reclaim_v1.evaluate` (inline top-N; no shared
`select_top_n` helper exists yet — backlog only):

1. Score all symbols that pass hard gates 1–10 this cutoff  
2. Keep `confluence_score ≥ LSR_V1_S_MIN`  
3. Sort descending; take top `LSR_V1_N_TOP`  
4. Drop if `has_active_event` **or** `emitted_today` for asset+direction  
5. Strip `_confluence_score`; `write_event`

---

## Trigger (bot-facing output)

| Field | Rule |
|-------|------|
| `strategy_id` | `liquidity-sweep-reversal-v1` |
| `setup_class` | `liquidity_reversal` |
| `phase` | `armed_impulse_retracement` |
| `direction` | `long` \| `short` (reversal side) |
| `entry_condition` | `{ "type": "limit_at_impulse_mid", "price": entry }` |
| `entry` | FVG-refined 50% impulse midpoint |
| `invalidation_price` | Sweep extreme ± `0.15 * SweepATR` |
| `targets` | Single **2.0R** |
| `observed_at` | End of BOS 15m bar |
| `valid_until` | `observed_at + 2h` |
| `horizon_minutes` | `120` |
| `confidence` / `confidence_status` | from confluence; always `uncalibrated` |

Engine-owned (not emitted as producer lifecycle): leverage, stake fraction,
trailing, breakeven, funding payments, venue reduce-only exits.

### ATR freeze vs r_max (exact)

| Quantity | Bar |
|----------|-----|
| `SweepATR` (invalidation buffer) | frozen at **sweep** bar `s` |
| `ATR_15m` for `r_max` gate | measured at **BOS** bar `b` |
| FVG snap distance `FVG_SNAP_ATR` | `ATR_15m` at BOS bar `b` |

### Execution adapter

Current adapter only forwards `limit_at_ema_context`. This family emits
`limit_at_impulse_mid` → **Telegram/Discord research path first**.

Adapter extension (separate ticket) should accept:

```text
entry_condition.type in {
  limit_at_ema_context,      # existing
  limit_at_impulse_mid       # this family
}
```

with the same geometry checks (stop/entry/target order). Do not force-fit EMA
entry type. **Not** in v1 acceptance.

---

## Feature snapshot (minimum)

```text
source_symbol, confluence_score, confidence_components, component_raw,

# refs
close_15m, close_1h, close_4h,
ema48_4h, atr_15m, atr_1h, atr_4h,
structure_bias, zone_bias, context_direction, trade_direction,
nearest_zone_4h, nearest_zone_1h,

# session / sweep
utc_day, pdh, pdl,
sweep_bar_at, sweep_depth, sweep_depth_atr,
sweep_low | sweep_high, sweep_atr,
close_location_sweep,

# structure
pivot_structure_level, pivot_confirmed_at,
bos_bar_at, bos_close, displacement_body, average_body_20,
impulse_low, impulse_high, entry_mid, entry_refined,
entry_inside_fvg, fvg_magnet_tf, fvg_magnet_id,
ob_near_entry,

# risk
entry, invalidation, risk, r_multiple_target,
completed_bar_at
```

Optional research-only: `stretch_next_fvg`, `opposite_pd_level`, 1h EMA20/50
context echo, funding/OI raw.

---

## State machine (plugin-local, PIT-replayable)

Per `asset + direction`, durable only inside cutoff evaluation (rebuild from bars;
do not require cross-process memory):

```text
idle
  │ qualifying sweep+reclaim
  v
sweep_armed  (frozen structure, sweep extreme, expiry clock)
  │ extreme broken OR time > 8 bars
  ├──────────────► idle (cancel)
  │ BOS close
  v
bos_confirmed → score → maybe emit armed_impulse_retracement
  │
  v
idle (day-side cap consumed) or wait next day
```

All state must be reconstructible from finalized bars at cutoff (lookahead-safe).

---

## Config knobs

Env prefix: **`LSR_V1_*`**. Load via `getattr(config, "LSR_V1_...", default)` like
`rsi_reclaim_v1.load_config`. Add matching attributes in `config.py` and
`.env.example`.

**Deliberate deltas vs other families** (do not copy-paste rsi/accum defaults):

| | LSR v1 | accum / ign / cont / rsi |
|--|--------|---------------------------|
| Target R | **2.0** | 1.5 |
| Horizon | **120 min** | 240 min |
| `R_MAX` | **3.0** | 2.5 |
| Entry type | `limit_at_impulse_mid` | ema / breakout_* |

Locked multiples at ship (change only via explicit research decision):

| Locked | Value |
|--------|--------|
| Pivot left/right | 2 / 2 |
| ATR period | 14 |
| Structure EMA | 48 on 4h (`structure_bias_4h`) |
| Sweep depth band | 0.10–1.00 ATR |
| Sweep→BOS validity | 8 × 15m |
| Entry retracement | 50% |
| Entry validity | 120 minutes |
| Stop buffer | 0.15 × SweepATR |
| Target | 2.0R |
| Zone proximity bins | 0.25 / 0.75 ATR |
| Zone states for bias | active + partial |
| Displacement hard | OFF |
| Close-location hard | OFF |
| 15m FVG table materialization | forbidden |
| Default in `STRATEGY_ENABLED_IDS` | **no** (opt-in) |

| Env / config attribute | Default | Role |
|------------------------|---------|------|
| `LSR_V1_S_MIN` | 0.55 | confluence floor |
| `LSR_V1_N_TOP` | 3 | top-N absolute emit cap |
| `LSR_V1_R_MAX` | 3.0 | max risk / ATR_15m at BOS |
| `LSR_V1_SWEEP_MIN_ATR` | 0.10 | min penetration |
| `LSR_V1_SWEEP_MAX_ATR` | 1.00 | max penetration |
| `LSR_V1_STOP_ATR_BUF` | 0.15 | invalidation buffer |
| `LSR_V1_RETRACE_PCT` | 0.50 | impulse midpoint |
| `LSR_V1_BOS_WINDOW` | 8 | bars sweep→BOS |
| `LSR_V1_ENTRY_HORIZON_MIN` | 120 | valid_until delta minutes |
| `LSR_V1_TARGET_R` | 2.0 | single target R |
| `LSR_V1_REQUIRE_DISPLACEMENT` | false | hard displacement |
| `LSR_V1_REQUIRE_CLOSE_LOCATION` | false | hard reclaim quality |
| `LSR_V1_FVG_SNAP_ATR` | 0.25 | snap entry to zone mid if within |
| `LSR_V1_USE_15M_EPHEMERAL_FVG` | true | trigger-only FVG in refine/score |
| `LSR_V1_PIVOT_LEFT` / `RIGHT` | 2 / 2 | tests/research only; product lock 2/2 |

---

## Universe and discovery

- Candidate symbols: same as other v2 plugins via `list_candidate_symbols` /
  warmed watchlist (ignition + continuation pools supply names; this thesis does
  not need a third discovery pool in v1).
- Initial research focus pairs (documentation only, not hard-coded filter):
  BTC, ETH, SOL USDT perps — then unchanged params on broader set.
- `liquidity_tier` may appear in snapshot for analysis; **not** a hard gate.

---

## Validation sequence (research)

Change one major dimension at a time. Prefer plateaus over knife-edge optima.

### Test 1 — Baseline

All locked defaults; hard displacement OFF; close-location OFF; FVG refine ON.

### Test 2 — Entry comparison (soft/research forks, separate strategy_id or flag)

Keep gates constant; compare snapshot research fields or parallel ids only if needed:

```text
BOS close (confirmed breakout_*) 
50% pullback limit (this baseline)
61.8% pullback limit
50% with FVG snap OFF
```

### Test 3 — Displacement hard ON vs OFF

### Test 4 — Close-location hard ON vs OFF

### Test 5 — Bias shell

```text
4h EMA48+zone (baseline)
structure-only (zone optional soft)
zone-only (structure optional soft)   # research only; not default product
```

### Test 6 — Target R plateau

```text
1.5R vs 2.0R vs 2.5R
```

### Lookahead / recursive discipline

- Pivots only after right-hand bars complete.
- PDH/PDL only from fully completed prior UTC day.
- Informative HTF from completed bars only (`source_end < cutoff`).
- Replay unit tests must fail if shifting future bars changes past emits.

Offline Freqtrade ports of the same geometry are allowed for **cost/fill research**
via `freqtrade_history` data, but live emission remains the alpha plugin path.

---

## Anti-overfitting rules

Do not jointly optimize:

- sweep min/max
- pivot width
- EMA period
- retrace %
- stop buffer
- target R
- BOS window
- all soft weights

Prefer broad acceptable plateaus. Document holdout periods; do not repeatedly
fit holdout.

Suggested research split (when history allows):

```text
Develop:  2021-01-01 → 2024-12-31
Holdout:  2025-01-01 onward
```

---

## Costs and pass/fail (research standard)

Producer outcomes are descriptive. Any offline backtest claiming viability must
include fees, funding when available, and slippage sensitivity.

Stronger initial pass (offline):

1. Positive expectancy after costs  
2. Acceptable max drawdown  
3. Enough trades  
4. Stable across >1 year  
5. No single month/trade dominates  
6. Reasonable BTC/ETH/SOL without per-pair retune  
7. No lookahead in replay tests  
8. Dry-run / live paper emits match backtest logic on shared windows  

v1 is a research strategy, not a profitability claim.

---

## Primary metrics (offline + outcome ledger)

```text
Total emits, long/short split
Fill rate (if engine feedback exists)
Win rate, avg win R, avg loss R, expectancy R/trade
Profit factor, max DD, max consecutive losses
Median/avg holding time
By year, by asset, by long/short
Confluence score decile vs outcome (calibration research only)
FVG-magnet vs pure-midpoint cohort comparison
```

---

## Implementation plan (ordered) — must ship all of M1–M3 before plugin enable

| Step | Deliverable | Done when |
|------|-------------|-----------|
| 0 | Spec lock (this doc + shared-lib) | audit adjustments accepted |
| 1 | **`session_levels.py` + `test_session_levels.py`** | M1 API + unit tests green |
| 2 | **`market_structure.py` + `test_market_structure.py`** | M2 API + unit tests green |
| 3 | **`liquidity_sweep.py` + `test_liquidity_sweep.py`** | M3 API + unit tests green |
| 4 | **`config.py` + `.env.example`** | `LSR_V1_*` + id in `PRICE_STRUCTURE_STRATEGY_IDS` |
| 5 | **`liquidity_sweep_reversal_v1.py` + `test_liquidity_sweep_reversal_v1.py`** | rsi-shaped plugin; imports M1–M3 for geometry |
| 6 | **`strategy_plugins.py`** | `KNOWN_STRATEGIES` + `register(...)` |
| 7 | **`discord_format.py` + `signal_publisher.py` + tests** | family label + snapshot context |
| 8 | **`test_strategy_plugins.py`** | known-set includes rsi + lsr |
| 9 | Optional: `execution_adapter` `limit_at_impulse_mid` | **separate ticket** |
| 10 | Flip shared-lib module map rows to “shipped” | docs match code |
| 11 | Optional: `agent.md` / README family bullet | operator discoverability |

### Files touched beyond M1–M3 + plugin

| File | Change |
|------|--------|
| `strategy_plugins.py` | known id + register |
| `config.py` | `LSR_V1_*`, `PRICE_STRUCTURE_STRATEGY_IDS` |
| `.env.example` | knobs + opt-in enable comment |
| `discord_format.py` | `_family("liquidity_reversal")` + context keys |
| `signal_publisher.py` | same family label (keep in sync with discord) |
| `test_discord_format.py` | label coverage |
| `test_strategy_plugins.py` | known set |
| `test_session_levels.py` | new |
| `test_market_structure.py` | new |
| `test_liquidity_sweep.py` | new |
| `test_liquidity_sweep_reversal_v1.py` | new |

**No change required for research v1 ship:** `outcome_evaluator.py`,
`structure_zones.py`, `confluence_scoring.py`, `strategy_v2_context.py` (reuse),
`execution_adapter.py` (deferred).

**Non-negotiable:** steps 1–3 are required build scope. Inlining PDH/pivot/sweep
only inside the plugin file **fails acceptance**.

Plugin tests (step 5) must cover:

- emit identity + phase + `limit_at_impulse_mid` + 2.0R + horizon 120
- fail: no pivot, depth OOB, bias conflict / agree fail, extreme broken, window expiry
- FVG snap within impulse
- re-arm (`has_active_event`) + UTC-day side cap (`emitted_today`)
- BOS only on last completed bar (no historical re-fire)
- no lookahead on pivot confirm
- geometry goes through M1–M3 modules (import/use, not copy-paste formulas)

---

## Non-goals (v1)

- Freqtrade strategy class or live order placement in this repo  
- Account risk %, leverage, stake math (engine-owned)  
- Order-book imbalance, liquidation heatmaps as hard gates  
- Weekly liquidity, equal-high/low clustering (candidate v1.1 soft)  
- CHOCH labeling beyond close-through BOS  
- Inverse FVG / breaker blocks as first-class types  
- Partial exits, BE move, trailing, DCA, pyramiding  
- 15m zones written to `structure_zones` table  
- Calibrated probability or LLM-set confidence  
- Merging strategy_ids with ignition / rsi-reclaim / accumulation  
- Machine learning  

These may be evaluated only after baseline hypothesis testing.

---

## Acceptance (implementation)

### Shared modules (required — build these)

- [ ] **`session_levels.py` shipped** with `pdh_pdl` (and tests in `test_session_levels.py`)
- [ ] PDH/PDL = prior UTC day only; developing day never used; lookahead tests pass
- [ ] **`market_structure.py` shipped** with confirmed 2/2 pivots + `latest_confirmed_pivot_*`
- [ ] Pivots confirm only after right-hand bars; tests prove no early confirm
- [ ] **`liquidity_sweep.py` shipped** with qualify / arm / advance / BOS / impulse / invalidation
- [ ] Sweep depth band, same-bar reclaim, extreme cancel, 8-bar expiry, close-only BOS tested
- [ ] Plugin imports M1–M3 for geometry (no duplicated PDH/pivot/sweep formulas in plugin)
- [ ] No durable sweep-state table; rebuild from bars each cutoff

### Platform wiring

- [ ] `liquidity-sweep-reversal-v1` in `KNOWN_STRATEGIES` + registered plugin
- [ ] Id in `config.PRICE_STRUCTURE_STRATEGY_IDS`
- [ ] `LSR_V1_*` in `config.py` + `.env.example`
- [ ] **Not** in default `STRATEGY_ENABLED_IDS` (opt-in)
- [ ] Discord + Telegram family label `"Liquidity reversal"` for `liquidity_reversal`
- [ ] `signal_publisher` family label stays consistent with `discord_format`

### Plugin

- [ ] Emits only as `strategy_id = liquidity-sweep-reversal-v1`
- [ ] `setup_class = liquidity_reversal`, `phase = armed_impulse_retracement`
- [ ] `run_plugin(cutoff_id, snapshot)` + `evaluate` / `evaluate_symbol` rsi-shaped
- [ ] Bars via `load_15m_bars` / `list_candidate_symbols`; cutoff via `completed_cycle`
- [ ] 4h bias agree-or-abstain; trade = reverse of context; both directions
- [ ] Freeze structure via `market_structure` at sweep; BOS via `liquidity_sweep`
- [ ] BOS emit only when BOS bar is last completed 15m
- [ ] Entry = 50% impulse midpoint with optional FVG/OB snap inside impulse
- [ ] `entry_condition.type = limit_at_impulse_mid`
- [ ] Invalidation = sweep extreme ± 0.15 SweepATR (ATR from sweep bar)
- [ ] `r_max` uses ATR at BOS bar; single **2.0R**; `horizon_minutes = 120`
- [ ] Soft keys match `weighted_confluence`; `confidence_status=uncalibrated`
- [ ] `S_min` + top-N + `has_active_event` + `emitted_today` day-cap
- [ ] Point-in-time replay from finalized cutoff snapshots only
- [ ] Unit tests for identity, hard fails, FVG refine, re-arm, day-cap, no lookahead
- [ ] No 15m structural zone materialization
- [ ] Exec adapter support **not** required for v1 acceptance

---

## Appendix A — Source model → analyst translation

| Source (Freqtrade-style) | Analyst v1 |
|--------------------------|------------|
| 1h EMA20/50 context | 4h EMA48 + zone agree-or-abstain; trade reverse of context |
| PDH/PDL | Same, UTC, from 15m aggregates |
| 15m 2/2 pivots + BOS close | Same |
| Sweep depth 0.1–1.0 ATR | Same hard gate |
| 50% limit entry | `limit_at_impulse_mid` + **FVG/OB refine** |
| 0.15 ATR stop buffer | `invalidation_price` |
| 2R TP | single target 2.0R |
| 0.50% risk / 1x leverage | **not in producer** |
| 120m entry timeout | `valid_until` = +2h |
| 1 long + 1 short BOS/day | UTC-day side cap + re-arm |
| Displacement / close-location | hard OFF; soft ON |
| FVG “non-goal” in source V1 | **First-class soft + entry magnet here** |
| Live backtest engine | Alpha plugin + optional offline research |

## Appendix B — Worked long sketch

```text
Context: 4h close < EMA48, nearest 4h zone bearish → context short → trade long OK
PDL = 100
15m sweeps to 99.4 (depth 0.6), ATR=1.0 → depth in band; close 100.2 > PDL
Freeze pivot high 101.5; SweepLow=99.4; SweepATR=1.0
Within 5 bars, close 101.7 > 101.5 → BOS
ImpulseHigh through BOS = 102.0
EntryMid = 99.4 + 0.5*(102.0-99.4) = 100.7
Bullish 1h FVG mid 100.65 within 0.25 ATR → entry snaps 100.65
invalidation = 99.4 - 0.15*1.0 = 99.25
risk = 1.40; target = 100.65 + 2.8 = 103.45
Emit armed limit; valid 2h; score boosted by fvg_entry_magnet + zone stack
```

## Appendix C — Diagram

```text
     PDH ─────────────────────────────────────
                      ╲ wick sweep (short setup)
                       ╲
     ─ reclaim close ───●──── BOS close below pivot low
                         ╲ impulse
                          ╲
                     ●──── limit @ 50% (± FVG mid)
                    inv
     PDL ─────────────────────────────────────

4h bias ──► context ──► reverse trade direction
1h/4h FVG/OB ──► zone_bias + soft stack + entry magnet
15m ──► PDH/PDL, pivot, sweep, BOS, impulse limit
```

## Appendix D — Pre-implement audit (2026-08-18)

| Finding | Spec response |
|---------|----------------|
| No M1–M3 or plugin in tree | Must-build; acceptance blocked until shipped |
| Plugin shape must match `rsi_reclaim_v1` | § Implementation contract |
| `PRICE_STRUCTURE_STRATEGY_IDS` omit blocks writes | Wiring checklist + acceptance |
| `has_active_event` alone ≠ day-cap | `emitted_today` algorithm |
| Soft weight key mismatch vs `weighted_confluence` | Component keys aligned; `contradiction_penalty` |
| `select_top_n` / `build_1_5r_target` not shipped | Documented backlog; plugin inlines |
| Family label falls through to "Impulse ignition" | Discord + signal_publisher touch required |
| Outcome evaluator already handles non-breakout limits | No outcome code change |
| Exec adapter only `limit_at_ema_context` | Deferred; not v1 acceptance |
| OB 20-bar swing ≠ BOS 2/2 | Explicit non-reuse |
| Defaults differ (2R / 2h / r_max 3.0) | Called out vs other families |

**Ready to implement** when this doc + `strategy-v2-shared-library.md` are accepted.
