# Strategy Fidelity Repair and Expansion v1

## Status

Implementation specification. No strategy or runtime code is changed by this
document.

This specification is the normative implementation brief for:

1. Repairing the eight currently enabled strategy IDs.
2. Adding three new strategy families from the supplied Pine descriptions.
3. Repairing the shared lower-timeframe resampling and cutoff boundaries used by
   every strategy.

It supersedes conflicting strategy cadence and bar-provenance statements in
older documents, while preserving the locked admission, intent, executor, and
PM contracts.

## Scope

### Existing strategies to repair

| Strategy ID | Intended signal model |
|---|---|
| `failed-break-v3` | 5m StochRSI trigger after confirmed 4h swing failure/reclaim |
| `bb-rsi-meanrev-v1` | 5m Bollinger/RSI extreme or divergence mean reversion |
| `williams-fractal-scalp-v1` | 1m confirmed fractal pullback with EMA trend stack |
| `ema9-continuation-stochrsi-v1` | 5m EMA9 setup with 1m StochRSI trigger |
| `dual-zone-follower-v2` | 5m EMA7/26/99 trend pullback with 1h ADX/DI |
| `dual-zone-short-follower-v2` | Short mirror of `dual-zone-follower-v2` |
| `ema20-pullback-h4-trend-v1` | Completed 1h EMA20 pullback under completed 4h trend |
| `ema-stack-15m-adx-stochrsi-5m-v1` | 5m StochRSI trigger under 15m EMA stack and 1h ADX |

### New strategies to add

The following provisional IDs are used by this specification:

| Provisional ID | Supplied name | Signal model |
|---|---|---|
| `gold-trend-ema-bb-stoch-v1` | `GoldTrendEMA_BB_Stoch` | 5m trend-aligned Bollinger pullback |
| `mtf-exhaustion-reversal-v1` | `MTFExhaustionReversal` | 5m reversal under 1h/4h exhaustion context |
| `trend-wall-v1` | `TrendWallStrategy` | 15m pullback/reclaim of a 1h EMA99 wall |

The IDs may be renamed during implementation review, but each strategy must
have one stable ID before any historical evaluation or live enablement.

## Account Boundary

Strategy plugins are deliberately account-agnostic and symbol-dumb.

Plugins must not:

- Read an account ID.
- Read an exchange route.
- Apply a Fundamo, Hyro, Propr, or venue-specific symbol allowlist.
- Emit account, exchange, quantity, leverage, sizing, or order-type fields.
- Decide whether a candidate is eligible for a particular account.

The plugin evaluates every symbol in its evaluator-supplied subscription scope.
Downstream admission and routing own symbol-account-strategy matching. The
three new strategies are intended for the Fundamo deployment, but that fact is
represented only by downstream routing configuration and policy, never by
strategy logic.

## Shared Data Contract

### Canonical base bars

The canonical live market source is confirmed Bybit 5m OHLCV. The gateway may
also retain confirmed 1m bars for strategies whose trigger timeframe is 1m.

All higher-timeframe bars used by strategies must be materialized by the engine
from the canonical market path, with the `1h`/`4h` hybrid seed contract as the
only approved historical exception:

```text
confirmed 5m -> derived 15m
confirmed 5m -> derived 1h
confirmed 5m -> derived 4h
direct completed 1h/4h seed + confirmed 5m tail -> hybrid strategy 1h/4h
```

No strategy may consume a separately fetched or provider-computed 15m, 1h, or
4h bar directly. The engine may read the regime-worker-owned direct 1h/4h seed
and stitch it to canonical 5m-derived data as specified in
`specs/hybrid-htf-engine-v1.md`. Resampling inside a strategy is allowed only
when it uses the cutoff-bounded canonical lower-timeframe rows and does not
introduce a second bar convention.

### Bar timestamp semantics

Every bar timestamp in `source_observations` is its exclusive `source_end`.
For example, a 5m bar ending at `12:05` represents the interval
`[12:00, 12:05)`.

