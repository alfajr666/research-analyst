# Regime-Session Module v1

## Status

Locked design specification, agreed during the operator discussion on
2026-09-04.

This specification defines the first hard gate in the research evaluation path.
It replaces static assumptions such as "US is trend" or "Asia is
mean-reversion" with a per-asset, point-in-time regime score.

The direct regime-history bootstrap is specified in
`specs/regime-history-bootstrap-v2.md`. The reversal-family activation gate is
specified in `specs/reversal-regime-gate-v1.md`.

Numeric defaults marked **research default** are implementation starting points.
They may be calibrated from historical data without changing the output
contract or ownership boundaries.

## 1. Core Decision

The regime-session module runs before strategy evaluation.

```text
completed 5m cutoff
        |
        v
subscription universe
        |
        v
 per-asset regime-session decision
         |
         +--> blocked: do not evaluate any strategy family for this asset
         |
         +--> ready: build per-asset family scopes
                         |
                         +--> trend assets -> trend strategies
                         +--> range assets -> mean-reversion strategies
                         +--> reversal assets -> reversal strategies
                                      |
                                      v
                             existing candidate admission
                                      |
                                      v
                             scoring, clash resolution, delivery
```

The decision is **per asset and per cutoff**. A missing or blocked SOL context
must not suppress a valid BTC context. A blocked asset receives no strategy
evaluation for that cutoff and cannot produce candidates or intents for it.
For a ready asset, strategy evaluation is additionally scoped by explicit
strategy family. A trend-family plugin may receive SOL while a mean-reversion
plugin does not; this is not an asset-wide allow/block gate.

The existing candidate-level hard admission remains in place after strategy
evaluation. The regime-session gate is additive and earlier; it does not replace
symbol/account, price, freshness, RR, ATR, or structural-stop admission.

## 2. Locked Decisions

### RSM-001: Session is not regime

UTC session and market regime are independent dimensions.

```text
session: Asia | Europe | US | rollover
regime:  trend | range | reversal | shock | unknown
```

Any session may contain any regime. The module must never contain a permanent
mapping such as `Asia -> mean_reversion` or `US -> trend`.

### RSM-002: The active universe is rotation plus permanent assets

The normal evaluation universe is:

```text
15 performance gainers
+ 15 performance losers
+ permanent assets
= 30 rotated assets + permanent assets
```

The current permanent set is owned by the rotation feed. At the time this design
was locked, the expected active count was 34. Open-position carryover may keep an
asset subscribed at the gateway for lifecycle data, but it does not silently
expand the normal candidate-evaluation universe.

The regime score is calculated independently for every asset in the active
evaluation universe. There is no BTC proxy for another asset.

### RSM-003: BTC/SPX correlation is removed

The cross-asset BTC/SPX correlation component is out of scope and must not be a
required input, readiness condition, or data-provider dependency.

The first version uses only the requested asset's own market data. A future
cross-asset feature would require a separate design decision and validation.

### RSM-004: The existing WebSocket stream is unchanged

The gateway continues to stream only the existing base timeframes:

```text
1m + 5m per subscribed asset
```

No 1h or 4h WebSocket topics are added. The gateway remains the sole writer of
`market.sqlite3` and derives 15m, 1h, and strategy-facing 4h bars locally from
completed 5m observations using the canonical resampler.

The regime-session module has separate direct Bybit REST `1h` and `4h` history
caches, owned by the regime worker and stored in `regime.sqlite3`. These caches
are not live WebSocket topics and are not replacements for strategy-facing 1h
or 4h context.

The regime module must not add exchange subscriptions, alter gateway sharding,
or create a second market-database writer.

### RSM-005: Direct higher-timeframe history is regime-exclusive

The live strategy path uses the existing Bybit market observations. A separate
live market provider is not required and must not be introduced.

The regime worker may use Bybit public REST only for its exclusive historical
`1h` and `4h` caches, as specified in `specs/regime-history-bootstrap-v2.md`. It
must not write `market.sqlite3`, provide 1m/5m strategy bars, or become a
competing live writer. The gateway continues to own 1m/5m REST bootstrap and
WebSocket data.

### RSM-006: Separate regime observation storage

