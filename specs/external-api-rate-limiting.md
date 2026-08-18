# External API Rate Limiting, Budgeting, and Client Strategy

## Status
Spec created 2026-08-18. Core rate limiting + OpenMarket real integration complete (correct /v1/points schema with exchange/rawSymbol/coin + interval/from/period; live "ok" responses on free tier for supported assets; CoinAnalyze seeing real 429s + graceful unavailable). All legacy sleeps/wrappers removed. Tests green. Live verification: OM returns real volume profiles/agg trades; coinalyze 429s logged and bucket respected.

This is the single source of truth. Implementation must follow this document.

## Implementation Progress
- [x] specs/external-api-rate-limiting.md
- [x] api_clients/base.py (TokenBucket + RateLimitedClient + interrupt hardening + always-log)
- [x] api_clients/coinalyze.py (full)
- [x] api_clients/openmarket.py (real impl + correct params + response parsing for series shape + _to_symbol_key)
- [x] openmarket_adapter.py shim
- [x] ingest_coinalyze.py / scanner.py / bootstrap_trend_history.py refactored to clients
- [x] orchestrator.py: cutoff mat + OM wire (now multi-asset from OPENMARKET_PERMANENT_ASSETS) + hardening
- [x] config.py + .env.example rate/budget/OM key/venue/permanent
- [x] test_load_rate_limiting.py + test_*_openmarket + unit tests
- [x] source_request_log for every call (429/ok/404/timeout/unavailable)
- [x] structure_zones + real FVG/OB/VP from source_observations; no dirty evals
- [x] Legacy wrappers, sleeps, accumulation/ignition files removed
- [x] Live rate verification (coinalyze ~57% 429 under load, OM ok when params match)

Next steps: longer prod watch to observe OM budget/weight in source_request_log, tune OPENMARKET weights vs daily 1000, expand to use OM series in plugins or zones when stable, monitor 429 rate for 24h+.

## Problem Statement
The system makes frequent calls to CoinAnalyze (open-interest, funding-rate, ohlcv-history, liquidation-history, long-short-ratio-history, predicted-funding-rate, etc.) and will soon call OpenMarket.