Derived bars must use UTC-aligned closed intervals:

- 15m ending at `12:15` contains 5m bars ending at `12:05`, `12:10`, and
  `12:15`.
- 1h ending at `13:00` contains the twelve 5m bars ending after `12:00` and
  through `13:00`.
- 4h ending at `16:00` contains the forty-eight 5m bars ending after `12:00`
  and through `16:00`.

The derived row must have:

- `source_start = bucket_end - interval`.
- `source_end = bucket_end`.
- `retrieval_kind = resampled`.
- Source and purity provenance inherited from the contributing 5m rows.

The gateway must persist a derived bar only after the complete set of required
5m bars is committed. Partial buckets must not be persisted, and an existing
derived row must be replaceable when a correction is received for its source
bars. A derived row must never be stored under the bucket start timestamp.

All resampling paths, including feature and zone materialization, must use this
same convention. There must be one shared resampling implementation, not
separate gateway, orchestrator, and strategy variants.

### Cutoff inclusion

The cutoff is the end timestamp of the latest completed base bar being
evaluated. A cutoff-bounded query includes rows with:

```text
source_end <= cutoff_at
```

It excludes every row with `source_end > cutoff_at`. A bar whose end timestamp
equals the cutoff is complete and is eligible. A forming bar is never eligible.

Every derived timeframe must apply the same rule. For example, at a 5m cutoff
of `12:05`, the latest eligible 15m bar is the one ending at `12:00`; the 15m
bar ending at `12:15` is not eligible. At a 15m cutoff of `12:15`, the 15m bar
ending at `12:15` is eligible.

### Higher-timeframe hold rule

At an execution cutoff between higher-timeframe boundaries, use the latest
completed higher-timeframe bar with `source_end <= cutoff_at` and hold its
indicator values constant until the next higher-timeframe bar closes.

Every plugin receives an explicit cutoff. It must derive the cutoff from the
cutoff ID or an explicit function argument, never from the current wall clock.
Delayed trigger processing must replay the original point-in-time data, not
the newest available data.

### Indicator parity

The implementation must match the supplied Pine semantics for equivalent
indicators:

- EMA uses the declared length and a documented warmup policy.
- RSI uses Wilder RMA smoothing, equivalent to TradingView `ta.rsi`.
- ATR uses Wilder RMA true-range smoothing, equivalent to TradingView
  `ta.atr`.
- StochRSI is RSI, then rolling RSI high/low, then raw StochRSI, then SMA K and
  SMA D with the declared lengths.
- A zero StochRSI denominator produces raw value `0`.
- Bollinger standard deviation and VWMA formulas must be documented and tested
  against fixed reference vectors.

No strategy may silently substitute a simple rolling mean for Wilder RSI or
ATR. Warmup must be sufficient for the longest indicator; insufficient history
returns no candidate rather than a seeded approximation.

## Common Plugin Contract

Every plugin must:

- Run only against a finalized explicit cutoff.
- Consume only cutoff-bounded bars and completed higher-timeframe values.
- Return candidate drafts; write no events directly from the strategy module.
- Preserve its own complete `feature_snapshot` through the pipeline.
- Include strategy ID, plugin version, asset, direction, observed timestamp,
  expiry, entry, invalidation, and any target it explicitly defines.
- Omit a target when the strategy has no explicit target. The downstream intent
  producer may derive a 2R target from entry and stop.
- Never add a strategy-local RR, stop-distance, account, or clash gate.
- Use one active signal per strategy, asset, and direction.
- Use deterministic candidate identity and replayable metadata.

The downstream admission layer remains authoritative for geometry, target
fallback, RR, freshness, stop distance, clash resolution, and final selection.
An explicitly supplied target is not replaced by the 2R fallback merely because
its RR is below the global minimum; it is admitted or rejected by downstream
policy.

## Evaluation Cadence

