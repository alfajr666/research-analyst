# Staged Native Exits v1

**Status:** RA normative (implemented in research-analyst). Intent-bus, standalone PM,
and executor sections are normative-deferred and owned by separate changes.

## 1. Background

Research Analyst intents carry a single take-profit: the first native strategy
target when present, else the 2R producer fallback (`derive_2r_target`). Multi-level
native exits (`bb-tp-race-locked-v1` tp1/tp2 ATR bracket) lose every level past the
first at admission, and the standalone PM can only manage a single
`original_target` (one NEAR_TP scale-out). The venue TP, the RA default TP, and
the strategy-native scale-out levels must be distinct, auditable fields so each
owner (RA, executor, PM) acts on the right one:

- RA produces intents: entry, SL, venue TP (2R default), native `targets[]`.
- Executor executes: entry, SL, one venue TP. No exit discretion.
- PM manages: staged scale-outs at native levels, time exits, adverse-TA exits.
  PM decisions stay advisory; the executor validates. Venue SL/TP remain the
  hard safety net and are never transferred to PM.

The 2R default is retained. The standalone PM moves profit-taking higher via
native levels; the RA default is not raised.

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

`build_executor_intent` emits:

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

## 3. Intent-bus contract (normative-deferred, separate change)

- Bybit deliveries already forward the RA envelope verbatim, so `targets`,
  `take_profit_mode`, and strategy metadata flow without adapter changes.
  `validate_bybit_geometry` checks scalars only; `assert_no_sizing_hints`
  forbids only entry-sizing keys (`fraction` is an exit directive, not entry
  sizing).
- Promote bus `hints.targets` from dead payload to the real array for
  consumers that read hints (separate bus-repo change with contract version).

## 4. Standalone PM contract (normative-deferred, separate change)

- Intent gains `targets[]`; per-level completion flags generalize
  `near_tp_reduction_completed`.
- The NEAR_TP policy fires only the nearest uncompleted in-band level per
  cycle with that level's fraction; stable idempotency key per level.
- Enforce sum(fractions) <= 1 at contract normalization; tie completion flags
  to `lifecycle_revision` so re-entries reset.
- Feed the array plus completed levels into the LLM context so discretionary
  REDUCE/EXIT sizes against remaining size.
- Reconciliation unchanged: a venue TP fill reads as flat from venue state;
  PM never infers positions.

## 5. Executor contract (normative-deferred, separate change if any)

No change required under venue-TP-furthest: the executor keeps placing one
venue TP (`take_profit`) plus SL and validating PM decisions. IOC today covers
limit entries only; market entries are immediate. Any multi-order native
bracket execution is explicitly out of scope: intermediate levels are managed
by PM decisions, not venue orders.

## 6. Rollout record (research-analyst)

Deployed 2026-09-15 (orchestrator + strategy-runner restart): admission keeps
the full native array, venue TP is furthest-native, TP1 min-RR exemption is
recorded, envelopes carry `targets[]` with per-strategy `take_profit_mode`.
Replay baselines for multi-target strategies (`bb-tp-race-locked-v1`)
restart at this contract version; single-target fingerprints are unchanged.
Downstream bus/PM/executor changes are pending; until then the bus forwards
the envelope verbatim and the venue TP behaves as the single backstop.
