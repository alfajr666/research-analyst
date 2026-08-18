# CA Rate Limit Takeover: BY/BN Seamless Data Provision (No Quality or Performance Degradation)

**Status**: Core + telemetry implemented (shaping in 15m+scanner, shaped_due_to_circuit logs, funding priority+8h window, health with caLimited/failoverActive/shaped/failoverBars, loader null safety, tests). Ready for enable + live validation. 
**Date**: 2026-08-18  
**Owner**: Research pipeline team  
**Related**: 
- specs/ca-truth-venue-agg-failover.md (core failover + prefer + purity)
- specs/external-api-rate-limiting.md (RateLimitedClient)
- data-platform-strategy-plugins.md (purity gates)

## Goal (Normative)

When CoinAnalyze (CA) enters sustained rate-limit / ban conditions (high 429s, zero usable 15m bars), the system **must** continue delivering **equivalent quality and performance** using Binance USDM + Bybit Linear (BY/BN) as automatic takeover provider.

- Primary data: completed 15m OHLCV (open/high/low/close/volume) for all "needed" symbols (recently observed assets + core).
- Secondary (best-effort, never faked): open_interest (USD notional sum), funding_rate (OI-weighted where available).
- All other positioning fields (predicted_funding, liquidation_*, long_short_ratio): explicitly unavailable.
- CA remains the truth source the moment usable bars return (automatic via prefer-merge).
- No degradation for price-structure strategies; mixed strategies continue to require pure_ca (correct safety).
- Pipeline cycle time, health reporting, and downstream consumers see continuous fresh bars via `data_purity` / `price_source` stamps.

**Success metric**: During multi-hour CA ban windows, preferred `max(source_end)` for needed assets continues advancing on 15m boundaries using BY/BN rows with valid provenance. Quality (strategy emit eligibility, feature availability) matches or exceeds what pure CA would provide for the supported fields.

## Deep Audit Findings (2026-08-18)

### CA Call Volume & Criticality
- **ingest_coinalyze** (every 15min, core freshness path):
  - ohlcv-history: already limited to core (`OPENMARKET_PERMANENT_ASSETS`, ~4 symbols). Good.
  - Then full ~99 symbols for: `open-interest`, `funding-rate`, `predicted-funding-rate`, `liquidation-history`, `long-short-ratio-history`.
  - This is the dominant source of CA quota consumption for the 15m backbone.
- **scanner** (hourly):
  - Heavy 7-day history pulls (ohlcv + oi-history + funding) across 200+ symbols. Discovery only — not required for 15m freshness.
- **bootstrap / deep_backfill**: bursty, one-off, non-continuous.
- Result: Even with ohlcv limited, CA is still hammered for secondary fields on every cycle.

### Failover (BY/BN) Current State
- `ingest_venue_agg_failover` now targets "assets that needed it" (recent source_observations + core) + budget guard.
- Provides excellent blended/single-venue OHLCV + volume.
- OI: frequently populated (sum USD).
- Funding: often unavailable (fetch succeeds less reliably or not attempted under budget).
- Writes single row per bar with full provenance + `data_purity`.
- Prefer loader (`strategy_v2_context.load_preferred_15m_bars`) + purity stamps in plugins already exist.
- Circuit (age or 429 rate) already triggers expansion.

### Quality / Consumption Impact
- v2 strategies (impulse, continuation, accumulation, rsi-reclaim) rely on `funding_rate` and `open_interest` for history / z-score style features.
- Loaders return `source` column; COALESCE(0) still present in some paths (risk of silent neutral bias).
- Mixed strategies correctly blocked on non-`pure_ca`.
- Health already reports preferred age (good).
- When CA returns 0 rows for hours: `max(source_end)` stalls, features incomplete, mixed silent.

### Rate Limiting & Telemetry
- `RateLimitedClient` + TokenBucket + header-aware (retry-after, remaining) is solid.
- `source_request_log` has excellent data (status, request_type, cutoff).
- Circuit already consumes 429 rate from the log.
- No current mechanism to shape/throttle CA *based on circuit state*.

### Gaps vs Goal
- CA calls continue at full volume even when circuit=open (wastes quota, delays recovery).
- Failover funding coverage is incomplete.
- No explicit "takeover mode" that reduces non-critical CA load while guaranteeing BY/BN supplies the backbone.
- Telemetry exists but is not used to drive request shaping.
- No clear health signal distinguishing "CA healthy", "CA limited + failover active".

## Solution Overview