Regime observations are stored outside the evaluator's analyst database:

```text
REGIME_DB_PATH=data/regime.sqlite3
```

The regime observation store has one writer: the managed regime-session worker.
The evaluator reads it read-only. This keeps regime writes, indexes, and
retention from contending with candidate, event, and delivery writes.

The market database remains gateway-owned. The regime database is not a second
general market ledger; it contains the regime-exclusive direct 1h/4h caches plus
derived score and gate observations with source cutoff references. The direct
cache schemas and retention contract are defined in
`specs/regime-history-bootstrap-v2.md`.

The existing `entry_policy_observations` table and `ENTRY_POLICY_MODE` setting
are provisional shadow-policy scaffolding. Before v1 enforcement, their session
cooldown behavior must be consolidated into this module and the canonical runtime
setting becomes `REGIME_SESSION_MODE`. There must be only one active
session/regime gate.

### RSM-007: Read and compute per asset, once per cutoff

The regime worker must process each active asset at most once per completed 5m
cutoff.

For one asset it reads:

- completed direct 1h bars from the regime cache;
- completed direct 4h bars from the regime cache;
- completed 5m bars for realized volatility.

The 1h and 4h regime views come from the direct Bybit REST caches. The 5m view
is retained for realized volatility. Strategy-facing 1h/4h views remain local
5m resamples and are not changed by this regime-only cache. The worker must
not independently reload and resample the same asset once for every strategy.

The implementation should use one per-cutoff asset cache and reuse the frames
for all regime calculations. It must not run inside each strategy plugin.

### RSM-008: Warmup is per asset

An asset is not regime-ready until it has enough completed data for all required
inputs. The production readiness requirement is 57 complete direct bars for
both the 1h and 4h ADX inputs, using the current 14-period ADX and 14-period
smoothing defaults.

The regime worker fetches 4 calendar days of direct 1h history and 15 calendar
days of direct 4h history, retaining at least 3 complete 1h days and 14
complete 4h days after boundary trimming. The gateway separately backfills the
recent 1m/5m strategy history for newly added rotated assets. That bootstrap
remains independent of regime readiness.

Insufficient warmup blocks that asset only. It does not fabricate an unknown
score and does not fall back to BTC, a global timestamp, another asset, or a
partial 5m-derived 4h score for the regime module.

## 3. Regime Score Contract

The public pure-function contract is:

```text
regime_score(current_time, market_data) -> {
  trend_weight: float [0,1],
  mean_reversion_weight: float [0,1],
  reversal_weight: float [0,1],
  confidence: float [0,1]
}
```

The persisted observation additionally includes version, asset, cutoff,
readiness, components, and gate reasons.

Weights are independent continuous scores. They do not need to sum to 1. The
consumer must not renormalize them silently.

### 3.1 Required market inputs

All inputs are for the same canonical asset and the same point-in-time cutoff.
The `adx_1h` and `adx_4h` values are computed from the regime-exclusive direct
1h and 4h caches. Realized-volatility values remain derived from canonical
completed 5m market observations. Mixed provenance is intentional and must be
recorded in the observation source references.

```text
adx_1h
adx_4h
adx_1h_previous
adx_4h_previous
realized_vol_recent
realized_vol_prior
```

The previous ADX values are required for a non-zero reversal signal. If they are
unavailable, reversal weight is zero but the trend/range score may still be
valid. The current ADX and volatility inputs are required for a ready score.

### 3.2 Trend strength and timeframe agreement

ADX is normalized to a 0 to 1 interval using the research default saturation
level of 50:

```text
normalized_adx = clamp(adx / 50, 0, 1)
trend_strength = (normalized_adx_1h + normalized_adx_4h) / 2
tf_agreement = 1 - abs(normalized_adx_1h - normalized_adx_4h)
```

The normalization level and timeframe weights are configuration, not hidden
constants. Timeframe disagreement lowers confidence and is retained as a
transition signal.

### 3.3 Volatility regime clarity

The initial research implementation compares realized volatility over two equal
completed-bar windows:

```text
vol_ratio = realized_vol_recent / realized_vol_prior
vol_regime_clarity = clamp(1 / vol_ratio, 0, 1)
```