| Strategy | Primary evaluation cutoff | Trigger behavior |
|---|---:|---|
| Failed Break | 5m | Every completed 5m cutoff |
| BB-RSI | 5m | Every completed 5m cutoff |
| Williams Fractal | 1m | Every completed 1m cutoff |
| EMA9 Continuation | 1m trigger with 5m setup | Every completed 1m cutoff |
| Dual-Zone long | 5m | Every completed 5m cutoff |
| Dual-Zone short | 5m | Every completed 5m cutoff |
| EMA20 Pullback | 1h signal, 5m coordinator | Only when a new completed 1h bar is available at a 5m cutoff |
| EMA Stack | 5m | Every completed 5m cutoff |
| Gold Trend | 5m | Every completed 5m cutoff |
| MTF Exhaustion | 5m | Every completed 5m cutoff |
| Trend Wall | 15m signal, 5m coordinator | Only when a new completed 15m bar is available at a 5m cutoff |

The live evaluator must not run a 1m strategy only every 5m. If 1m evaluation
triggers are not yet supported by the daemon, the strategy remains disabled
rather than silently losing four out of five trigger opportunities.

The 1h and 15m coordinator strategies must evaluate once per newly completed
signal bar, deduplicated by `(strategy_id, asset, direction, signal_bar_end)`.

## Existing Strategy Repairs

### `failed-break-v3`

Preserve the current intent:

- 5m execution close is the entry.
- 4h confirmed swing failure/reclaim supplies direction and invalidation.
- 4h pivot confirmation uses three bars on each side, with no future-bar use.
- 5m StochRSI uses RSI14, StochRSI14, K3, D3.
- Long trigger is K below 20 crossing above D after a bullish reclaim.
- Short trigger is K above 80 crossing below D after a bearish reclaim.
- Stop is the sweep extreme.
- Explicit target is 2R.
- Signal validity is 5 minutes.

Repair requirements:

- Derive the 4h context from the canonical 5m projection, not a malformed
  15m-to-4h projection.
- Use one confirmed 5m cutoff for both the 5m trigger and 4h setup state.
- Keep the active-event guard and make the setup state deterministic at the
  cutoff.

### `bb-rsi-meanrev-v1`

Lock these parameters:

| Parameter | Value |
|---|---:|
| Execution timeframe | 5m |
| Bollinger length | 30 |
| Bollinger multiplier | 2.0 |
| RSI length | 13 |
| Skinny-width ratio | 0.70 |
| ATR length | 14 |
| Divergence pivot | 5 |
| Stop buffer | 0.25 ATR |

Long is a lower-band/RSI extreme or confirmed bullish divergence. Short is the
mirror upper-band/RSI extreme or bearish divergence. Preserve the middle-band
target when it is explicitly produced; downstream admission may reject it when
it does not meet global RR. Apply the configured Bollinger multiplier in both
band and width calculations. Do not hard-code the feature snapshot defaults.

The centered divergence pivot must be confirmed before it is used and must not
include the current unconfirmed endpoint.

### `williams-fractal-scalp-v1`

Lock these parameters:

| Parameter | Value |
|---|---:|
| Execution timeframe | 1m |
| Minimum bars | 130 |
| Fractal left/right bars | 2 / 2 |
| Trend EMAs | 20 / 50 / 100 |
| Target | 2R |
| Signal horizon | 60 minutes |

The fractal center is the bar two positions before the latest completed bar.
Long requires EMA20 > EMA50 > EMA100, rising EMA20/EMA50, a confirmed bullish
fractal below EMA20, and the fractal close above EMA100. Short is the exact
mirror. Stop selection remains the current EMA50/EMA100 structural rule, and
the target is 2R.

The plugin must be scheduled at 1m and must not be relabeled as a 5m or 15m
candidate by an outer evaluation loop.

### `ema9-continuation-stochrsi-v1`

Lock these parameters:

