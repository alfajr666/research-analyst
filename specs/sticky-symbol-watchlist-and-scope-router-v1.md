# Sticky Symbol Watchlist and Scope Router v1

## Status

Locked design specification, agreed during the operator discussion on
2026-09-07.

This specification defines sticky rotation membership, the bounded effective
subscription universe, and evaluator-owned per-strategy scope routing. It does
not define a 5m-only migration or an upstream account-symbol policy.

## 1. Decision

The rotation feed remains the source of new symbol selections, but a selected
non-permanent symbol remains active for a configurable sliding TTL. The active
watchlist has a configurable hard cap that includes permanent symbols.

The gateway and evaluator consume the same effective universe:

```text
point-in-time performance snapshot
                |
                v
       rotation feed publisher
                |
                v
     durable sticky watchlist state
                |
                v
       capped effective universe
          /                 \
         v                   v
  ws_gateway             evaluator
  REST bootstrap         scope router
  live WebSocket         per-plugin scope
                              |
                              v
                       symbol-dumb strategies
```

The current market-data timeframes remain unchanged by this specification.
The existing 1m and 5m stream/evaluation behavior is preserved until the
separate 1m strategy audit is complete.

## 2. Objectives

- Reduce subscription churn caused by four-hour rotation changes.
- Preserve recent market history and reduce repeated REST backfills.
- Keep the market-data and evaluation universes consistent.
- Bound WebSocket, REST, SQLite, regime, and strategy resource usage.
- Keep strategies free of rotation, watchlist, account, and symbol-policy logic.
- Make scope decisions point-in-time, deterministic, and replayable.

## 3. Terminology

- **Permanent symbol**: a symbol that is always in the effective universe and
  never expires. The current set is `BTC`, `ETH`, `PAXG`, and `QQQUSDT`.
- **Rotation selection**: a symbol selected by one published performance feed
  snapshot.
- **Watchlist entry**: durable membership for a non-permanent rotation
  selection, including its selection and expiration metadata.
- **Watchlist TTL**: the duration for which a selection remains active after
  its most recent valid selection.
- **Effective universe**: the capped union of permanent symbols and active
  watchlist entries.
- **Scope router**: evaluator-owned logic that derives the assets passed to
  each plugin without changing strategy code.
- **Strategy scope**: the immutable, cutoff-bound asset list provided to one
  plugin invocation.
- **Account-symbol policy**: the separate policy that determines whether a
  strategy may create an intent for a symbol/account combination.

## 4. Locked Invariants

1. Strategies do not call the rotation publisher or inspect watchlist
   configuration.
2. Strategies do not contain symbol allowlists or account-specific filters.
3. The rotation algorithm and performance-ranking semantics do not change.
4. Permanent symbols are always retained, subject to the implementation's
   canonical symbol normalization.
5. A non-permanent selection expires only after its individual TTL elapses.
6. A valid new selection refreshes that symbol's TTL from the selection time.
7. The effective universe never exceeds the configured hard cap.
8. The cap includes permanent symbols. The configured cap must be at least the
   number of permanent symbols.
9. Watchlist membership is not extended by a stale, missing, or invalid feed.
10. Feed freshness and individual watchlist-entry expiration are separate
    concepts.
11. The gateway and evaluator use the same cutoff-bound effective universe.
12. Open-position carryover may extend gateway market-data subscription for
    lifecycle data, but it does not silently extend the normal evaluation
    universe.
13. Account-symbol admission remains a downstream hard safety gate.
14. The 1m market-data and strategy contract remains unchanged by this
    specification.
15. Binance OI rotation remains separate from this watchlist.

## 5. Watchlist Lifecycle

### 5.1 Selection and refresh

At each valid rotation-feed publication:

1. Read the point-in-time ranked rotation selections.
2. Retain all permanent symbols.
3. Add newly selected non-permanent symbols as watchlist entries.
4. Set each selected symbol's `expires_at` to `selected_at + watchlist_ttl`.
5. Refresh `last_selected_at` and the source feed identity for existing entries.
6. Remove entries whose expiration is at or before the evaluation cutoff.
7. Apply the hard cap using the deterministic priority rules below.
8. Publish the resulting effective universe and watchlist metadata as one
   versioned feed state.

The TTL is sliding. Re-selection extends membership; mere continued presence in
an old feed does not.

### 5.2 Durable entry metadata

Each non-permanent watchlist entry must carry, at minimum:

```text
asset
permanent
first_selected_at
last_selected_at
expires_at
last_feed_id
last_rank_side
last_rank
```

The feed state must also identify:

```text
watchlist_ttl_hours
watchlist_max_symbols
effective_symbol_count
watchlist_entry_count
effective_universe_version
```

### 5.3 Feed outage behavior

The last durable watchlist state may continue to supply entries while their
individual `expires_at` remains in the future. A feed outage must not silently
refresh or extend entries.

The state must expose degraded freshness, including the last valid feed ID and
the feed age. Once an entry expires, it is removed even if the rotation worker
has not recovered. If no non-permanent entries remain, the effective universe
contains permanent symbols only.

This prevents a four-hour feed freshness boundary from defeating the intended
longer watchlist TTL while still failing closed on membership age.

## 6. Cap and Eviction

The cap is a hard limit on the effective universe, including permanent symbols.

Priority is applied in this order:

1. Permanent symbols.
2. Symbols selected by the newest valid rotation feed.
3. Older, unexpired watchlist entries.

When the union exceeds the cap, older entries are evicted first. Ties are
resolved deterministically by:

1. Earliest `expires_at`.
2. Oldest `last_selected_at`.
3. Lower rotation rank, when available.
4. Canonical asset name.