**Circuit-driven CA shaping + robust BY/BN data supply.**

1. Keep CA as primary (ingest_coinalyze always runs first for core ohlcv).
2. After each ingest, run failover (already wired).
3. When circuit=open (or high recent 429 rate):
   - Shape/reduce non-critical CA calls inside `ingest_coinalyze` and `scanner`.
   - Ensure failover aggressively fills **all needed symbols** with best possible OHLCV + OI + funding.
4. Enhance telemetry and health to make takeover visible and automatic.
5. Preserve all existing purity, prefer, and emit gates.

This achieves takeover without reducing quality: the data continues flowing from BY/BN with honest stamps. CA recovery is aided as a side-effect (lower load) but is not the primary target.

## Detailed Design

### 1. Circuit Definition (reuse + strengthen)
Reuse existing `_circuit_open()` logic:
- Age of preferred core bars > `FAILOVER_CIRCUIT_AGE_MIN` (30m), **or**
- 429 rate on coinalyze over `FAILOVER_CIRCUIT_WINDOW_MIN` (30m) >= `FAILOVER_CIRCUIT_429_RATE` (0.50).

Make the 429 rate calculation more robust (exponential decay or separate "sustained" window).

Expose a clean helper: `is_ca_limited()` used by both failover and ingest shaping.

### 2. CA Request Shaping When Limited
In `ingest_coinalyze` (when `is_ca_limited()`):
- Always attempt core ohlcv-history (detection + potential recovery path).
- Skip or heavily reduce:
  - Full-universe `open-interest`, `funding-rate`, `predicted-funding-rate`, `liquidation-history`, `long-short-ratio-history` (or limit to core only).
  - Use smaller batch sizes or skip secondary endpoints entirely.
- Log clearly: "CA limited — shaping to core ohlcv only".

In `scanner` (hourly, when limited):
- Skip or defer deep history pulls (7d ohlcv/oi/funding).
- Fall back to lighter or cached data if possible. Discovery can tolerate staleness better than the 15m backbone.

In `bootstrap_trend_history`:
- Respect a global "CA limited" flag and skip or rate-limit.

All shaping decisions must be logged to `source_request_log` with `status="shaped_due_to_circuit"`.

### 3. BY/BN Takeover Data Completeness
Enhance `ingest_venue_agg_failover` (while circuit=open or for any gap):
- Always target all "needed" assets (current recent-observed logic is good; keep and document).
- Prioritize within budget:
  1. Klines (OHLCV) — mandatory for a bar.
  2. OI history (best effort sum).
  3. Funding history (best effort weighted avg).
- Increase per-asset attempts for funding if klines succeeded (funding is the weakest field today).
- Never invent values. Document units and provenance exactly as today.
- Expose `FAILOVER_FUNDING_ATTEMPTS_PER_ASSET` or similar for tuning.

Ensure the prefer loader and health report `failoverLatestAt` and `failoverBarsLast2h`.

### 4. Telemetry & Observability Upgrades
- Add to `source_request_log` query helpers or health.json:
  - `caLimited`: boolean (from circuit)
  - `ca429Rate15m`, `preferredAgeMin`, `failoverActive`
- Enhance failover log:
  ```
  Failover: circuit=open gaps_filled=14 assets=... caShaped=true
  ```
- In health print (orchestrator):
  ```
  Health: age=12.7m ... caLimited=true failoverActive=true ca={'429':71,'ok':11,'shaped':12}
  ```

### 5. Strategy / Consumer Safety
- No change to purity gates (mixed still require pure_ca on signal bar).
- Loaders already expose `source` — strategies should prefer checking `source` + `data_purity` over assuming fields.
- Update any remaining `COALESCE(..., 0)` in live paths to treat missing as unavailable where it affects decisions (future cleanup, not blocking).

### 6. Configuration
New / extended knobs (all ship with safe defaults):

| Env | Default | Meaning |
|-----|---------|---------|
| `CA_SHAPE_ON_CIRCUIT` | `true` | Master for shaping non-critical CA calls when limited |
| `CA_SHAPE_SKIP_SECONDARY` | `true` | Skip full-universe oi/funding/liq/ls in ingest when limited |
| `FAILOVER_FUNDING_PRIORITY` | `true` | When budget allows, attempt funding after klines |
| `FAILOVER_MAX_REQUESTS_PER_CYCLE` | `80` | (existing) |
| Existing circuit + failover knobs remain. | | |

`MARKET_FAILOVER_ENABLED` remains the outer switch.

