# Polars Computation Migration v1

## 1. Purpose

Move numerical market computation from Python row/list loops into Polars
expressions and Polars series operations, while keeping Python responsible for
orchestration, policy, state machines, audit provenance, persistence, and
delivery.

The migration must improve throughput and reduce Python object churn without
changing strategy outputs, cutoff semantics, warmup behavior, admission
decisions, or persisted audit contracts.

## 2. Target Architecture

```text
SQLite source observations and regime bars
  -> typed Polars DataFrames
  -> Polars numerical kernels
       resampling, indicators, rolling features, zone candidates
  -> Python policy layer
       cutoff/source policy, state machines, strategy conditions,
       admission, clash resolution, provenance, persistence
  -> SQLite ledgers and optional intent bus
```

Polars is used through its Python API, but the heavy columnar work executes in
Polars' native engine. Python remains at the boundary and consumes scalar or
row-level results.

## 3. Scope

### In scope

- Canonical OHLCV resampling and completeness checks.
- Shared EMA, Wilder RSI, Wilder ATR, StochRSI, Bollinger, VWMA, rolling
  extrema, returns, and volatility features.
- Shared and strategy-local ADX/DMI implementations after exact parity is
  established.
- Vectorizable portions of FVG and order-block detection.
- Vectorizable regime-score inputs and reversal-gate inputs.
- Strategy feature columns for current and tested legacy plugins.
- PM-sidecar numerical context, using the same shared kernels where contracts
  permit.
- Unit, parity, integration, replay, lookahead, and end-to-end validation.

### Remains Python

- SQLite ownership, connection lifecycle, and trigger claiming.
- Source precedence, duplicate resolution, cutoff and handoff policy.
- Missing-data and fail-closed decisions.
- Strategy plugin iteration and family routing.
- Stateful zone lifecycle transitions.
- Sweep/BOS, failed-break, EMA-retest, double-touch, and other sequential
  state machines.
- Candidate construction, admission policy, structural stop selection, clash
  resolution, scoring, fingerprints, and admission proofs.
- Provenance assembly, audit writes, publisher retries, and intent delivery.

## 4. Non-Negotiable Contracts

### Numerical parity

- Existing in-house indicators remain the reference implementation until a
  Polars implementation passes fixture parity.
- EMA uses the existing SMA seed at index `span - 1`.
- Wilder RSI uses the existing arithmetic seed and zero-loss behavior.
- Wilder ATR uses the existing true-range definition and arithmetic seed.
- StochRSI preserves null warmup, zero-denominator output, and K/D alignment.
- Bollinger calculations preserve population standard deviation (`ddof=0`)
  where the current strategy uses it.
- ADX/DMI implementations must not be silently unified when their warmup or
  smoothing contracts differ. Each strategy contract must be measured first.

### Time and data safety

- All calculations use completed bars only.
- End-stamped candles remain end-stamped.
- Exact boundary and boundary-minus-one-millisecond representations normalize
  to one logical timestamp.
- Missing bars, conflicting duplicates, malformed candles, and cutoff mismatch
  fail closed.
- Centered pivots and divergence cannot expose future information before the
  original confirmation point.
- Hybrid direct-seed and canonical-tail handoff remains unchanged.

### Persistence

- Polars frames are transient computation inputs/outputs, not a replacement for
  owned SQLite stores.
- Persisted output is limited to required source observations, raw candidates,
  admission proofs, alpha events, lightweight feature summaries, and audit
  records.
- Per-zone analyst rows remain disabled; zones are recomputed in memory.

## 5. Current Computation Inventory

### Data plane

| Area | Current location | Migration target |
| --- | --- | --- |
| SQLite rows to frames | `strategy_v2_context.py` | typed Polars boundary |
| 15m/1h/4h resampling | `strategy_v2_context.py` | Polars dynamic grouping plus Python completeness policy |
| WS resample persistence | `ws_gateway.py` | consume Polars output; Python writes rows |
| hybrid HTF merge | `strategy_v2_context.py` | Polars joins/filters plus Python handoff policy |

### Shared indicators

| Indicator | Current location | Target |
| --- | --- | --- |
| EMA | `strategy_v2_context.py` | Polars seeded EWMA |
| Wilder RSI | `strategy_v2_context.py` | Polars gain/loss columns and seeded EWMA |
| Wilder ATR | `strategy_v2_context.py` | Polars true-range columns and seeded RMA |
| StochRSI | `strategy_v2_context.py` | Polars rolling min/max/mean |
| ADX/DMI | regime and strategy modules | one parity-tested Polars kernel per contract |
| Bollinger | compact and v2 strategies | Polars rolling mean/std with explicit `ddof` |
| VWMA/volatility | strategy and regime modules | Polars rolling/sum expressions |

### Structural and regime computation

| Area | Polars work | Python work |
| --- | --- | --- |
| FVG detection | shifted OHLC comparisons | evidence records and lifecycle |
| Order-block detection | rolling prior highs/lows and displacement columns | emitted zone records and lifecycle |
| Pivot detection | centered rolling extrema candidates | confirmation/as-of policy |
| Sweep/BOS | ATR and candle feature columns | mutable state machine and event order |
| Regime score inputs | ADX, log returns, volatility columns | score weights, hysteresis, gate decisions |
| Structural admission | ATR and bar validity columns | zone selection, buffers, fail-closed proof |

