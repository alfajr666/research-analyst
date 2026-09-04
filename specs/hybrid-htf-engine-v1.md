# Hybrid Higher-Timeframe Engine v1

## Status

Implementation specification. This changes the strategy `1h`/`4h` data path to
an engine-owned hybrid series. Strategy plugins remain symbol-dumb and blind to
the source transition.

## Decision

For strategy evaluation, the engine may use completed direct Bybit REST candles
from the regime-owned history cache as historical seed data. The engine then
hands off to canonical market data, derived from committed `5m` observations,
for the newest tail.

```text
regime.sqlite3 direct 1h/4h seed
              |
              +---- exact HTF handoff ----+
                                           |
market.sqlite3 canonical 5m -> 1h/4h -----+--> engine frame --> unchanged plugin
```

The regime worker remains the sole writer of `regime.sqlite3`. The evaluator
opens it read-only. No strategy receives a regime connection, handoff value, or
source-selection flag.

## Scope

- Hybrid loading applies only to `1h` and `4h` strategy frames.
- `1m`, `5m`, and `15m` remain canonical market paths.
- The existing shared `resample_ohlcv` implementation is used for the live
  tail.
- The same engine context is used for HTF feature materialization and plugin
  invocation.
- Outside an invocation-scoped engine context, the existing market-only loader
  behavior is unchanged.

## Handoff Contract

The engine finds the latest contiguous canonical `5m` tail ending at the
requested cutoff. For each target timeframe, it floors the earliest tail bar to
that timeframe boundary.

```text
direct seed:       source_end <= handoff_at
canonical HTF:     source_end > handoff_at and source_end <= cutoff_at
```

The direct and canonical segments must have unique, strictly increasing bar end
timestamps. A forming bar, future bar, duplicate, malformed candle, or missing
canonical `5m` bar makes the affected HTF frame unavailable. The engine does not
interpolate, compress, or silently fill a gap.

If no direct seed is available, `HYBRID_HTF_MODE=shadow` may expose an explicit
`canonical_only` frame. This is observable and is not treated as hybrid
readiness. In `enforce` mode, the frame is unavailable. If the current
canonical tail is unavailable, direct history must not be used as a substitute
for current market data.

## History and Readiness

The strategy seed target is 240 completed bars for both `1h` and `4h`, covering
the current longest EMA warmups with margin. The regime score's 57-bar minimum
remains a separate readiness rule.

```text
regime_history_ready
strategy_htf_seed_ready
canonical_tail_ready
hybrid_htf_ready
```

Direct cache retention and pagination must satisfy the strategy seed target.
The initial retention target is at least 14 calendar days for `1h` and 45 days
for `4h`, with one extra fetch day of margin. If the configured seed depth is
increased, retention and fetch depth must increase automatically.

## Engine Wiring

The engine installs an invocation-scoped hybrid context around each evaluation
stage:

1. Orchestrator HTF feature and zone materialization.
2. Strategy plugin execution for the same finalized cutoff.

Stages may use separate read-only contexts, including when operational pipeline
boundaries require it, but both contexts must use the same contract and exact
cutoff-bounded source rules. Direct history is immutable and the market cutoff
is finalized before either stage reads it, so separate contexts cannot observe
different accepted data for the same cutoff.

Existing plugins continue calling `load_bars_for_interval`; no plugin changes are
required. The context caches frames by asset, timeframe, and cutoff.

## Provenance

Emitted candidates receive engine metadata containing:

- `data_contract_version`;
- cutoff;
- per-timeframe handoff timestamp;
- direct bar IDs and bar version;
- canonical 5m observation IDs;
- availability and source mode.

Indicator calculations run over the merged chronological frame without resetting
at the handoff. Delayed evaluation uses the original cutoff and source IDs.

## Validation

The implementation must test exact-boundary stitching, cutoff exclusion, gap and
duplicate handling, missing regime history, canonical-only diagnostics,
indicator continuity, delayed replay, plugin failure isolation, and unchanged
regime/gateway behavior. Direct-versus-resampled OHLC and indicator parity must
be measured before enforcement. Runtime `enforce` mode is rejected unless
`HYBRID_HTF_PARITY_VALIDATED=true` explicitly records that validation has been
completed.
