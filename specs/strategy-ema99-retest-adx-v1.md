# EMA99 Retest with 1H ADX v1

## Status

Revised implementation contract for the current direct-HTF, shared-computation,
completed-5m engine. This strategy replaces the live dual-zone v2 long and
short plugins with one bidirectional strategy after explicit rollout
approval.

This revision changes the engine integration boundary, not the EMA99 cross and
retest thesis. It removes unsupported analyst-side position-management claims:
Research Analyst produces candidate intents only. The executor owns venue
protection, hard exits, fills, and position truth.

The supplied Pine source is the behavioral reference. The implementation must
use finalized bars only and must not reproduce TradingView's realtime 1H
lookahead/repainting behavior.

## Decision Summary

- Canonical strategy ID: `ema99-retest-adx-v1`.
- One plugin evaluates both long and short directions.
- Primary evaluation and execution reference timeframe: completed 5m bars.
- Higher-timeframe filter: the latest eligible completed 1h ADX/DMI observation.
- Long and short entries are stateful: qualifying EMA cross, then EMA99 retest.
- The proposed ATR invalidation remains strategy-defined; the executor owns
  venue placement and protection confirmation.
- The strategy does not define a take-profit target. The executor derives and
  manages its configured 2R protection policy.
- The executor owns final order type, sizing, leverage, and venue behavior.
- Downstream routing selects the executor destination. Any separately enabled
  Propr fan-out remains an independent downstream route.
- Public alpha messages omit confidence until a separate calibration contract is
  approved. The internal alpha schema may retain confidence for audit only.

The retired IDs remain recognized only as legacy metadata during migration:

- `dual-zone-follower-v2`
- `dual-zone-short-follower-v2`

They must not remain active registrations after cutover.

## Scope

### In scope

- Closed-bar 5m EMA26/EMA99 cross detection.
- Closed-bar 1h ADX filter and directional state arming.
- Closed-bar EMA99 retest detection with wick tolerance.
- One entry per qualifying cross and direction.
- Closed-bar ATR invalidation calculation.
- Existing admission controls and downstream routing.
- Durable state reconstruction from finalized market bars.
- Replay and provenance tests for the candidate path.

### Out of scope

- Strategy-owned take-profit levels.
- Strategy-selected order type.
- Strategy sizing, leverage, or account risk policy.
- Live strategy-specific exit or stop-revision delivery.
- Intrabar, tick, or forming-candle decisions.
- Sharing state between symbols.
- Re-entry after an exit without a new qualifying EMA cross.

## Parameters

All parameters require the `EMA99_RETEST_` configuration prefix. Long and short
use the same values because they are one strategy.

| Parameter | Default | Meaning |
| --- | ---: | --- |
| `EMA99_RETEST_FAST_EMA_LENGTH` | `26` | Fast/local EMA |
| `EMA99_RETEST_SLOW_EMA_LENGTH` | `99` | Slow/local EMA and retest level |
| `EMA99_RETEST_RSI_LENGTH` | `14` | 5m RSI length |
| `EMA99_RETEST_ATR_LENGTH` | `14` | 5m ATR length |
| `EMA99_RETEST_ATR_STOP_MULTIPLIER` | `2.0` | ATR distance from trigger wick |
| `EMA99_RETEST_ADX_TIMEFRAME` | `1h` | Higher-timeframe filter |
| `EMA99_RETEST_ADX_LENGTH` | `14` | 1h DMI length |
| `EMA99_RETEST_ADX_SMOOTHING` | `14` | 1h ADX smoothing |
| `EMA99_RETEST_MIN_ADX` | `25.0` | Strict minimum ADX at the cross |
| `EMA99_RETEST_MAX_RETEST_DISTANCE_PCT` | `0.1` | Maximum close distance from EMA99 |
| `EMA99_RETEST_LONG_EXIT_RSI` | `72.0` | Strict long RSI exit threshold |
| `EMA99_RETEST_SHORT_EXIT_RSI` | `28.0` | Strict short RSI exit threshold |
| `EMA99_RETEST_EXIT_SPREAD_PCT` | `0.5` | Minimum close-to-EMA26 exit spread |

