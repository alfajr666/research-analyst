# Dual-Zone Follower v3

## Status

Current-design implementation specification. This revives the dual-zone setup
under new IDs; the retired v2 IDs remain historical metadata only.

## Strategy IDs

- Long: `dual-zone-follower-v3`
- Short: `dual-zone-short-follower-v3`
- Cadence: completed `5m`
- Execution frame: completed `5m`
- EMA frame: completed `15m`
- Family: `trend`
- Route: Bybit Fundamo

## Data Contract

- Execution bars are completed canonical `5m` observations bounded by the exact
  evaluation cutoff. They provide the candidate close, observation timestamp,
  entry price, and five-minute validity window.
- EMA7, EMA26, and EMA99 are computed from completed canonical `15m` bars
  bounded by the same exact evaluation cutoff. The 15m EMA frame supplies
  trend, channel, stop-anchor, and target-anchor values only; it does not
  change the five-minute execution cadence.
- ADX and DI are computed from the direct regime-owned `1h` frame through
  `load_bars_for_interval`; no canonical higher-timeframe fallback is allowed.
- The plugin is source-blind and does not read regime state, account policy, or
  structural zones.
- The engine attaches direct-HTF provenance and structural context after plugin
  evaluation. The strategy must not construct either record itself.

## Signal Rules

- Long regime: the 5m close is above the 15m EMA26 and EMA99, with EMA26 above
  EMA99. Short uses the mirrored ordering.
- Zone A is a pullback within `1.0%` of 15m EMA26. Zone B is a pullback within
  `0.25%` of 15m EMA99.
- ADX14 must be at least `22`; DI direction must agree with the candidate.
- Zone A uses a `3.0%` target from 15m EMA7 and a `1.0%` stop buffer from its
  15m EMA26 anchor. Zone B uses a `5.0%` target from 15m EMA7 and a `1.0%`
  stop buffer from its 15m EMA99 anchor. Long and short geometry is mirrored.

## Admission And Delivery

- Every draft enters the normal raw-signal and deterministic admission pipeline.
- Structural zone admission, ATR bounds, freshness, expiry, account policy,
  clash resolution, and admission proof remain authoritative.
- Only admitted events can reach the shared SQLite intent bus. The strategy does
  not write filesystem intent files or executor state.

## Compatibility Checks

- No `dual-zone-follower-v2` or `dual-zone-short-follower-v2` event is produced.
- Missing or invalid direct `1h` data fails closed.
- Forming, future, duplicate, malformed, or cutoff-mismatched bars fail closed
  through the shared direct-history contract.