## 6. Migration Phases

### Phase 0: Baseline and instrumentation

- Capture deterministic fixtures for all shared indicators and resampling.
- Record current per-cutoff runtime, frame sizes, Python conversion counts, and
  memory for representative BTC, ETH, and broad-universe evaluations.
- Add parity helpers that compare null positions, finite values, and tolerances.
- Do not change strategy versions or production routing.

### Phase 1: Shared indicator kernels

- Add a dedicated Polars kernel module with typed DataFrame/Series boundaries.
- Migrate EMA, Wilder RSI, Wilder ATR, and StochRSI first.
- Preserve current public helper signatures through thin adapters where needed.
- Add exact fixture tests and randomized property tests against the reference
  implementations.
- Migrate Bollinger, VWMA, rolling extrema, and realized-volatility features.

### Phase 2: Resampling and frame assembly

- Replace Python dictionary grouping in `resample_ohlcv` with Polars dynamic
  grouping or equivalent typed expressions.
- Keep explicit Python validation for duplicate aliases, completeness, and
  cutoff boundaries.
- Add parity tests for partial buckets, missing bars, millisecond aliases,
  source provenance, open/close ordering, and mixed purity.

### Phase 3: Zone candidates and market structure

- Compute FVG and order-block candidate columns in Polars.
- Keep lifecycle transitions and evidence construction in Python initially.
- Migrate pivots, PDH/PDL, candle geometry, and rolling structure features.
- Confirm no future rows are used before moving state-machine inputs.

### Phase 4: ADX/DMI and regime inputs

- Implement each ADX/DMI contract separately in Polars.
- Compare against strategy-local and regime-local references before sharing any
  implementation.
- Migrate realized volatility, reversal-gate RSI/ADX windows, and OLS inputs.
- Preserve insufficient-data and fail-closed behavior exactly.

### Phase 5: Strategy feature frames

- Give each plugin a typed feature-frame builder.
- Compute shared indicators once per asset/timeframe/cutoff.
- Pass feature frames into Python strategy policy functions.
- Remove repeated prefix recomputation, especially repeated ADX calculations in
  EMA-retest and double-touch strategies.
- Keep strategy IDs and versions unchanged until parity and replay validation
  pass.

### Phase 6: Admission and PM context

- Move only numerical structural ATR/bar-validity columns into Polars.
- Keep admission proofs, selected-zone policy, and fingerprints in Python.
- Make PM-sidecar TA consume the shared feature kernel rather than its own RSI
  and StochRSI implementation.

### Phase 7: Rollout and removal

- Run shadow parity for a complete replay window.
- Compare candidate IDs, directions, prices, stops, targets, admission reasons,
  regime gates, and published alpha IDs.
- Enable the Polars path per computation family behind a configuration switch.
- Remove reference implementations only after one validated release cycle.
- Keep rollback switches until post-rollout replay and operational metrics pass.

## 7. First Implementation Slice

The first slice migrates the shared indicator calculations without changing
their public APIs:

- `ema_series` uses a seeded Polars `ewm_mean`.
- `wilder_rsi` computes gains/losses and seeded RMA in Polars.
- `wilder_atr` computes true range and seeded RMA in Polars.
- `stoch_rsi` uses Polars rolling min/max/mean with explicit null and zero
  denominator behavior.

The adapters may return Python lists because existing strategy interfaces expect
lists. The numerical recurrence and rolling calculations execute in Polars.
The next slice will migrate the resampler after this kernel boundary is proven.

## 8. Verification Gates

### Unit

- Shared indicator fixture parity.
- Randomized finite-series parity.
- Null/warmup and zero-denominator behavior.
- Existing structure, regime, and strategy unit suites.

### Integration

- Gateway resample persistence.
- Hybrid HTF seed/tail merge.
- Regime score persistence and gate scope.
- Plugin registry and candidate ledger.
- Structural admission proof verification.

### End to end

- Structural-stop E2E.
- Strategy-specific E2E suites.
- Regime-session E2E.
- Symbol-scope E2E.
- Full `python3 -m pytest -q`.
- `python3 -m compileall -q src tests`.

Acceptance requires zero unexplained candidate or admission differences on the
same cutoff fixtures. Performance improvements are measured separately from
correctness and cannot justify a parity exception.

## 9. Rollback

- Each migrated family has a reference adapter and a Polars implementation
  until rollout validation completes.
- A failing parity gate disables only the migrated family.
- No database schema rollback is required for transient feature computation.
- Persisted event and admission contracts remain versioned and unchanged.

## 10. Risks

- Recursive indicators may look vectorized but differ at their seed boundary.
- `group_by_dynamic` can silently change end-boundary semantics.
- Converting list-valued provenance columns can create expensive object data.
- Fully vectorizing state machines can obscure event ordering and lookahead
  rules.
- Sharing an indicator implementation across strategies can accidentally change
  strategy behavior and therefore requires an explicit version decision.