The 1h `+DI` and `-DI` values are calculated and recorded for observability,
but they are not an entry gate. The supplied Pine strategy gates only on ADX.

## Current Engine Boundary

Production evaluation follows this path:

```text
regime.sqlite3 direct Bybit REST 1h history
        |
        +--> invocation-scoped direct HTF context --> 1h ADX/DMI

market.sqlite3 committed completed 5m history
        |
        +--> shared computation context --> 5m EMA/RSI/ATR features

5m features + direct 1h ADX/DMI
        -> one bidirectional EMA99 plugin
        -> raw candidate -> admission -> alpha event -> downstream router
```

The plugin is source-blind. Its pure evaluator receives cutoff-bound frames and
materialized numerical inputs; it does not call Bybit, choose a database, write
an event, perform admission, or open a second executor inbox. The thin runtime
adapter may use the existing `strategy_market_connection`,
`load_bars_for_interval`, `context.features`, and `context.dmi_adx` seams for
direct unit-call compatibility and invocation-scoped loading. It must never
compute 1h data from canonical 5m bars or bypass the direct HTF context.

The registry declaration is authoritative for orchestration:

- cadence: `5m`;
- required intervals: `5m`, `1h`;
- required features: 5m EMA26, EMA99, RSI14, and ATR14;
- stateful: `true` because cross/retest state is replayed sequentially;
- lookback: sufficient for the engine's direct seed and strategy warmup.

## Data and Point-in-Time Rules

At a 5m cutoff `t`:

1. The 5m input contains only candles whose source end is at or before `t`.
2. The 1h input comes only from the regime-owned direct history context. It
   contains native bars whose source end is at or before `t`; a forming, future,
   canonical-resampled, or mixed-source 1h bar is never used.
3. EMA26, EMA99, RSI14, and ATR14 are materialized once by the shared 5m
   computation context using the repository's in-house kernels.
4. ADX, `+DI`, and `-DI` are materialized once by the shared direct-HTF context
   using the repository's in-house Wilder/DMI contract equivalent to Pine
   `ta.dmi(14, 14)`. The strategy gates only on ADX.
5. Direct-history, feature, or indicator readiness failure produces no
   candidate. It is an
   unavailable-data result, not a strategy error.
6. Evaluation cadence is the completed 5m cutoff. No intrabar value can arm,
   enter, or change the candidate.

## Entry State Machine

State is maintained independently per symbol and is reconstructed by replaying
finalized 5m bars. It must not be held only in process memory.

```text
                         +----------------+
                         | Neutral / idle |
                         +--------+-------+
                                  |
                   golden cross + ADX > 25
                                  v
                         +----------------+
                         | Waiting long   |
                         +--------+-------+
                                  |
                         EMA99 long retest
                                  v
                         +----------------+
                         | Long emitted   |
                         +----------------+

                         +----------------+
                         | Waiting short  |
                         +--------+-------+
                                  |
                        EMA99 short retest
                                  v
                         +----------------+
                         | Short emitted  |
                         +----------------+
```

### Cross detection

For the current closed 5m bar `t` and prior closed bar `t-1`:

```text
golden_cross = EMA26[t] > EMA99[t] and EMA26[t-1] <= EMA99[t-1]
death_cross  = EMA26[t] < EMA99[t] and EMA26[t-1] >= EMA99[t-1]
trend_ok     = ADX1h[t] > EMA99_RETEST_MIN_ADX
```

When `golden_cross and trend_ok`:

- `waiting_long = true`;
- `waiting_short = false`;
- `traded_long = false`.

When `death_cross and trend_ok`:

- `waiting_short = true`;
- `waiting_long = false`;
- `traded_short = false`.

If the ADX test fails on the cross bar, no waiting state is armed. A later ADX
improvement does not retroactively arm that cross.

### Retest detection

The EMA99 retest is evaluated after cross state has been replayed through the
current closed bar.

