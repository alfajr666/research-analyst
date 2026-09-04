# Fundamo EMA99 Retest with 1H ADX v1

## Status

Implementation specification. This strategy replaces the live dual-zone v2
long and short plugins with one bidirectional Fundamo strategy.

The supplied Pine source is the behavioral reference. The implementation must
use finalized bars only and must not reproduce TradingView's realtime 1H
lookahead/repainting behavior.

## Decision Summary

- Canonical strategy ID: `ema99-retest-adx-fundamo-v1`.
- One plugin evaluates both long and short directions.
- Primary evaluation and execution reference timeframe: completed 5m bars.
- Higher-timeframe filter: the previous completed 1h ADX/DMI observation.
- Long and short entries are stateful: qualifying EMA cross, then EMA99 retest.
- The RSI/EMA26-spread exit is deterministic and authoritative.
- The RSI/EMA26-spread exit is not delegated to the LLM PM sidecar.
- ATR protection remains strategy-defined; the executor owns venue placement and
  protection confirmation.
- The strategy does not define a take-profit target. The executor derives and
  manages its configured 2R protection policy.
- The executor owns final order type, sizing, leverage, and venue behavior.
- Bybit delivery is routed to the `fundamo` account. Any separately enabled
  Propr fan-out remains an independent downstream route.

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
- Closed-bar ATR stop calculation and subsequent stop revisions.
- Closed-bar RSI/EMA26-spread mechanical exits.
- Fundamo routing and existing admission controls.
- Durable state reconstruction from finalized market bars.
- Durable mechanical exit decision delivery to the executor.

### Out of scope

- Strategy-owned take-profit levels.
- Strategy-selected order type.
- Strategy sizing, leverage, or account risk policy.
- LLM selection of the RSI/spread exit.
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

## Data and Point-in-Time Rules

At a 5m cutoff `t`:

1. The 5m input contains only candles whose source end is at or before `t`.
2. The latest 1h input is the last completed 1h candle whose source end is at
   or before `t`. A forming 1h candle is never used.
3. EMA26, EMA99, RSI14, and ATR14 are calculated from completed 5m closes and
   OHLC values.
4. ADX, `+DI`, and `-DI` use Wilder/DMI semantics equivalent to the Pine
   `ta.dmi(14, 14)` calculation, but with the confirmed 1h observation.
5. Insufficient history produces no candidate or exit decision. It is an
   unavailable-data result, not a strategy error.
6. Evaluation cadence is the completed 5m cutoff. No intrabar value can arm,
   enter, stop-update, or exit a position.

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

- `strategy_id=ema99-retest-adx-fundamo-v1`;
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
be omitted because the alpha event schema requires the field. Admission and
intent construction may derive the executor's configured 2R target, but that
derived value must not be written back as a strategy target or used in the
strategy's entry/exit rules.

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

At entry emission, `current_closed_bar` is the retest bar. After a position is
open, every subsequent completed 5m bar recalculates the stop using the fixed
trigger wick and the latest completed-bar ATR. The stop may therefore move as
ATR changes, but the trigger wick never moves.

Stop updates are mechanical protection revisions, not LLM decisions. A failed
revision must never remove or weaken the last confirmed venue stop. The
executor remains authoritative for applying, confirming, retrying, and
recording each revision.

If the executor cannot yet accept an external mechanical stop revision, that
capability is a prerequisite for Pine-faithful live activation. Freezing the
initial ATR stop is explicitly not equivalent behavior.

## Mechanical Exit Policy

The strategy exit is evaluated for open positions originating from the
canonical strategy ID, using the same completed 5m cutoff as the strategy
evaluation.

```text
long_exit  = RSI14[t] > 72.0
             and (close[t] - EMA26[t]) / EMA26[t] > 0.005

short_exit = RSI14[t] < 28.0
             and (EMA26[t] - close[t]) / EMA26[t] > 0.005
```

Both conditions are strict. Equality does not trigger an exit. RSI and spread
must be true on the same completed 5m bar.

