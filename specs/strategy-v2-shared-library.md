# Sketch: Strategy v2 shared library

## Status

Design sketch aligned to **shipped modules** (not greenfield). Use this when
extending plugins or extracting more shared code. Does not change strategy
contracts in the per-family specs.

## Goals

1. One place for bias, zones, compression, ATR geometry, emit floor, re-arm.
2. Per-family plugins only own **thesis gates + trigger geometry + weight maps**.
3. Keep confidence uncalibrated; LLM booster outside this library.

## Non-goals

- Merging the three `strategy_id`s
- Calibrated probability
- Exec adapter / sizing rules

## Module map (current → target)

```text
                     finalized cutoff snapshot
                               │
               ┌───────────────┼───────────────┐
               v               v               v
      strategy_v2_context   structure_zones   (optional VP snapshot)
          bars/resample         FVG/OB
          bias/compression
          re-arm / zones
               │
               v
         confluence_scoring
               │
   ┌─────┬─────┼─────┬──────────┐
   v     v     v     v          v
 accum  ign  cont  rsi-reclaim  LSR (+ session_levels,
                                    market_structure,
                                    liquidity_sweep)
   └─────┴─────┴─────┴──────────┘
               v
          alpha_outbox
```

| Module | Role | Owns today |
|--------|------|------------|
| `strategy_v2_context.py` | PIT bars, HTF resample, EMA/ATR, bias, compression, expansion fail, zone scores, active-event re-arm | Shared **context + hard-gate primitives** |
| `confluence_scoring.py` | ATR proximity bins, weighted sum, confidence map | Shared **soft score math** |
| `structure_zones.py` | FVG/OB detect + attach evidence | Platform zones |
| `accumulation_base_v2.py` | EMA limit thesis | Family plugin |
| `impulse_ignition_v2.py` | Base lid breakout thesis | Family plugin |
| `continuation_breakout_v2.py` | Trend+flag breakout thesis | Family plugin |
| `rsi_reclaim_v1.py` | RSI pullback + EMA reclaim thesis | Family plugin |
| `session_levels.py` (**must build**) | UTC PDH/PDL from completed 15m | Shared structure primitive |
| `market_structure.py` (**must build**) | Confirmed 2-left/2-right pivots | Shared structure primitive |
| `liquidity_sweep.py` (**must build**) | Sweep+reclaim, freeze, BOS, impulse, invalidation | Shared structure primitive |
| `liquidity_sweep_reversal_v1.py` (**must build**, after helpers) | Counter-context sweep → BOS → impulse midpoint limit | Family plugin — `specs/strategy-liquidity-sweep-reversal-v1.md` |
| `strategy_plugins.py` | Registry + cutoff invoke | Wiring only |

## Shared API surface (canonical)

### Context / bars

```text
completed_cycle(now) -> cutoff
load_15m_bars(conn, symbol, cutoff) -> df
load_btc_15m(conn, cutoff) -> df
list_candidate_symbols(conn, cutoff) -> [(symbol, asset)]
resample_ohlcv(bars_15m, "1h"|"4h") -> df
ema_last(closes, span) -> float | None
atr_last(bars, period=14) -> float | None
last_completed_bar_fresh(bars_15m, cutoff) -> bool
```

### Bias (agree-or-abstain)

```text
structure_bias_4h(bars_4h) -> long|short|missing     # EMA48_4h
zone_bias_4h(zones, ref_close, atr_4h) -> (bias, zone)  # nearest active|partial
resolve_bias(structure, zone) -> direction | None      # None = hard fail
```

### Compression / expansion

```text
compression_ok(bars_1h, n, k, atr_1h) -> (ok, high, low, range)
prior_base_expansion_fail(bars_1h, n, g, atr_1h, direction) -> bool
prior_range_ratio(bars_1h, n, p) -> float | None       # ignition (+ cont flag quality)
```

### Zones / soft geometry