| Parameter | Value |
|---|---:|
| Setup timeframe | 5m |
| Trigger timeframe | 1m |
| EMA length | 9 |
| Setup lookback | 15 bars |
| RSI/StochRSI | 14 / 14 |
| K/D smoothing | 3 / 3 |
| Extreme thresholds | 20 / 80 |
| Stop buffer | 2 setup ATR14 |
| Minimum stop distance | 0.1% of entry |
| Target | 2R |

The 5m setup requires all closes in the 15-bar setup window to remain on the
correct side of EMA9. The 1m trigger requires a prior relevant StochRSI extreme,
a directionally correct K/D cross, and the latest 1m close on the correct side
of EMA9. The prior extreme must have a bounded trigger-memory window; an
unbounded historical `any()` is not faithful to a continuation trigger and must
be replaced with an explicit parameter.

The strategy must be evaluated on every 1m cutoff while using only the latest
completed 5m setup bar. Candidate expiry remains five minutes unless a later
strategy decision explicitly changes it.

### `dual-zone-follower-v2` and `dual-zone-short-follower-v2`

Preserve the existing v2 thesis and parameters from
`specs/strategy-dual-zone-follower-v2.md`:

- Execution bars: 5m.
- EMA7, EMA26, EMA99 on 5m.
- Confirmed 1h DMI/ADX, DI length14, ADX smoothing14, minimum ADX22.
- Directional DI confirmation enabled.
- Channel A distance1.0%, stop1.0%, target from EMA7 at3.0%.
- Channel B distance1.5%, stop1.0%, target from EMA7 at5.0%.
- Channel A has precedence.
- Signal expiry is five minutes.

Repairs:

- Consume the explicit cutoff rather than wall-clock time.
- Consume the latest confirmed 1h bar derived from canonical 5m data.
- Include confirmed ADX, +DI, -DI, EMA7, EMA26, EMA99, channel, and cutoff in
  the feature snapshot.
- Add the common active-event guard.
- Preserve the long/short symmetry and do not put routing logic in the plugin.

### `ema20-pullback-h4-trend-v1`

The canonical signal timeframe is the completed 1h bar, as stated by its
strategy specification. The 5m coordinator must invoke it only when a new 1h
bar has closed; it must not evaluate the same 1h signal twelve times.

Lock these parameters:

- Local 1h EMA20 and ATR14.
- 4h EMA50 and EMA200 trend context.
- Swing lookback10.
- Long regime: 4h close > EMA200 and EMA50 > EMA200.
- Short regime: exact inverse.
- Long signal: low <= EMA20, close > EMA20, bullish current candle, bearish
  previous candle, and close >= previous open.
- Short signal: exact mirror.
- Long stop: lowest low of the 10-bar 1h lookback minus 1 ATR14.
- Short stop: highest high of the 10-bar 1h lookback plus 1 ATR14.
- Target is not required; when omitted, downstream producer fallback is 2R.

The observed bar, expiry, and coordinator deduplication must be explicit. The
implementation must resolve the current contradiction between registry cadence
5m, 1h signal data, and five-minute validity without duplicating signals.

### `ema-stack-15m-adx-stochrsi-5m-v1`

Preserve the specification in
`specs/strategy-ema-stack-adx-stochrsi-5m-v1.md`:

- 5m execution.
- Confirmed 15m EMA20/50/100/200 stack.
- Maximum EMA20/EMA200 spread1.0%.
- Confirmed 1h ADX14 with smoothing14 and minimum20.
- RSI14, StochRSI14, K3, D3, oversold20, overbought80.
- 5m ATR14 and stop at EMA200 +/- 1.5 ATR.
- Target2R.

Repairs:

- Use Wilder RSI rather than the current rolling-simple-average RSI.
- Calculate and pass the configured ADX value rather than checking a hard-coded
  threshold outside the evaluator.
- Include raw StochRSI, confirmed ADX/DI, all EMA values, ATR, stop, target, and
  cutoff in the feature snapshot.