Stable or compressing volatility does not reduce clarity. Expanding volatility
reduces clarity. A later implementation may replace this formula after
historical validation, but the input and output semantics remain unchanged.

### 3.4 Session transition discount

The initial transition center is the Europe-to-US handoff at `13:00 UTC`.
The research default transition width is `+/- 60 minutes`. Discount is
continuous, not a hard session switch, with a research default floor of `0.5`.

```text
outside transition window: 1.0
at transition center:       0.5
between center and edge:    linear interpolation
```

The transition discount affects confidence and therefore weights. It does not
pretend that the market regime changes at the clock boundary.

The session opening cooldown is configured independently as
`REGIME_SESSION_COOLDOWN_MINUTES`, with a research default of 30 minutes. It is
the only clock-based hard block in the initial gate.

### 3.5 Confidence and family weights

The first composition, without BTC/SPX correlation, is:

```text
confidence = tf_agreement
           * vol_regime_clarity
           * transition_discount

trend_weight         = confidence * trend_strength
mean_reversion_weight = confidence * (1 - trend_strength)
reversal_weight       = confidence * reversal_signal
```

The reversal signal is continuous:

```text
trend_decay = max(0, previous_trend_strength - trend_strength)
```

It becomes non-zero only when the previous trend strength is at least the
configured research default of `0.55`. Decay is normalized by the configured
research default of `0.15`.

This means a low-ADX market can produce a high mean-reversion weight. It does
not mean that a specific UTC session is mean-reversion.

### 3.6 Family activation and hysteresis

The trend and mean-reversion weights are continuous research outputs. They
become evaluator scopes through a versioned activation policy:

```text
family activation ON:  weight >= 0.35  (research default)
family activation OFF: weight <  0.25  (research default)
```

The ON threshold must be greater than or equal to the OFF threshold. A family
that was active for the immediately preceding score remains active while its
weight is at or above OFF. A family that was not active must reach ON before it
is activated. If there is no prior score, the ON threshold applies. This
prevents small score oscillations from changing the plugin scope every cutoff.

Activation is evaluated independently for each asset and family. Reversal is an
exception: its evaluator scope is controlled by the dedicated boolean gate in
`specs/reversal-regime-gate-v1.md`, not by `reversal_weight` thresholds. There
is no single regime label and no asset-level shortcut such as "trend means run
all trend-capable assets". The evaluator consumes a scope such as:

```json
{
  "family_assets": {
    "trend": ["SOL"],
    "mean_reversion": ["ETH"],
    "reversal": []
  }
}
```

The initial family mapping is explicit metadata on every registered strategy
plugin. Unknown-family plugins are not evaluated in `enforce` mode; they may
still run in `off` or `shadow` mode for observation. An asset may appear in
multiple family scopes during a transition.

## 4. Gate Contract

The gate consumes the score and session context and returns:

```json
{
  "asset": "SOL",
  "cutoff": "2026-09-04T13:05:00Z",
  "session_name": "us",
  "session_phase": "active",
  "regime_score_status": "ready",
  "decision": "allow | block",
  "reasons": [],
  "regime_score": {},
  "family_weights": {},
  "active_families": []
}
```

### 4.1 Mandatory block reasons

The gate blocks an asset before strategy evaluation when any of these apply:

- required market data is missing;
- required data is stale or not point-in-time complete;
- 1h/4h warmup is insufficient;
- the cutoff is not finalized;
- the session is inside the configured opening cooldown;
- an explicit, versioned operator policy says the current session/regime
  combination is blocked.

The initial environment compatibility matrix is empty until validated. A low
trend weight alone does not create an asset-level block; it only determines
whether trend-family plugins receive that asset. A high trend weight does not
activate mean-reversion or reversal plugins. A ready asset may have zero active
families when no family clears its activation threshold.

### 4.2 Unknown and failure behavior

The gate fails closed for the affected asset when score inputs are unavailable,
malformed, stale, or from a future cutoff.

The gate must distinguish:

```text
blocked_by_data
blocked_by_session
blocked_by_policy
```

These are not candidate-level hard-gate failures and must not be reported as
executor rejections. No candidate, alpha event, or intent is created for a
blocked asset/cutoff.