```text
long_retest = waiting_long
              and low[t] <= EMA99[t]
              and close[t] >= EMA99[t]
              and (close[t] - EMA99[t]) / EMA99[t] <= 0.001

short_retest = waiting_short
               and high[t] >= EMA99[t]
               and close[t] <= EMA99[t]
               and (EMA99[t] - close[t]) / EMA99[t] <= 0.001
```

The wick may cross EMA99. The closing price must remain on the correct side.
The 0.1% distance boundary is inclusive.

### Entry emission

On a long retest when `traded_long` is false:

- Save `long_trigger_low = low[t]`.
- Mark `traded_long = true`.
- Clear `waiting_long`.
- Emit one long candidate.

On a short retest when `traded_short` is false:

- Save `short_trigger_high = high[t]`.
- Mark `traded_short = true`.
- Clear `waiting_short`.
- Emit one short candidate.

The same direction cannot emit another entry until a new qualifying same-side
cross resets its traded flag. An exit, stop, restart, or PM action does not by
itself authorize re-entry.

## Entry Event Contract

The event remains an advisory entry thesis. It must include:

- `strategy_id=ema99-retest-adx-v1`;
- `direction=long|short`;
- `setup_class=ema99_retest_adx`;
- `phase=long_retest|short_retest`;
- completed-bar `observed_at`, `valid_until`, and `horizon_minutes=5`;
- `entry_price` and the existing entry reference condition;
- `invalidation_price` from the ATR stop calculation;
- `targets=[]` as an explicit empty list;
- internal confidence fields required by the alpha event schema;
- a feature snapshot containing the source symbol, cutoff, EMA26, EMA99, RSI,
  ATR, ADX, `+DI`, `-DI`, cross type, retest distance, and trigger wick.

`targets=[]` means the strategy intentionally supplies no target. It must not
be omitted because the alpha event schema requires the field. Admission may
derive a target in its private admission proof, and the alpha outbox may persist
that admitted target for delivery auditability. The derived value is never a
strategy target and is never used in the strategy's entry or exit rules. The
executor intent builder remains the owner of the final 2R delivery target.

The executor continues to own:

- 2R target derivation and final protection policy;
- entry order type;
- quantity, sizing, and leverage;
- venue precision and attached protection;
- delivery receipts, fills, and position truth.

## ATR Stop Protection

The stop follows the supplied Pine geometry:

```text
long_stop  = long_trigger_low  - ATR14_5m[current_closed_bar] * 2.0
short_stop = short_trigger_high + ATR14_5m[current_closed_bar] * 2.0
```

At entry emission, `current_closed_bar` is the retest bar. The proposed stop is
the candidate invalidation price and passes through normal admission. The
executor owns final protection placement, venue precision, protection
confirmation, hard exits, and later stop management. Research Analyst does not
run a position loop, emit stop-revision decisions, or weaken a confirmed venue
stop.

The pure `evaluate_stop_revision` helper, if retained for offline parity
research, is not part of the live analyst contract and must not be wired into
the publisher or executor decision inbox.

## Exit Reference

The RSI/EMA26 exit formulas remain a research reference for the strategy thesis.

```text
long_exit  = RSI14[t] > 72.0
             and (close[t] - EMA26[t]) / EMA26[t] > 0.005

short_exit = RSI14[t] < 28.0
             and (EMA26[t] - close[t]) / EMA26[t] > 0.005
```

Both conditions are strict. Equality does not trigger an exit. RSI and spread
must be true on the same completed 5m bar.

No live exit decision is emitted by this repository. The standalone PM and
executor remain governed by their own contracts; this strategy metadata cannot
override them. Any future strategy-specific exit handoff requires a separate
cross-repository specification and executor implementation.

## Admission and Routing

The candidate continues through the existing pipeline:

```text
completed 5m cutoff
    -> one bidirectional strategy plugin
    -> raw candidate capture
    -> symbol/account policy
    -> hard admission and clash resolution
    -> alpha outbox
    -> executor intent construction
    -> shared SQLite intent bus -> downstream executor route
```

