# Dual-Zone Follower v3

## Status

Current-design implementation specification. This revives the dual-zone setup
under new IDs; the retired v2 IDs remain historical metadata only.

## Strategy IDs

- Long: `dual-zone-follower-v3`
- Short: `dual-zone-short-follower-v3`
- Cadence: completed `5m`
- Family: `trend`
- Route: Bybit Fundamo

## Data Contract

- Execution bars are completed canonical `5m` observations bounded by the exact
  evaluation cutoff.
- ADX and DI are computed from the direct regime-owned `1h` frame through
  `load_bars_for_interval`; no canonical higher-timeframe fallback is allowed.
- The plugin is source-blind and does not read regime state, account policy, or
  structural zones.
- The engine attaches direct-HTF provenance and structural context after plugin
  evaluation. The strategy must not construct either record itself.

## Signal Rules

- Long regime: close, EMA26, and EMA99 are ordered above EMA99 with close above
  both EMAs. Short uses the mirrored ordering.
- Zone A is a pullback within `1.0%` of EMA26. Zone B is a pullback within
  `1.5%` of EMA99.
- ADX14 must be at least `22`; DI direction must agree with the candidate.
- Zone A uses a `3.0%` target and `1.0%` stop buffer from its anchor. Zone B
  uses a `5.0%` target and `1.0%` stop buffer. Long and short geometry is
  mirrored.

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