### 4.3 Existing positions

The gate controls new strategy evaluation and new entry production only.

It must not:

- close an existing position;
- reduce an existing position;
- cancel an existing order;
- modify executor protection;
- suppress PM sidecar evaluation;
- rewrite an already published candidate or intent.

Open-position lifecycle remains executor-owned.

## 5. Storage Contract

`regime.sqlite3` contains these evaluator-readable tables.

### 5.1 `regime_scores`

One immutable row per asset, cutoff, and score version.

```text
observation_id       TEXT PRIMARY KEY
asset                TEXT NOT NULL
cutoff_at            TEXT NOT NULL
rotation_feed_id     TEXT NOT NULL
score_version        TEXT NOT NULL
status               TEXT NOT NULL
trend_weight         REAL
mean_reversion_weight REAL
reversal_weight      REAL
confidence            REAL
inputs_json          TEXT NOT NULL
components_json      TEXT NOT NULL
source_observation_ids TEXT NOT NULL
recorded_at          TEXT NOT NULL
UNIQUE(asset, cutoff_at, score_version)
```

### 5.2 `regime_gate_decisions`

One immutable gate result per asset, cutoff, and gate version.

```text
decision_id          TEXT PRIMARY KEY
asset                TEXT NOT NULL
cutoff_at            TEXT NOT NULL
rotation_feed_id     TEXT NOT NULL
gate_version         TEXT NOT NULL
decision             TEXT NOT NULL
session_name         TEXT NOT NULL
session_phase        TEXT NOT NULL
reasons_json         TEXT NOT NULL
family_activation_json TEXT NOT NULL
score_observation_id TEXT
recorded_at          TEXT NOT NULL
UNIQUE(asset, cutoff_at, gate_version)
```

`family_activation_json` records the per-family activation decision and the
activation-policy version used for that cutoff. The evaluator accepts a gate
result only when asset, cutoff, rotation feed ID, and gate version match. A
stale result is equivalent to missing data and blocks that asset.

### 5.3 Direct 1h/4h regime history

The regime worker also owns the `regime_1h_bars`, `regime_1h_backfill_jobs`,
`regime_4h_bars`, and `regime_4h_backfill_jobs` tables defined in
`specs/regime-history-bootstrap-v2.md`. These tables contain only direct Bybit
REST 1h/4h history used by regime scoring. They are not a replacement for the
gateway's market observations and are not consumed by strategy plugins.

## 6. Runtime Ownership and Scheduling

```text
gateway
  -> writes completed 1m/5m observations
  -> locally derives 15m/1h/4h observations
  -> publishes completed cutoff trigger

regime-session worker
  -> reads market.sqlite3 read-only for completed 5m volatility inputs
  -> backfills and reads direct 1h/4h history in regime.sqlite3
  -> scores active rotated/permanent assets
  -> writes regime.sqlite3

orchestrator
  -> reads regime.sqlite3 read-only
  -> waits only within a bounded cutoff grace period
  -> evaluates every subscribed asset in shadow/off mode
  -> in enforce mode, evaluates each asset only for its active strategy families
  -> skips blocked assets and unknown-family plugins
```

The regime worker is a separately managed process. It must not be started
manually alongside production services and must not own `market.sqlite3`. Its
direct 1h/4h REST writes are limited to the regime-owned cache.

The orchestrator must use a bounded wait for the exact cutoff. If the regime
worker has not published a matching result before the grace period expires, the
asset is blocked rather than evaluated without the first hard gate.

The worker should compute one score batch per completed 5m cutoff, not once per
strategy. 1m and 15m evaluation paths, if enabled, consume the newest preceding
valid 5m regime observation and may not use a future observation.

### 6.1 Operational observability

Each completed batch must emit compact, machine-readable observability with:

- cutoff and rotation feed ID;
- regime session mode and asset count;
- ready/retryable counts for direct 1h and 4h history;
- score-ready and score-insufficient counts;
- gate allow/block counts and active-family counts;
- per-asset diagnostics for blocked or insufficient assets, including history
  coverage, market 5m coverage, missing score inputs, and gate reasons.