Existing global admission remains in force, including freshness, directional
stop geometry, structural HTF zone checks, and the configured ATR-based risk
policy. Those controls are downstream policy and are not part of the Pine
thesis. The proposed strategy stop remains authoritative and is never mutated
by admission.

The strategy must not create a second analyst-local executor inbox. The shared
intent bus remains the authoritative handoff, and downstream routing
must be resolved from the canonical strategy ID rather than caller overrides.

## Migration

### Legacy positions

Positions opened under `dual-zone-follower-v2` or
`dual-zone-short-follower-v2` retain their originating metadata and legacy
management interpretation. The new RSI/spread policy must not be applied
retroactively to them.

### Cutover sequence

1. Keep the canonical strategy disabled while implementation and replay tests
   run.
2. Add the canonical ID to the registry, required-feature declaration, and
   admission classification. Configure its destination in the downstream
   router, not in the strategy.
3. Verify direct 1h loading and shared 5m feature/DMI materialization at an
   exact historical cutoff.
4. Verify the canonical plugin evaluates both directions from one registration.
5. Verify targetless strategy events produce executor-derived 2R intents with a
   passing admission proof.
6. Remove the two old IDs from the active production allowlist while retaining
   their legacy metadata and routing recognition.
7. Enable the canonical ID only after replay, lookahead, admission, and paper
   delivery checks pass.
8. Restart only the managed services importing changed modules through `oxmgr`.
9. Verify completed 5m cycles, valid events, downstream routing, independent
   Propr fan-out behavior when enabled, and clean publisher state.

No existing executor position or shared intent-bus record may be deleted as part
of this strategy cutover.

## Required Tests

### Signal parity

- EMA cross equality cases match the inclusive prior-bar rules.
- ADX threshold is strict: `25.0` does not arm; values above it do.
- Forming 1h bars are excluded.
- Long and short retest wick cases match the Pine formulas.
- The 0.1% retest boundary is accepted; values above it are rejected.
- A cross with insufficient ADX never arms a later retest.
- Opposite crosses clear the previous waiting direction.
- Only one entry is emitted per qualifying cross.
- State replay after restart matches uninterrupted replay.
- Long and short are emitted through one plugin registration.

### Protection and exit references

- Initial long/short ATR stops use the retest trigger wick.
- Later stop revisions use the fixed trigger wick and current completed-bar ATR.
- RSI/spread exit requires both conditions on the same closed 5m bar.
- RSI and spread equality boundaries do not exit.
- No analyst-side mechanical exit or stop-revision decision is delivered.
- Executor and standalone-PM contracts remain unaffected by this plugin.

### Contract and operations

- Strategy output events contain `targets=[]`; any admitted target persisted by
  the outbox is delivery metadata, not strategy output.
- Executor intent construction derives the configured 2R target downstream.
- Entry order type is absent from the strategy event and selected by executor
  profile policy.
- Strategy candidates contain no account or venue routing fields.
- Legacy dual-zone positions remain identifiable and are not migrated silently.
- Shared intent-bus delivery is idempotent.
- No direct database or network access occurs inside pure strategy evaluation.
- Direct HTF provenance is attached by the engine and matches the evaluation
  cutoff.
- Full repository tests, focused strategy tests, and `git diff --check` pass.

## Acceptance Criteria

The replacement is ready for live activation only when all of the following
hold:

- Every evaluation uses committed completed 5m data and regime-owned direct 1h
  data at the exact evaluation cutoff.
- The implementation matches the defined cross, retest, and ATR invalidation
  rules on deterministic fixtures.
- The plugin uses the shared computation/direct HTF contexts and declares its
  required intervals, features, and stateful replay behavior.
- Long and short behavior is exposed by one active strategy plugin.
- No strategy-owned target or order-type behavior is present.
- Executor-derived 2R intent delivery is verified without changing the alpha
  thesis.
- No analyst-side PM loop, exit decision delivery, or stop-revision delivery is
  introduced.
- Existing legacy positions and records remain recoverable.
- Production logs show successful 5m evaluations, valid event counts, clean
  candidate/intent delivery, and healthy managed services.