Current state (post-implementation):
- Proactive TokenBucket + header-driven 429 handling in RateLimitedClient for CoinAnalyze + OpenMarket.
- All calls go through api_clients/* ; no scattered time.sleep(2) or global RateLimiter.
- Every request (success/429/timeout/error) logged to source_request_log with status, weight, meta.
- CoinAnalyze core always returns partial data or [] on problems (never blocks pipeline).
- OpenMarket returns {"status":"unavailable"} on any error/budget/disable and never blocks.
- Old wrappers kept only internally in ingest for thin compat; direct client used in scanner/bootstrap/orchestrator.
- 26+ core tests green (rate + drop + outcome + signal paths).

This approach does not scale when we add OpenMarket (weight-based quotas) or additional providers.

## Goals
- **Proactive respect for limits**: Stay under quotas instead of reacting to 429s.
- **Unified, observable client layer**: One place for auth, headers, retries, logging, and metrics.
- **Budgeting for free tiers**: OpenMarket Free (1,000 weight/day, 10/min) + CoinAnalyze free tier must be explicitly allocated and tracked.
- **Graceful degradation**: Optional enrichment (OpenMarket) must return `unavailable` on any rate-limit, timeout, or budget exhaustion and **never block** the CoinAnalyze backbone.
- **High observability**: Every external request logged to `source_request_log` (already used for OpenMarket). Track 429 count, backoff time, weight consumed, latency.
- **Efficiency**: Batching + controlled concurrency + header-driven backoff + minimal requests (delta fetches where possible).
- **Configurability and testability**: Limits, budgets, deadlines driven by env + easy to mock.
- **Professional production patterns**: Token-bucket or equivalent, jitter, circuit-breaker elements, per-endpoint accounting.

## Locked Product Decisions

### Sources and their characteristics

| Source       | Type          | Quota Model                  | Phase-1 Role                          | Must never block core? |
|--------------|---------------|------------------------------|---------------------------------------|------------------------|
| CoinAnalyze  | Broad         | (undocumented free tier)     | Primary 15m OHLCV, OI, funding, liq, discovery | No (it *is* the core) |
| OpenMarket Free | Enrichment | 1,000 weight/day, 10/min    | Selected-universe Bybit Perp HTF profile + 15m flow | Yes |

CoinAnalyze remains the durable broad-universe source. OpenMarket is strictly optional enrichment for a bounded selected universe (see `data-platform-strategy-plugins.md` for permanent + rotating candidates).

### Request classification & budgets

**CoinAnalyze (core, no hard daily cap observed but still rate-limited)**:
- All calls go through a single adaptive client.
- Goal: minimize 429s to < 1% of requests in steady state.
- Use per-endpoint logical cost (e.g. full history > current snapshot).

**OpenMarket (optional, hard budget)**:
- Allocation (aligned with existing stub + data-platform spec):
  1. Permanent assets (BTC,ETH,SOL,PAXG,XAUT): 7d HTF profile every 4h.
  2. Permanent + selected: 15m flow at event-time only.
  3. Remaining budget reserved for ranked-candidate event-time checks.
- Every call records `weight`, `budget_remaining` in `source_request_log`.
- On any 429 / timeout / budget_exceeded → return `{"status": "unavailable"}` for the asset(s).

### Unavailable semantics (must be respected everywhere)
- `unavailable` is a first-class value in `feature_snapshots`, plugin events, and Telegram output.
- It never prevents cutoff finalization, plugin execution, or event emission.
- Publisher already omits `unavailable` items from Context (see `signal_publisher.py`).

### Observability table
All external calls (CoinAnalyze + OpenMarket) must write to `source_request_log`:
- `source`: "coinalyze" | "openmarket"
- `request_type`: endpoint or "htf_profile"/"15m_flow"
- `weight`: estimated or actual
- `budget_remaining`: for OpenMarket (null for CoinAnalyze)
- `status`: "ok" | "429" | "timeout" | "budget_exceeded" | "skipped" | "unavailable"
- `response_meta_json`: includes headers, retry_after, etc.

### Technical constraints
- Use `httpx` (already a dependency).
- One shared `httpx.Client` (or `AsyncClient`) per source with connection pooling.
- No global `time.sleep` scattered in business logic.
- All rate-limit decisions inside the client layer.
- Support both sync (current orchestrator) and future async paths.
- Deadlines for OpenMarket must be enforced client-side (hard timeout + return unavailable).

## Architecture

### New module layout
```
api_clients/
    __init__.py
    base.py                 # RateLimitedClient, TokenBucket, exceptions
    coinalyze.py            # CoinAnalyzeClient
    openmarket.py           # OpenMarketClient (real implementation)
    __tests__/
```

### Core abstractions
1. `TokenBucket` (or equivalent) per source or per-endpoint.
2. `RateLimitedClient` base class that:
   - Acquires tokens before request
   - Executes with timeout
   - On 429: read `Retry-After` (or default), update bucket, raise or return typed error
   - Always logs to `source_request_log`
   - Returns structured result + metadata
3. Specific clients implement endpoint helpers that return clean dicts (or `unavailable` marker).

### Integration points (must be updated)
- `ingest_coinalyze.py` → use `CoinAnalyzeClient`
- `scanner.py` → use `CoinAnalyzeClient`
- `bootstrap_trend_history.py` → use `CoinAnalyzeClient` (history endpoints)
- `openmarket_adapter.py` → replace stub with `OpenMarketClient`
- `orchestrator.py` → pass `cutoff_id`, call OpenMarket only when budget allows
- `source_request_log` schema already sufficient; may need minor index for query perf

### Configuration (new env vars in config.py)
```env
COINANALYZE_RPS=0.08                    # requests per second target
COINANALYZE_MAX_CONCURRENT=5
COINANALYZE_DEFAULT_RETRY_AFTER=5
OPENMARKET_WEIGHT_BUDGET_PER_CUTOFF=... # derived from daily cap
```

All values overridable; defaults chosen to stay well under observed limits.

## Error & Fallback Matrix

| Situation                  | CoinAnalyze (core)          | OpenMarket (optional)          |
|----------------------------|-----------------------------|--------------------------------|
| 429                        | Backoff + retry limited times | Log + return unavailable      |
| Timeout / deadline         | Retry with jitter           | Return unavailable immediately|
| Budget exhausted           | N/A                         | Return unavailable            |
| Auth / 401/403             | Fatal (log + skip ingest)   | Return unavailable            |
| 5xx                        | Retry with backoff          | Return unavailable            |
| Network error              | Retry                       | Return unavailable            |

Core path must still complete even if CoinAnalyze has partial failures (use whatever data arrived).

## Testing Strategy
- Unit tests for `TokenBucket`, header parsing, unavailable paths.
- Integration tests that mock `httpx` responses with 429 + `Retry-After` headers.
- E2E test that exercises a full cutoff with OpenMarket budget exhaustion → `unavailable` in feature snapshot.
- Property test: after N rate-limited calls, budget accounting is consistent.
- Load test script (separate) that simulates sustained load without exceeding limits.

## Rollout & Cutover
1. Implement client layer behind feature flags.
2. Wire CoinAnalyze first (keep old code as fallback temporarily).
3. Wire OpenMarket.
4. Remove old manual sleeps and global `_rl` once tests pass.
5. Update all call sites and docs.
6. Monitor `source_request_log` for 429 rate and budget consumption for 48h.

## Open Questions (to be locked)
- Exact per-endpoint weight costs for CoinAnalyze (discover via headers or docs).
- Whether to add a small persistent cache for recent snapshots to reduce calls.
- Async migration timeline (orchestrator is currently sync).

This spec deliberately separates "rate limiting as infrastructure" from strategy logic. All future data sources must go through the same client pattern.