```text
compute_htf_zones(bars_1h, bars_4h) -> zones
zone_stack_and_ltf_scores(zones, ref, atr, direction) -> (ltf_inside_htf, zone_stack)
snapshot_zones_for_asset(snapshot, asset) -> zones
```

### Score

```text
proximity_score(distance_atr, same=0.25, near=0.75) -> [0,1]
weighted_confluence(components, weights) -> (score, weighted_parts)
confidence_from_confluence(score) -> (confidence, "uncalibrated")
```

### Emit ops (shipped vs backlog)

```text
# SHIPPED
has_active_event(strategy_id, asset, direction, ...) -> bool

# BACKLOG only (each plugin still inlines today — do not assume these exist)
select_top_n(candidates, s_min, n_top) -> list
build_1_5r_target(entry, inv, direction) -> price
# LSR uses 2.0R inline: entry ± target_r * |entry - inv|
```

### Session levels / market structure / liquidity sweep (**must build** for LSR)

Normative contracts: `specs/strategy-liquidity-sweep-reversal-v1.md`
§ Shared modules + § Implementation contract.

Ship M1–M3 **before** enabling `liquidity-sweep-reversal-v1`. Plugin must import
these; no inline PDH/pivot/sweep duplicates. M3 is pure geometry (takes pdh/pdl
and structure level as args); plugin wires M1+M2→M3.

```text
# session_levels.py
pdh_pdl(bars_15m, asof_ts) -> {pdh, pdl, prior_utc_day, bar_count} | None

# market_structure.py
confirmed_pivot_highs(bars_15m, left=2, right=2) -> list[pivot]
confirmed_pivot_lows(bars_15m, left=2, right=2) -> list[pivot]
latest_confirmed_pivot_high(bars_15m, asof_index, left=2, right=2) -> pivot | None
latest_confirmed_pivot_low(bars_15m, asof_index, left=2, right=2) -> pivot | None

# liquidity_sweep.py
qualify_bullish_sweep / qualify_bearish_sweep
arm_long_sweep / arm_short_sweep
advance_sweep_state(state, bars, through_index, bos_window=8)
bos_long / bos_short
impulse_long / impulse_short
entry_mid / invalidation_long / invalidation_short
```

Also planned plugin-local (may later lift): `emitted_today(...)` UTC-day side cap
for LSR (broader than `has_active_event`).

Plugins should call shared helpers rather than reimplementing ATR bins, bias
tables, or re-arm file scans.

## Per-plugin responsibility (only)

| Concern | accum | ignition | continuation | rsi-reclaim | liq-sweep-rev |
|---------|-------|----------|--------------|-------------|---------------|
| Extra hard gates | `d_max` to EMA99_1h | edge `e`, no breach, `c_ratio` | 4h `t_min`, flag `retr_max`, `x_max` | 1h sep band, RSI turn, touch/reclaim, body floor | reverse-of-4h-bias, PDH/PDL sweep, pivot BOS, impulse mid |
| Entry type | `limit_at_ema_context` | `breakout_above/below` @ base | `breakout_*` @ flag | `breakout_*` @ reclaim close | `limit_at_impulse_mid` (+ FVG snap) |
| Invalidation | worse-of EMA band vs base | opposite base | opposite flag | worse-of bar extreme vs mid EMA | sweep extreme ± 0.15 SweepATR |
| Weight map | ema/volume-centric | OI/funding/RS/impulse | trend/retrace/acceptance | RSI/reclaim/extension-centric | FVG magnet + sweep/displacement quality |
| Shared deps | context/zones/score | same | same | same | **+ session_levels + market_structure + liquidity_sweep** |
| Config prefix | `ACC_V2_*` | `IGN_V2_*` | `CONT_V2_*` | `RSI_RECLAIM_*` | **`LSR_V1_*`** |
| Purity class | PRICE_STRUCTURE | MIXED | MIXED | PRICE_STRUCTURE | **PRICE_STRUCTURE** |
| Default enabled | yes | yes | yes | opt-in | **opt-in** |
| Target R / horizon | 1.5 / 4h | 1.5 / 4h | 1.5 / 4h | 1.5 / 4h | **2.0 / 2h** |

