# Fundamo Strategy Portfolio

## Status

Implementation specification. No implementation is included in this document.

## Purpose

Add three strategy families that evaluate the approved 92-symbol static universe
and route their executor intents exclusively to the Fundamo Bybit profile.

The existing `dual-zone-follower-v1` and
`dual-zone-short-follower-v1` implementations are retired. They must not remain
enabled, registered as live plugins, or produce events after cutover. The
enhanced dual-zone family replaces them.

## Strategy IDs

Use these IDs unless an implementation review explicitly changes them:

| Family | Long | Short |
|---|---|---|
| Enhanced dual-zone | `dual-zone-follower-v2` | `dual-zone-short-follower-v2` |
| EMA20 pullback H4 trend | `ema20-pullback-h4-trend-v1` | same bidirectional plugin |
| EMA stack / ADX / StochRSI | `ema-stack-15m-adx-stochrsi-5m-v1` | same bidirectional plugin |

## Universe

- Source is `config.load_static_symbols()` backed by
  `symbols/static_universe.json`.
- The list currently contains exactly 92 canonical base assets.
- Evaluate every listed asset on every applicable completed cutoff.
- Do not use discovery rotation, top-N selection, or the legacy 64-symbol
  dual-zone reference list.
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

Common operational defaults:

- One active signal per strategy, asset, and direction.
- No pyramiding at the strategy level.
- Signal validity: 5 minutes unless the executor contract requires a different
  explicit value.
- Take-profit mode: fixed full close.
- All three strategy routes: `exchange_id=bybit`, `account_id=fundamo`.

## Pipeline placement

```text
WS gateway market DB
        |
        v
completed cutoff finalization
        |
        v
strategy plugin evaluation over 92 assets
        |
        v
raw candidate capture + global admission
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
2. Remove both retired v1 dual-zone IDs from enabled defaults and live registry
   wiring. Historical events remain immutable.
3. Add all new IDs to the appropriate admission/purity classification used by
   the alpha outbox.
4. Add explicit per-strategy routing entries for every new ID to Fundamo.
5. Ensure no compact-strategy override can classify these strategies as Hyro.
6. Add configuration prefixes and documented defaults without changing global
   intent sizing ownership.
7. Preserve plugin failure isolation: one strategy or symbol failure must not
   prevent other strategy families or assets from completing.
8. Preserve durable raw-candidate and admission status recording.

## Acceptance criteria

- A complete run attempts all 92 assets for each applicable strategy.
- No new strategy intent contains `account_id=hyro`.
- Retired dual-zone v1 IDs cannot be enabled accidentally as live plugins.
- An admission rejection is observable and does not cause the strategy to alter
  its emitted target or stop.
- Duplicate cutoff execution produces no duplicate alpha event or intent.
- No event uses an unfinished higher-timeframe bar.
- Existing PM sidecar behavior continues to use the originating strategy ID.
