# Fundamo Strategy Portfolio

## Status

Implementation specification. Detailed strategy fidelity, resampling, cadence,
and repair requirements are defined by
`specs/strategy-fidelity-repair-and-expansion-v1.md`.

## Purpose

Fundamo-routed strategy families evaluate the cutoff-bound effective watchlist
plus permanent assets and route their executor intents exclusively to the Fundamo
Bybit profile. Compact strategies retain their Hyro permanent-asset route and
additionally fan out to the Fundamo profile for every effective-universe asset.
Evaluation scope is governed by
`specs/strategy-symbol-performance-rotation-v1.md`.

The former dual-zone strategy family is retired. Its IDs must not remain enabled,
registered as live plugins, or produce events after cutover. The EMA99 retest
strategy is the current replacement for that trend slot.

## Strategy IDs

The current Fundamo-routed IDs are:

| Strategy ID | Route |
|---|---|
| `dual-zone-follower-v3` | Bybit Fundamo |
| `dual-zone-short-follower-v3` | Bybit Fundamo |
| `ema99-retest-adx-v1` | Bybit Fundamo |
| `ema20-pullback-h4-trend-v1` | Bybit Fundamo |
| `gold-trend-ema-bb-stoch-v1` | Bybit Fundamo |
| `mtf-exhaustion-reversal-v1` | Bybit Fundamo |
| `ema99-double-touch-stochrsi-state-v1` | Bybit Fundamo |
| `ema7-26-cross-hammer-shooting-star-1h-adx-v1` | Bybit Fundamo |

Compact strategy IDs are not part of this portfolio. They are hard-routed to
Bybit Hyro and restricted to the permanent assets.

## Universe

- When performance rotation is enabled, evaluate every asset in the effective
  cutoff-bound watchlist plus the permanent assets on every applicable completed
  cutoff. The watchlist is selected from the valid Bybit linear USDT-perpetual
  ticker universe and maintained by the rotation feed.
- When rotation is disabled or unavailable, evaluate the permanent-only safe
  scope.
- The rotation feed uses the equal per-side split defined in
  `specs/strategy-symbol-performance-rotation-v1.md`.
- Do not use discovery rotation, OI rotation, or a legacy fixed symbol list.
- Market data lookup uses the repository's canonical asset/native-symbol mapper.
- Execution symbol expansion remains executor/outbox-owned.

## Common plugin contract

Each plugin must:

- Read only finalized, point-in-time data bounded by its cutoff.
- Never read the current/open/future bar.
- Return zero or more alpha-event drafts through the existing alpha outbox seam.
- Emit no position sizing, leverage, quantity, or account-specific execution data.
- Include `strategy_id`, plugin version, asset, direction, observed timestamp,
  valid-until timestamp, entry, invalidation, target, setup/phase, and a complete
  feature snapshot sufficient to replay the decision.
- Use `confidence_status="uncalibrated"`; these rules do not create calibrated
  probabilities.
- Remain blind to global admission policy. In particular, strategies must not
  add an RR gate, stop-distance gate, clash rule, or score threshold to mimic
  admission.
- Remain symbol-dumb: the plugin evaluates every symbol delivered by the
  upstream subscription universe and contains no symbol or account allowlist.

Common operational defaults:

- One active signal per strategy, asset, and direction.
- No pyramiding at the strategy level.
- Signal validity: 5 minutes unless the executor contract requires a different
  explicit value.
- Take-profit mode: fixed full close.
- Downstream deployment policy may route these strategy IDs to
  `exchange_id=bybit`, `account_id=fundamo`; plugins remain account-agnostic.

## Pipeline placement

```text
WS gateway subscription universe
        |
        v
market observations and completed cutoff
        |
        v
symbol-dumb strategy plugin evaluation
        |
        v
symbol-account-strategy hard gate + global admission
        |
        v
alpha outbox
        |
        v
intent builder -> shared SQLite intent bus -> bybit/fundamo
```

Admission owns freshness, stop geometry, minimum RR, maximum stop distance,
candidate clash resolution, and selection. A strategy may emit a candidate that
admission later rejects.

## Required wiring

1. Add the new IDs to the known strategy set and plugin registry.
2. Remove all retired dual-zone IDs from enabled defaults and live registry
   wiring. Historical events remain immutable.
3. Add all new IDs to the appropriate admission/purity classification used by
   the alpha outbox.
4. Add explicit downstream routing entries for every new ID to Fundamo.
5. Apply symbol-account-strategy policy downstream; compact Hyro restrictions and
   route forcing must not be implemented inside strategy code.
6. Add configuration prefixes and documented defaults without changing global
   intent sizing ownership.
7. Preserve plugin failure isolation: one strategy or symbol failure must not
   prevent other strategy families or assets from completing.
8. Preserve durable raw-candidate and admission status recording.

## Acceptance criteria

- A complete run attempts all assets in the configured effective watchlist plus
  permanent assets when rotation is enabled, and permanent assets only when it
  is disabled or unavailable.
- No new strategy intent contains `account_id=hyro`.
- Retired dual-zone IDs cannot be enabled accidentally as live plugins.
- An admission rejection is observable and does not cause the strategy to alter
  its emitted target or stop.
- Duplicate cutoff execution produces no duplicate alpha event or intent.
- No event uses an unfinished higher-timeframe bar.
- Standalone PM behavior continues to use the originating strategy ID.