An eviction does not refresh or delete the durable historical record. It removes
the entry from the effective universe and records an eviction reason. A later
selection may add it again and starts a new TTL.

If the configured rotation target cannot fit after reserving permanent symbols,
the feed must remain valid but truncate selections deterministically according
to the published ranking order. Configuration validation should warn when the
rotation target exceeds the available capped slots.

## 7. Market-Data Contract

The effective universe is passed to `ws_gateway` for both initial and dynamic
subscription reconciliation.

For every newly added symbol, the gateway performs:

```text
REST backfill of configured streamed timeframes
        -> commit through the market DB writer
        -> refresh deep-backfill readiness
        -> live WebSocket subscription
```

Existing entries that remain active are not re-backfilled on every feed update.
Expiration removes future live subscription demand but does not delete retained
market history.

The current streamed timeframes remain `1m` and `5m`. Higher timeframes continue
to use the existing local resampling and hybrid HTF contracts. This document
does not authorize changing them to 5m-only.

The market database remains owned by `ws_gateway`. The watchlist publisher does
not write market observations, and no second market database writer is added.

## 8. Evaluator Scope Router

The evaluator constructs a cutoff-bound scope before invoking plugins. The
router consumes the effective universe and the existing regime scope, then
produces one scope per plugin.

```text
effective universe
        |
        +--> regime family scope
        |
        +--> plugin metadata
        |
        v
strategy scope router
        |
        +--> plugin A: assets [...]
        +--> plugin B: assets [...]
        +--> plugin C: assets [...]
```

The router may apply regime-family routing already defined by the regime
contract. It must not embed account-symbol policy in this specification.

Each plugin invocation receives a snapshot containing its resolved strategy
scope. Existing generic helpers may read that scope, but strategy modules must
not know why an asset was included or excluded.

The scope decision must include:

```text
evaluation_cutoff
effective_universe_version
feed_id
regime_scope_id or regime cutoff
strategy_id
allowed_assets
excluded_assets with reasons
scope_contract_version
```

The router is an optimization and routing boundary. It does not create
candidates, score candidates, mutate strategy output, or replace admission.

## 9. Account-Symbol Policy Boundary

Account-symbol policy is intentionally separate and unresolved by this
specification.

The global watchlist and WebSocket universe must not be reduced merely because
one strategy routes to a particular account. A symbol may be needed by another
strategy, regime calculation, open-position lifecycle, or future consumer.

A future account-symbol scope provider may be composed into the evaluator scope
router:

```text
watchlist scope
    -> regime scope
        -> account-symbol scope provider
            -> plugin invocation
                -> downstream admission hard gate
```

Until that provider is separately specified and validated:

- account-symbol policy remains enforced during candidate admission;
- final route and proof validation remain mandatory;
- no account policy is used to prune shared WS or REST market data;
- no strategy module receives account-policy logic.

## 10. Observability

The rotation feed, gateway health, regime summary, and evaluator observability
must expose, at minimum:

- Effective universe count.
- Permanent symbol count.
- Active watchlist count.
- Watchlist TTL and cap.
- Added, expired, evicted, and re-selected symbols.
- Last valid feed ID and feed age.
- Effective universe version.
- REST backfill duration and row count for additions.
- WebSocket subscribed symbol, topic, and connection counts.
- Evaluator strategy-scope counts by plugin.
- Regime-scope counts by family.
- Scope exclusions and reasons.
- Evaluation duration and trigger lag.
- Market-writer queue depth and resampling duration.

The system must distinguish these states:

```text
ready              valid current feed and valid effective universe
degraded           stale feed, but unexpired watchlist entries remain
permanent_fallback no unexpired non-permanent entries remain
invalid            malformed or unsafe state; fail closed to permanents
```

## 11. Resource Acceptance Criteria

Before production enforcement, benchmark at the configured cap and verify:

1. WebSocket connections remain stable through feed refresh and reconnect.
2. Dynamic backfill does not create duplicate market writers or unbounded
   writer-queue growth.
3. Resampling completes within the available cadence budget.
4. Regime processing completes before the next required 5m cutoff.
5. Evaluation processing completes before its next trigger budget.
6. SQLite growth and retention remain within the approved operational budget.
7. A feed outage preserves only unexpired entries and never extends TTL.
8. Replay at the same cutoff reproduces the same effective universe and scopes.

## 12. Required Tests

- Permanent symbols never expire.
- New selections create entries with the configured TTL.
- Re-selection refreshes TTL without duplicating entries.
- Expiration occurs exactly at the cutoff boundary.
- Stale feeds do not refresh entries.
- Feed outages preserve unexpired entries and expose degraded state.
- The effective universe never exceeds the cap.
- Eviction order is deterministic.
- The newest feed selections and permanent symbols survive cap pressure.
- Gateway additions receive one REST backfill before live subscription.
- Existing retained symbols are not repeatedly backfilled.
- Expired symbols are removed from future subscriptions.
- Open-position carryover is subscription-only and does not expand evaluation
  scope.
- Gateway and evaluator receive identical effective-universe versions.
- Router output is deterministic for the same cutoff and inputs.
- Strategies remain free of rotation, watchlist, and account-symbol branches.
- Account-symbol admission still rejects invalid candidates downstream.
- 1m data and strategy behavior remain unchanged by this feature.

## 13. Explicitly Deferred

The following are not decisions in this specification:

- Rewriting 1m strategies to 5m.
- Removing 1m WebSocket ingestion.
- Disabling or rewriting strategies that currently require 1m data.
- Moving account-symbol policy from admission into the scope router.
- Changing the performance-ranking algorithm.
- Changing the rotation refresh cadence.
- Changing direct regime-history ownership or the hybrid HTF contract.
