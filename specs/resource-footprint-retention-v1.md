# Resource Footprint And Retention v1

## 1. Status

Locked. This document records the closed resource and storage decisions for the
research analyst after the DuckDB evaluation. It does not change strategy
logic, candidate semantics, admission, or delivery contracts.

Status: locked for implementation.

Companion documents:

- `specs/hybrid-polars-shared-computation-v1.md` (computation architecture)
- `SPEC_DATABASE_RETENTION.md` (operational retention runbook)
- `docs/DESIGN.md` §12 (tiered prune)

## 2. Decision Record

Three decisions are closed:

1. **SQLite remains the online durable store.** DuckDB is rejected for the
   online path in every role:
   - as a replacement for `market.sqlite3`, `analyst.sqlite3`, or
     `regime.sqlite3`;
   - as the backfill/direct-history store (`regime_1h_bars` /
     `regime_4h_bars`).
   Rejection reasons: the cross-process single-writer/many-reader contract
   (regime worker writes while orchestrator and engine read exact-cutoff
   history) is SQLite WAL's strength and DuckDB's file model forbids
   read-while-writing across processes; the direct cache is bounded at 240
   bars/timeframe/asset and pruned with margin, so there is no growth for
   columnar storage to win against; the workload is small high-frequency
   upserts plus batched deletes, which favor row storage; and
   `hybrid-polars-shared-computation-v1.md` §4 explicitly prohibits the swap.
   The SQLite→Polars handoff through the shared computation context (and its
   bounded per-cutoff frame cache) is already the efficient path.
2. **No offline research lake.** The Parquet/DuckDB export path is not built.
   Historical depth beyond online retention is not required by any active
   consumer. If research depth is ever needed, it can be reconstructed from
   exchange sources or re-derived at that time; this decision can be reopened
   explicitly, not silently.
3. **Retention is sized to the live consumer set**, not to research
   convenience. The default TTLs in `config.py` are reduced as specified in
   §4 and are the locked defaults. `0` continues to disable a tier.

## 3. Evidence Basis

- The gateway persists only `5m` (streamed) and `15m` (derived) intervals into
  `market.sqlite3`; `30m` is never materialized there.
- The canonical strategy path returns an empty frame for `1h`/`4h` loads from
  the market database; strategy and admission HTF context comes exclusively
  from regime-owned direct history (`regime_1h_bars`/`regime_4h_bars`).
- The regime score/gate runtime consumers read only the latest row per asset
  (hysteresis); older rows serve replay/audit only.
- Direct-history seed depth is 240 bars per timeframe (14 complete 1h days,
  45 complete 4h days) with fetch margin.
- Active consumer lookbacks: strategies declare `lookback_days=20` (5m/15m),
  the gateway resample window is 1 day, trade-quality market context needs
  2 days, the default frame lookback is 16 days.

## 4. Locked Retention Defaults

Changes to `config.py` defaults (env-overridable as before):

| Setting | Old default | New default | Basis |
| --- | ---: | ---: | --- |
| `PRUNE_5M_DAYS` | 30 | **21** | retention floor = maximum declared strategy lookback (`lookback_days=20` on the vectorbt ports) + 1 day margin; no `load_bars` window may be truncated by retention (160 assets ≈ 46K rows/day; ~30% cut vs 30d) |
| `PRUNE_15M_DAYS` | 90 | **30** | covers 20d strategy lookback plus restart-rebuild margin |
| `PRUNE_1H_DAYS` | 365 | **30** | legacy derivation only; the gateway no longer derives 1h and no live consumer reads it. A short positive TTL drains residue — `0` would disable the tier and keep rows forever |
| `PRUNE_4H_DAYS` | 365 | **45** | same residue-drain rationale; 45d mirrors the 45-complete-4h-days direct-history margin |
| `REGIME_SCORE_RETENTION_DAYS` | 30 | **14** | runtime reads latest-per-asset only; 14d covers replay windows without a year-scale ledger |
| `REGIME_GATE_RETENTION_DAYS` | 90 | **30** | hysteresis reads the previous decision; 30d keeps bounded audit depth |
| `MARKET_REGIME_RETENTION_DAYS` | 365 | **30** | legacy market-DB regime rows have no active consumer |
| `MARKET_WATCHLIST_HISTORY_DAYS` | 365 | **90** | rotation analysis horizon, not evaluation input |

Unchanged (already matched live consumers):

- `ANALYST_*` family (2–30d), `MARKET_OPTION_RETENTION_DAYS` (3d),
  `MARKET_AUXILIARY_RETENTION_DAYS` (30d), `MARKET_DISCOVERY_RETENTION_DAYS`
  (90d), direct-history seed retention (3/14d minimum).

Explicit non-changes:

- Active, pending, running, retryable, delivery, and executor-handoff state
  keep their existing lifecycle guarantees; this spec changes age-based TTLs
  only.
- Direct regime history (`regime_1h_bars`/`regime_4h_bars`) keeps its
  seed-depth contract. It is not affected by market-DB interval TTLs.
- The offline compaction workflow (Sunday 04:30 UTC, stop → checkpoint →
  VACUUM → verify → restart) is the only file-reclamation path and is
  unchanged.

## 4.1 Retention-Floor Invariant

No interval TTL may be lower than the maximum lookback any live consumer can
request for that interval, plus a one-day margin. Retention must never be the
reason a requested frame is shorter than requested; strategies own their
data-readiness floors and must continue to fail closed on real data faults.

Current floors (keep synchronized with the plugin registry when strategies
change):

| Interval | Max declared lookback | Retention floor | Default |
| --- | ---: | ---: | ---: |
| 5m | 20 days (`lookback_days=20` on all 7 vectorbt ports) | 21 days | `PRUNE_5M_DAYS=21` |
| 15m | 20 days (derived from the cached 5m base, `base_needed = max(lookback, 16)`) | 21 days | `PRUNE_15M_DAYS=30` (already above floor) |
| Direct 1h/4h | seed-depth contract (240 bars, 14/45 complete days) | unchanged | governed by `DIRECT_HTF_*`, not `PRUNE_*` |

The 20-day port lookback is the only live consumer above 16 days. If a future
strategy declares a longer lookback, this spec and the affected TTL default
must move together.

## 5. Rollout

- Defaults change in code; a deployment may pin old values via env during
  transition. TTL changes are reversible without schema change.
- Retention runs on each owner's writer connection in bounded batches with
  passive checkpoints, as before. Watch the six-hour maintenance log lines and
  the weekly compaction integrity checks after rollout.
- Parity: no strategy, admission, or delivery output changes. The only
  observable differences are smaller steady-state databases and shorter
  prune batches.
