# Staged Native Exits v1

**Status:** RA normative (implemented in research-analyst). Intent-bus §3
implemented in the shared bus package (`staged_native_targets` forwarding in
`producer_adapters.build_research_analyst_delivery`, price-list
`hints.targets` plus full-fidelity `hints.native_targets` and
`take_profit_mode` passthrough on the Propr path; Bybit path already forwards
the envelope verbatim). Downstream position-management and execution semantics
are outside Research Analyst; the single venue TP remains the executor
backstop. Per-level completion and idempotency are downstream concerns.

## 1. Background

Research Analyst previously collapsed multi-level strategy targets to one
take-profit. This contract preserves the full native target array through
admission and the shared-bus TradeIntent. The 2R producer fallback remains for
strategies without a native target. Execution and staged-exit behavior are
downstream and outside this repository.

## 2. RA contract (normative, implemented)

### 2.1 Native targets array

A candidate may carry `targets` as a list of prices (strategy-native, ordered
near-to-far from entry) or a list of `{price, fraction}` objects. Admission
normalizes to `[{price, fraction}]`:

- `price`: finite, positive, strictly monotonic away from entry in the trade
  direction. Any violation fails the candidate closed.
- `fraction`: share of current position the PM reduces at that level,
  `0 < fraction <= 1`. Strategies may specify it; unspecified intermediate
  levels default to `NATIVE_TARGET_DEFAULT_FRACTION` (0.5 of current size at
  the time the level triggers, so cascading levels can never exceed the
  remaining size). The final (venue) level carries `fraction: null` (remainder).
- Fractions are PM exit-management directives, not entry sizing. Entry sizing
  stays executor-owned.

### 2.2 Venue TP selection

`selected_take_profit` (the venue TP, executor-placed, PM backstop) is the
furthest admitted native level when native targets exist, else the 2R producer
fallback. Rationale: a venue order inside PM-managed levels would fill
instantly and cut the runner before the PM poll can act. The proof records
`selected_take_profit_source` (`native_furthest` or `producer_derived_2r`).

### 2.3 Reward/risk gate and native exemption

The `INTENT_MIN_RR` (2.0) gate applies to TP1. Strategies in
`NATIVE_TP_MIN_RR_EXEMPT_IDS` (default: `kama-trend-following-v1`, whose edge is
a 1R symmetric ATR bracket) may deliver a sub-minimum native TP1: the gate is
skipped and the proof records `min_rr_exempt_native: true` with the TP1 RR.
RA validates geometry, not the strategy's edge. Levels past TP1 are
geometry-checked only (a farther level trivially clears a nearer level's RR).

### 2.4 Executor envelope

`build_trade_intent` emits:

- `take_profit`: venue TP (2.2).
- `targets`: full admitted `[{price, fraction}]` array, venue level last.
- `take_profit_mode`: per-strategy from `STRATEGY_TAKE_PROFIT_MODES`
  (`bb-tp-race-locked-v1` → `bracket_tp1_tp2_race`,
  `mr-vwap-locked-v1` → `vwap_target`,
  `kama-trend-following-v1` → `symmetric_atr_bracket`,
  default `fixed_full_close`), via route override, else global default.
- `metadata.target_source`: `strategy_target` or `producer_derived_2r` (unchanged).

The admission fingerprint binds the full `targets` array in addition to
`take_profit`. Venue-TP-furthest changes admitted economics for multi-target
strategies (notably `bb-tp-race-locked-v1` tp1 → tp2); replay baselines for
those strategies restart at this contract version.

## 3. Intent-bus contract (implemented 2026-09-15)

- Bybit deliveries already forward the RA envelope verbatim, so `targets`,
  `take_profit_mode`, and strategy metadata flow without adapter changes.
  `validate_bybit_geometry` checks scalars only; `assert_no_sizing_hints`
  forbids only entry-sizing keys (`fraction` is an exit directive, not entry
  sizing).
- The Propr RA adapter (`build_research_analyst_delivery`, target=propr)
  normalizes the admitted envelope `targets` via `staged_native_targets`:
  `hints.targets` is the price list (existing `float(targets[0])` consumers
  keep working), `hints.native_targets` carries the full
  `[{price, fraction}]` fidelity, and `take_profit_mode` passes through
  top-level. Additive optional keys: no bus contract version bump; scanners
  (unscored, no staged exits) are unaffected.

## 4. Standalone PM contract (implemented 2026-09-15)

- Intent gains `native_targets[]` (`pm/contracts.normalize_native_targets`):
  bounded (8), lenient (malformed entries dropped, `sum(fractions) <= 1`
  enforced, otherwise degrades to `[]` = single venue-TP behavior). The key is
  omitted from `to_dict()` when empty so stored single-TP rows round-trip
  unchanged (no fingerprint or idempotency drift).
- Ingestion (`pm/feeds._intent_from_delivery`) reads top-level `targets`
  (Bybit verbatim path), `hints.native_targets` (Propr path), or legacy
  `hints.targets` prices; downstream consumers receive the array through
  `intent.to_dict()` as advisory evidence.
- NEAR_TP decisions carry `target_levels` (Decision field, engine-attached
  from the matched intent) so venues can fire per-level reductions.
- The reference consumer (`pm/consumers._target_plan`) selects the nearest
  uncompleted in-band level with that level's fraction; sole null-fraction
  levels and level-less decisions keep legacy single-TP flow.

## 5. Executor contract (implemented 2026-09-15 in both venues)

Both venues implement per-level NEAR_TP firing with venue-owned selection:

- The decision-carried `target_levels` array is validated leniently; absent
  or invalid arrays keep exact legacy single-shot behavior.
- The venue fires only the nearest uncompleted in-band level per cycle with
  that level's fraction of the current remainder (intermediate levels only;
  the null-fraction final level belongs to the native TP backstop, except a
  sole null level which keeps legacy behavior with the configured fraction).
- Completion is recorded per level with a stable per-level key
  (Bybit: `near_tp_levels` table scoped by position/instance/revision;
  Propr: `PM_NEAR_TP_REDUCED_L{index}` journal events), tied to the position
  instance so re-entries reset. Legacy one-time rows are still written, so
  level-less decisions stay fail-closed.
- Observations expose `near_tp_completed_levels` (`[{index, price}]`) alongside
  the legacy `near_tp_reduction_completed` flag; the PM consumes both.
- No multi-order native bracket execution: intermediate levels remain
  PM-decision-driven, exactly as originally scoped. IOC today covers limit
  entries only; market entries are immediate.

## 6. Rollout record (research-analyst)

Deployed 2026-09-15 (orchestrator + strategy-runner restart): admission keeps
the full native array, venue TP is furthest-native, TP1 min-RR exemption is
recorded, envelopes carry `targets[]` with per-strategy `take_profit_mode`.
Replay baselines for multi-target strategies (`bb-tp-race-locked-v1`)
restart at this contract version; single-target fingerprints are unchanged.
The shared bus forwards the envelope verbatim. This repository makes no claim
about how downstream consumers interpret or execute individual target levels.
