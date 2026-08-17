# Alpha Outcome Policy

## Scope

This policy describes descriptive outcome recording for the deterministic alpha
ledger. It does not authorize execution or turn an event into a trade.

## Terminal Outcomes

- `not_triggered`: the entry condition was never met by `valid_until`.
- `target`: a configured target was reached after a trigger and before expiry.
- `invalidated`: the invalidation price was reached after a trigger and before expiry.
- `expired`: the event triggered but no target or invalidation barrier was reached
  before `valid_until`.

## Same-Bar Rule

15-minute OHLC bars do not reveal intrabar order. When a single bar could both
trigger entry and cross a target or invalidation barrier, the outcome evaluator
must record `ambiguous_same_bar` and must not label the event `target` or
`invalidated`. Descriptive return metrics may still be recorded from subsequent
completed bars, with `details.same_bar_policy = "ambiguous"`.

This conservative policy remains in force until a point-in-time lower-timeframe
source is introduced and documented.
