# EMA99 Wall + 5m StochRSI v1

## Status

Implementation specification for a multi-timeframe strategy using the universal
mechanical exit sidecar. Default scope is always the Bybit Fundamo account and
all 97 static symbols.

## Identity and Scope

```text
strategy_id: ema99-wall-stochrsi-v1
plugin_version: v1
universe: symbols/static_universe.json (97 symbols)
entry timeframe: 5m
context timeframe: 1h
account route: bybit/fundamo
direction: long and short
```

Evaluate only completed candles. The 1h context is held constant between
completed 1h boundaries; the 5m trigger is evaluated on each completed 5m
cutoff.

## Parameters

### 1h wall and regime

```text
wall_timeframe: 1h
wall_ema_length: 99
wall_lookback: 5
htf_rsi_length: 14
long_htf_rsi_min: 45
long_htf_rsi_max: 60
short_htf_rsi_min: 40
short_htf_rsi_max: 55
```

### 5m trigger

```text
execution_rsi_length: 14
stochrsi_length: 14
k_smoothing: 3
oversold: 20
overbought: 80
```

### Stop

```text
htf_atr_length: 14
atr_stop_buffer: 2.0
```

### Mechanical TA exit

```text
long_exit_rsi: 70
short_exit_rsi: 30
```

The mechanical exit policy is `ema99-wall-stochrsi-v1` and runs on completed
5m candles.

## 1h Context

On each completed 1h cutoff, calculate:

```text
wall_ema = EMA(close, 99)
wall_delta = close - wall_ema
```

Long wall validity:

```text
lowest(close - EMA99, 5)[1] >= 0
```

Short wall validity:

```text
highest(close - EMA99, 5)[1] <= 0
```

The lookback tests completed 1h closes only. Wicks through EMA99 do not fail
the wall condition.

Also calculate from completed 1h bars:

```text
htf_rsi = RSI(close, 14)
structure_low = lowest(low, 5)
structure_high = highest(high, 5)
htf_atr = ATR(14)
```

The strategy uses the most recently completed 1h values, never an in-progress
1h candle.

## 5m StochRSI

Calculate on completed 5m closes:

```text
rsi5 = RSI(close, 14)
rsi_low = lowest(rsi5, 14)
rsi_high = highest(rsi5, 14)
stoch_raw = 0 when rsi_high == rsi_low
            100 * (rsi5 - rsi_low) / (rsi_high - rsi_low) otherwise
k = SMA(stoch_raw, 3)
d = SMA(k, 3)
```

Long trigger:

```text
cross(k, d, upward)
and min(k, k[1]) <= 20
```

Short trigger:

```text
cross(k, d, downward)
and max(k, k[1]) >= 80
```

The current or immediately previous 5m bar may supply the extreme condition.

## Entry Rules

Long entry requires:

```text
long wall valid
1h RSI in [45, 60]
long StochRSI trigger
no existing position for the symbol/account
```

Short entry requires:

```text
short wall valid
1h RSI in [40, 55]
short StochRSI trigger
no existing position for the symbol/account
```

Emit one side-specific event per symbol and cutoff. `pyramiding=0` is enforced
by universal admission and the executor's position check.

## Entry Intent

The analyst emits:

```text
entry_price: completed 5m close
invalidation_price:
  long  = structure_low - 2 * htf_atr
  short = structure_high + 2 * htf_atr
fixed take_profit: absent
```

The analyst does not emit `order_type`; the executor selects it from the
Fundamo execution profile. The locked stop remains active for the position.

Universal entry/stop geometry and stop-distance gates still apply. A fabricated
1:2 target must not be added merely to satisfy a fixed-target contract.

## Historical Mechanical Exit Policy

This policy is retained as strategy research context. It is not an independent
sidecar or a competing decision authority in the locked PM design. The LLM PM
sidecar receives the strategy parameters and current indicator context and may
return `hold`, `reduce`, `exit`, or `near_tp` under the locked confidence and
executor-protection rules.

The policy evaluates every completed 5m cutoff while the position is open.

Long mechanical exit:

```text
rsi5 > 70 and k >= 80
```

Short mechanical exit:

```text
rsi5 < 30 and k <= 20
```

The conditions above are historical strategy context only in the locked design.
They are not emitted by an independent mechanical sidecar or used as a competing
decision stream. The LLM may use the indicator context when deciding `hold`,
`reduce`, `exit`, or `near_tp`, but the executor's SL/TP and protection behavior
remain independent of the LLM.

## Event Metadata

Entry events include:

```text
strategy_id
plugin_version
input_snapshot_id
execution_timeframe: 5m
context_timeframe: 1h
wall_ema
wall_lookback
htf_rsi
stoch_k
stoch_d
structure_low/high
htf_atr
management_mode: analyst_managed
mechanical_exit_policy: ema99-wall-stochrsi-v1
```

Position-management events include the mechanical policy inputs, trigger ID,
LLM request ID when applicable, decision ID, veto status, and executor receipt.

## Routing

All events from this strategy route exclusively to:

```text
exchange_id: bybit
account_id: fundamo
```

The 97-symbol static universe is loaded from `config.load_static_symbols()`.
No compact asset set, discovery watchlist, or rotation feed is permitted.

## Logging and Observability

Entry logs must show:

```text
strategy_id, asset, cutoff, wall_status, htf_rsi, stoch_k, stoch_d,
direction, entry, stop, route, admission_status, rejection_reason
```

Mechanical-exit logs must show:

```text
policy_id, policy_version, asset, side, position_id, cutoff,
rsi5, stoch_k, trigger_result, trigger_event_id, veto_allowed
```

LLM decision logs must show:

```text
request_id, trigger_event_id, model, latency_ms, action,
decision_scope, veto_applied, reduced_size_mode, reason, delivery_status
```

Required strategy metrics:

```text
entries_evaluated{direction,result}
entries_emitted{direction}
mechanical_exit_evaluations{direction,result}
mechanical_exit_triggers{direction}
llm_vetoes{direction}
llm_early_exits{direction}
llm_reductions{direction}
```

## Required Tests

1. Completed 1h values exclude the active 1h bar.
2. Completed 5m values exclude the active 5m bar.
3. Long wall and RSI boundaries are inclusive.
4. Short wall and RSI boundaries are inclusive.
5. StochRSI crossovers match the current/previous extreme allowance.
6. Flat-range StochRSI follows the defined zero behavior.
7. Long and short stops use the prior five completed 1h bars plus/minus 2 ATR.
8. All 97 static symbols are evaluated.
9. Events route only to `bybit/fundamo` and contain no `order_type`.
10. Fixed TP is absent for analyst-managed mode.
11. Mechanical long/short exits produce trigger events with full TA inputs.
12. LLM veto produces a bounded reduced-size decision.
13. Mechanical trigger, LLM decision, and executor receipt are observable.
14. Hard stop behavior cannot be vetoed.