When triggered:

1. Persist a deterministic mechanical exit trigger before delivery.
2. Include position ID, position revision, strategy ID, side, cutoff, RSI,
   EMA26, close, spread, and the rule name in the trigger record.
3. Write an executor `EXIT` decision with `controller=mechanical_strategy`.
4. Do not call the LLM for that triggered exit.
5. Do not allow an LLM `HOLD`, `REDUCE`, or other response to veto or replace
   the triggered full exit.
6. Let the executor own reduce-only close submission, reconciliation, retry,
   and venue-confirmed closure.

The existing LLM PM sidecar may continue its normal observation of positions,
but it is not the authority for this strategy's RSI/spread exit. The mechanical
decision must be idempotent by position ID, position revision, strategy policy,
and 5m cutoff.

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
    -> Fundamo delivery
```

Existing global admission remains in force, including freshness, directional
stop geometry, stop-distance limits, and any configured ATR-based risk floor.
Those controls are downstream policy and are not part of the Pine thesis.

The strategy must not create a second analyst-local executor inbox. The shared
intent bus remains the authoritative handoff, and the existing Fundamo route
must be resolved from the canonical strategy ID rather than caller overrides.

## Migration

### Legacy positions

Positions opened under `dual-zone-follower-v2` or
`dual-zone-short-follower-v2` retain their originating metadata and legacy
management interpretation. The new RSI/spread policy must not be applied
retroactively to them.

### Cutover sequence

1. Add the canonical strategy implementation and its mechanical exit policy in
   disabled/shadow mode.
2. Add the canonical ID to Fundamo routing and admission registries.
3. Remove the two old IDs from active plugin registration while retaining their
   legacy metadata and routing recognition.
4. Verify the canonical plugin evaluates both directions from one registration.
5. Verify targetless strategy events produce executor-derived 2R intents.
6. Verify mechanical exits and ATR revisions against mocked executor snapshots.
7. Enable live evaluation for the canonical ID.
8. Restart only the managed services required by the deployment through
   `oxmgr`.
9. Verify completed 5m cycles, zero invalid events, Fundamo routing, decision
   delivery, and no LLM call on mechanical triggers.

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

### Protection and exits

- Initial long/short ATR stops use the retest trigger wick.
- Later stop revisions use the fixed trigger wick and current completed-bar ATR.
- RSI/spread exit requires both conditions on the same closed 5m bar.
- RSI and spread equality boundaries do not exit.
- Mechanical exit decisions use the exact position ID and cutoff identity.
- Mechanical exits do not call the LLM and cannot be vetoed.
- Stop revision failure retains the last confirmed venue protection.

### Contract and operations

- Candidate events contain `targets=[]`, not a strategy-generated target.
- Executor intent construction derives the configured 2R target downstream.
- Entry order type is absent from the strategy event and selected by executor
  profile policy.
- Fundamo routing cannot be overridden by stale or caller-supplied routing.
- Legacy dual-zone positions remain identifiable and are not migrated silently.
- Shared intent-bus delivery is idempotent.
- Full repository tests, focused strategy tests, and `git diff --check` pass.

## Acceptance Criteria

The replacement is ready for live activation only when all of the following
hold:

- Every evaluation uses only completed 5m and confirmed 1h data.
- The implementation matches the defined cross, retest, ATR, and RSI/spread
  rules on deterministic fixtures.
- Long and short behavior is exposed by one active strategy plugin.
- No strategy-owned target or order-type behavior is present.
- Executor-derived 2R intent delivery is verified without changing the alpha
  thesis.
- Mechanical exits are durably delivered and cannot be delegated to or vetoed
  by the LLM PM sidecar.
- Dynamic ATR stop revisions are supported and venue-confirmed, or live
  activation is blocked.
- Existing legacy positions and records remain recoverable.
- Production logs show successful 5m evaluations, valid event counts, clean
  mechanical decision delivery, and healthy managed services.