- Add the common active-event guard.
- Do not use the unused `strength` argument as a substitute for the actual ADX
  value.

## New Strategy Definitions

### `gold-trend-ema-bb-stoch-v1`

Execution is on completed 5m bars. Parameters:

| Parameter | Value |
|---|---:|
| Fast EMA | 50 |
| Slow EMA | 200 |
| Bollinger length | 20 |
| Bollinger standard deviation | 2.0 |
| RSI length | 14 |
| StochRSI length | 14 |
| K/D smoothing | 3 / 3 |
| Oversold/overbought | 20 / 80 |
| ATR length | 14 |
| ATR stop multiplier | 3.5 |
| Bollinger touch tolerance | 0.0005 |

Long requires close above EMA50 and EMA200, low touching the lower Bollinger
Band within tolerance, and K crossing above D while K < 20. Short is the exact
mirror at the upper band with K > 80.

Long invalidation is entry minus ATR14 * 3.5. Short invalidation is entry plus
ATR14 * 3.5. No explicit target is required; omit it and allow downstream 2R
fallback. The EMA50 failure exit is strategy-management evidence, not a reason
to mutate a previously emitted stop or target.

The name is historical. Unless a separate scope decision is recorded, the
strategy evaluates every symbol in its evaluator input rather than only gold.

### `mtf-exhaustion-reversal-v1`

Execution is on completed 5m bars. All 1h and 4h inputs come from the canonical
5m-derived projections and are held at the latest completed values.

Parameters:

| Parameter | Value |
|---|---:|
| RSI length | 14 |
| 4h divergence lookback | 24 bars |
| StochRSI length | 14 |
| K/D smoothing | 3 / 3 |
| 1h ADX length/smoothing | 14 / 14 |
| Maximum 1h ADX | 25 |
| 1h RSI long/short extreme | <30 / >70 |
| ATR length | 16 |
| ATR stop multiplier | 2.0 |
| VWAP horizon | 24 hours |

Long requires a confirmed bullish 4h RSI divergence, 1h RSI <30, 1h ADX <25,
and a 5m StochRSI K/D bullish cross with K <20. Short is the mirror.

The divergence must compare two confirmed 4h price swing lows/highs and their
corresponding RSI values. A live 5m close must not be compared to a rolling
4h level as a substitute for divergence.

The 24H VWAP parameter is timeframe-qualified:

- VWMA length96 on 15m derived bars, or
- VWMA length288 on 5m bars.

The implementation must choose one and record it in the feature snapshot. If a
VWAP target is explicitly emitted, it must satisfy directional geometry and
downstream RR. Otherwise omit it and use the producer's 2R fallback. Stop is
entry +/- ATR16 * 2 and is locked at signal creation.

### `trend-wall-v1`

The signal timeframe is completed 15m. The 5m coordinator evaluates only when
a new 15m bar has closed. Its 1h context is derived from canonical 5m bars.

Parameters:

| Parameter | Value |
|---|---:|
| 1h wall EMA | 99 |
| 1h trend EMAs | 7 / 26 |
| 1h ADX length | 14 |
| Minimum 1h ADX | 20 |
| RSI length | 14 |
| RSI turn thresholds | 40 / 60 |
| Execution ATR length | 16 |
| Wall proximity | 1% |
| Wall-break buffer | 0.5 execution ATR |
| Volume MA length | 20 |
| Minimum volume ratio | 0.5 |

Long requires the completed 15m close above the 1h EMA99 wall and within 1% of
it, a 15m low touching the wall, 1h EMA7 > EMA26, 1h ADX >20, RSI turning up
below40, and volume ratio >0.5. Short is the exact mirror.

Structural exit evidence is a 15m close beyond the wall by 0.5 execution ATR.
Emergency invalidation is entry +/- 2 execution ATR. No explicit target is
required; omit it and allow downstream 2R fallback. Stops are locked when the
candidate is created.