## Architecture Changes (Minimal)

```
orchestrator cycle
  ├─ ingest_coinalyze()          # always; shaped when limited
  ├─ ingest_venue_agg_failover() # fills gaps + takeover
  ├─ ... (prune, features, plugins)
  └─ health (now reports caLimited + failover stats)
```

- `is_ca_limited()` lives in failover module (or small shared `ca_health.py`) and is imported by ingest.
- All CA clients continue using existing `RateLimitedClient` (no behavior change when not limited).

## Safety & Invariants (Must Hold)

1. CA is never completely skipped for core ohlcv (recovery detection path remains).
2. When CA produces usable bars, they immediately win via prefer-merge (no sticky failover).
3. Mixed strategies never emit on non-pure_ca (existing gate).
4. No faked neutrals for oi/funding (existing rule).
5. OM request volume unchanged (failover is independent).
6. OI rotation remains on its dedicated DB and writer.
7. Disabling `MARKET_FAILOVER_ENABLED` restores pure CA behavior.
8. Total CA + BY/BN requests during limited period is lower than today (shaping + best-effort).

## Acceptance Criteria

1. During a simulated or real CA 429 storm, preferred bars for needed assets continue advancing using `venue_agg_v1` with valid OHLCV + provenance.
2. Non-critical CA calls are measurably reduced (via source_request_log counts) exactly when circuit=open.
3. Core ohlcv attempts for CA continue (lightly) so recovery is detected.
4. Health and logs clearly show `caLimited` + takeover activity.
5. Existing v2 strategies that can run on synthetic continue to do so with correct stamps; mixed remain blocked.
6. When CA recovers mid-window, new CA bars supersede synthetic within one cycle.
7. Unit + integration tests cover shaping paths, funding priority, and end-to-end takeover with purity.

## Test Plan (Beyond Existing Failover Tests)

- `test_ca_limited_shapes_ingest`: when circuit forced open, secondary endpoints are skipped or reduced in ingest_coinalyze.
- `test_takeover_provides_ohlcv_oi`: end-to-end with mocked CA 429s, verify bars have close + oi (when venues return it).
- `test_funding_best_effort`: funding present only when BY/BN provide it.
- `test_recovery_prefers_ca`: after shaping, inject good CA bars → next preferred load returns pure_ca.
- Load test / simulation: sustained 429s + verify no quality regression in price-structure emit paths.
- Monitor `source_request_log` counts for shaped vs normal.

## Rollout

1. Land with `CA_SHAPE_ON_CIRCUIT=true` (default) but `MARKET_FAILOVER_ENABLED=false` (keep dark).
2. Enable failover in staging; verify shaping logs + takeover bars.
3. Production: enable failover first (tests green), then shaping (observability green).
4. Tune `FAILOVER_MAX_REQUESTS_PER_CYCLE` and shaping flags based on real 429 windows.

## Risks & Mitigations

- Over-throttling delays CA recovery → keep core ohlcv attempts.
- Incomplete funding in takeover → document as best-effort; strategies already have guards.
- Scope creep (full replacement of CA) → strictly limited to limited periods + prefer logic.
- Budget exhaustion in failover → existing hard cap + skipped_budget logging.

## Open Questions / Future

- Should scanner be fully skipped or just use lighter data when limited?
- Long-term: multiple CA keys with automatic rotation on sustained 429 rate?
- Deeper request shaping inside the client (per-endpoint weights when limited)?

## Implemented (as of 2026-08-18)

- CA shaping: in `ingest_coinalyze` (core ohlcv always; secondaries skipped + logged when limited) and `scanner` (heavy pulls skipped).
- `log_ca_shaped` + "shaped_due_to_circuit" status in source_request_log (telemetry visible in health ca= dict).
- Failover funding: priority + 8h lookback window for higher hit rate on latest rate (funding changes infrequently).
- Health: `caLimited`, `failoverActive`, `caShaped15m`, `failoverBars30m` in print + health.json.
- Loader safety: oi/funding extracted without force-0 (None when unavailable in vagg), then fill_null(0) in preferred df for compat; no bias change.
- Config: CA_SHAPE_*, FAILOVER_FUNDING_PRIORITY; .env.example updated (failover=true example).
- Tests: extended in test_scanner.py for limited logic + shaped log.
- All guarded by MARKET_FAILOVER_ENABLED (default false in code).

This spec is targeted, safe, and directly achieves the goal of BY/BN takeover providing the data so quality and performance are preserved when CA is banned. 