## Extraction backlog (optional deepenings)

1. **`select_top_n` + batch evaluate loop** — identical in three plugins; lift to
   `strategy_v2_context.evaluate_batch(...)`.
2. **`risk_ok(entry, inv, atr, r_max)`** — one helper.
3. **`near_edge(ref, extreme, atr, e, side)`** and **`inside_range`** — shared by
   ignition + continuation.
4. **VP proximity helper** — single function reading feature_snapshot OM VP vs
   approx label guard.
5. **Default weight tables** — data in config or `strategy_v2_weights.py`, not
   copied dicts in each plugin.
6. Keep **family files thin**: gates → components dict → `weighted_confluence` →
   event dict.

Do not extract thesis-specific retrace/extension math into a god-module.

## Config layout

Independent env prefixes (grilled): `ACC_V2_*`, `IGN_V2_*`, `CONT_V2_*`,
`RSI_RECLAIM_*`, **`LSR_V1_*`**. Plus global `LLM_BOOST_CAP` (delivery-order only).

LSR must also be listed in `PRICE_STRUCTURE_STRATEGY_IDS` (with accumulation +
rsi-reclaim) so `alpha_outbox.write_event` allows the OHLCV-only path.

### Locked defaults (2026-08-18 grill + LSR lock)

| Knob | Acc | Ign | Cont | RSI | **LSR** |
|------|-----|-----|------|-----|---------|
| `N` | 12 | 12 | 12 | — | — |
| `k` | 2.0 | 2.0 | 2.0 | — | — |
| `g` | 0.25 | 0.25 | 0.25 | — | — |
| `d_max` | 0.50 | — | — | — | — |
| `e` | — | 0.35 | 0.35 | — | — |
| `r_max` | 2.5 | 2.5 | 2.5 | 2.5 | **3.0** |
| `S_min` | 0.55 | 0.55 | 0.55 | 0.55 | **0.55** |
| `N_top` | 3 | 3 | 3 | 3 | **3** |
| target R | 1.5 | 1.5 | 1.5 | 1.5 | **2.0** |
| horizon_min | 240 | 240 | 240 | 240 | **120** |
| `P` / `c_ratio` | — | 20 / 0.85 | — | — | — |
| `P` / `t_min` | — | — | 12 / 1.0 | — | — |
| `retr_max` | — | — | 0.40 | — | — |
| `x_max` / `x_bars` | — | — | 3.0 / 96 | — | — |
| sweep band ATR | — | — | — | — | **0.10–1.00** |
| bos_window | — | — | — | — | **8** |
| stop buf ATR | — | — | — | — | **0.15** |
| `WEIGHT_PROFILE` | — | — | `balanced` (`early`\|`balanced`\|`confirmed`) |
| `LLM_BOOST_CAP` | 0.10 (global) | | |

Also locked (not env): EMA inv band 1.5%, target 1.5R, horizon 4h, zone bins 0.25/0.75 ATR.

## Stress-pass notes (specs ↔ code)

Already implemented and registered: all three v2 plugins + shared context/score
lib + config env knobs + tests. When stress-passing:

- Prefer fixing **library helpers** over copy-paste in plugins.
- Spec acceptance checklists remain the source of truth for behavior.
- Any new shared function must stay PIT (cutoff-bounded bars only).

## Related

- `specs/adr-strategy-confluence-scoring.md`
- `specs/strategy-accumulation-base-v2.md`
- `specs/strategy-impulse-ignition-v2.md`
- `specs/strategy-continuation-breakout-v2.md`
- `specs/data-platform-strategy-plugins.md`