The strategy overlaps conceptually with Dual-Zone. Both candidates may be
produced, but downstream scoring and clash resolution decide whether either is
selected. The plugin must not suppress the other strategy itself.

## Pipeline Repairs

The implementation must also repair these shared defects before enabling any new
strategy:

1. The gateway resampler must use complete 5m buckets and correct end
   timestamps.
2. Feature and zone materialization must be cutoff-bounded and use the shared
   resampler; it must not query future rows.
3. Plugin invocation must pass an explicit cutoff and preserve the strategy's
   feature snapshot rather than replacing it with the generic snapshot.
4. Strategy modules must return drafts only. Central invocation owns admission,
   raw capture, selection, and event writing.
5. Registry cadence must be explicit for every strategy. A plugin with a 1m,
   5m, 15m, or coordinator cadence must not run under unrelated interval passes.
6. The live daemon must provide a real 1m trigger path before enabling the two
   1m strategies at their declared cadence.
7. Candidate identity must include the strategy ID, plugin version, asset,
   direction, observed bar end, and input cutoff snapshot.
8. Active-event checks must be deterministic and shared where possible.

## Downstream Contract

The producer and admission layers remain unchanged in ownership:

- Missing target may receive a 2R producer fallback.
- Explicit target is preserved and independently admitted.
- Admission owns minimum RR2.0, stop-distance limits, freshness, expiry,
  geometry, and candidate clashes.
- The intent builder and shared SQLite bus own delivery and downstream routing.
- The executor owns account-specific sizing, leverage, order type, precision,
  protection, and receipts.

The three new strategies must be routed downstream according to the deployment
policy intended for Fundamo. No strategy candidate may contain account-aware
logic or fields.

## Required Tests

### Resampling and cutoff

- A 5m bar ending at a boundary belongs to the correct higher-timeframe bucket.
- No partial 15m/1h/4h bar is persisted.
- Derived `source_end` equals bucket end, never bucket start.
- A cutoff includes `source_end == cutoff` and excludes later rows.
- A delayed trigger produces the same candidate as immediate processing.
- Feature zones cannot use rows after the cutoff.

### Indicator parity

- Fixed reference vectors compare RSI and ATR against Wilder/Pine values.
- StochRSI K/D vectors match the declared 14/3/3 calculation.
- Bollinger multiplier changes affect both bands and width calculations.
- EMA warmup does not emit seeded candidates before sufficient history.

### Strategy behavior

- Each of the eight repaired strategies has long and short fixtures.
- Each new strategy has entry, rejection, stop geometry, and cutoff fixtures.
- MTF Exhaustion requires confirmed 4h divergence rather than a level proxy.
- TrendWall evaluates once per new 15m bar.
- Williams and EMA9 evaluate once per completed 1m cutoff.
- EMA20 evaluates once per new completed 1h signal bar.
- No strategy writes directly to the alpha outbox.
- Strategy feature snapshots survive central invocation unchanged.
- No candidate contains account, exchange, sizing, leverage, or order-type data.

### Runtime and integration

- Registry cadence filtering is tested for every strategy.
- Duplicate cutoff processing produces no duplicate candidate or intent.
- Downstream routing tests verify account policy without importing routing into
  strategy modules.
- The full suite and `git diff --check` pass before implementation is accepted.

## Acceptance Criteria

Implementation is complete only when:

1. All eight existing IDs produce decisions from the parameters stated here and
   their referenced family specs.
2. All three new families produce replayable candidates from the stated
   parameters.
3. Every HTF value is derived from cutoff-bounded canonical 5m data.
4. No forming or future bar can affect an event.
5. Live cadence matches the strategy table.
6. Account matching remains exclusively downstream.
7. Missing targets use the producer 2R fallback without strategy-side mutation.
8. Existing target-bearing strategies retain their explicit target for admission.
9. The eight repairs and three additions are independently observable in
   plugin, candidate, admission, and intent ledgers.
10. The new strategies remain disabled until historical replay and paper-path
    validation are completed.