Shadow-mode `evaluated` or `assets` counts must not be labeled as history
`ready` counts. A blocked asset's direct-history state and score state must be
distinguishable in the log.

## 7. Resource Budget and Safety

For the expected 34 active assets:

```text
WebSocket topics remain:        34 x (1m + 5m) = 68
Direct 1h/4h topics added:      0
Regime score batches per 5m:    1
Market database writers:        1, the gateway
Regime database writers:        1, the regime worker
Direct 1h cache:                4 days fetched, 3 days retained
Direct 4h cache:                15 days fetched, 14 days retained
```

The regime worker must:

- read through a read-only connection;
- use a bounded per-cutoff cache;
- avoid per-strategy reloads;
- expose duration, rows read, blocked assets, and stale-result metrics;
- avoid unbounded queues or historical full-database scans in the live path;
- retain only the configured regime observation history after validation.

The first implementation must be benchmarked with 34 assets before live
enforcement. A slow regime worker must cause explicit blocked assets, not a
fallback to a second provider or an unguarded evaluator run.

## 8. Rollout Modes

The module has three modes:

```text
off
shadow
enforce
```

The runtime configuration key is `REGIME_SESSION_MODE`.

### `off`

No regime observation is required for evaluation. This is the rollback mode.

### `shadow`

The worker writes score and gate observations. The evaluator continues to run,
but every asset records whether the first hard gate would have allowed or
blocked it. Shadow mode is the default during parameter validation.

### `enforce`

The evaluator requires a matching gate result before running strategies for an
asset. Blocked assets are skipped before plugin invocation. Ready assets are
passed to plugins only when that plugin's explicit family is active for the
asset. This scope is per asset and per strategy, not one global asset list.

The old post-evaluation session annotation must not remain a second independent
cooldown gate. Session cooldown and environment policy belong to this gate, with
one versioned decision and one set of reasons.

## 9. Validation Requirements

Before enabling `enforce`, produce an out-of-sample report grouped by:

- asset;
- rotation cohort and feed version;
- UTC session;
- market regime;
- strategy family;
- gate decision;
- candidate outcome in normalized `R`;
- realized execution outcome where attribution is reliable.

Required checks:

- every active rotated asset has sufficient 1h and 4h warmup;
- no future bars enter score inputs;
- session labels do not imply regime labels;
- blocked assets produce zero strategy evaluations;
- inactive families produce zero evaluations for that asset;
- active families retain existing candidate admission behavior;
- threshold hysteresis does not flap family activation around the boundary;
- regime worker failure blocks only affected assets;
- no additional WebSocket topics are created;
- the evaluator's cutoff latency and CPU remain within baseline tolerance;
- score distributions are stable across multiple rotation periods.
- direct 1h and 4h history have no future, duplicate, or gapped bars;
- direct 1h and 4h values have documented parity diagnostics against 5m resamples;
- newly rotated assets do not enter enforcement before direct 1h/4h readiness;
- live 1m/5m bootstrap remains separate from regime 1h/4h readiness.

The score is not approved for live sizing until candidate-level outcome data is
populated and the score has been evaluated out of sample. Realized executor PnL
and candidate market outcomes must remain separate measurements.

## 10. Rollback

Rollback is a configuration change to `REGIME_SESSION_MODE=off`. Rollback must:

- stop requiring regime observations for evaluation;
- leave existing positions untouched;
- leave the gateway's 1m/5m subscriptions unchanged;
- leave historical regime observations intact for audit;
- not delete or rewrite candidates, intents, or execution receipts.

If the regime worker is unhealthy in `enforce` mode, the safe behavior is to
block new evaluation for assets without a matching observation. The operator may
then switch to `off` after checking the service health and cutoff audit.

## 11. Explicit Non-Goals

- No direct 1h/4h WebSocket subscriptions; direct 1h/4h REST history is allowed
  only for the regime-exclusive cache.
- No BTC/SPX correlation provider.
- No second live market-data writer.
- No static session-to-regime mapping.
- No strategy-family hard switch based only on session.
- No position flattening or PM-sidecar changes.
- No strategy-owned sizing or leverage logic.
- No live enforcement before out-of-sample validation.
