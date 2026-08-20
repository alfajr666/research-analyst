# Binance OI rotation — retention, prune, and static membership

| Field | Value |
| --- | --- |
| Status | **Implemented** |
| Date | 2026-08-20 |
| Parent | [`binance-oi-rotation-scanner.md`](./binance-oi-rotation-scanner.md) |
| Related | NT [`ADR-013`](../../nautilus-trading-os/specs/ADR-013-oi-rotating-static-subtract-and-ttl.md); NT [`IMPL-013`](../../nautilus-trading-os/specs/IMPL-013-oi-rotating-static-subtract-and-ttl.md); [`binance-oi-rotation-10m-fast-path.md`](./binance-oi-rotation-10m-fast-path.md) |
| Owner process | PM2 `binance-oi-rotation-scanner` (`binance_oi_rotation_worker.py`) |
| DB | `data/binance_oi.db` (`BINANCE_OI_DB_PATH`) |

## 1. Problem

The scanner already:

- Soft-expires **feed** candidates (`BINANCE_OI_ROTATION_FEED_EXPIRY_HOURS`, default 6h).
- Soft-expires **watchlist membership** (`BINANCE_OI_ROTATION_WATCHLIST_HOURS`, default 36h) via `expires_at` and `state=expired` rows.

It does **not**:

- `DELETE` aged rows from observations / raw_oi / events / history → **DB grows without bound**.
- Prefer novel assets in the membership set used by NT `data-oi` (static majors re-enter forever and dilute consumer `ROTATING_LIMIT`).

NT ADR-013 fixes consumer static-subtract. This spec owns **RA-side TTL hard prune** and optional **static membership skip** so the OI DB cannot grow forever and the active set stays discovery-shaped.

## 2. Decisions

### 2.1 Retention is distinct by object (normative)

| Object | Soft TTL | Hard prune default | Notes |
| --- | --- | --- | --- |
| Feed JSON | 6h | atomic overwrite | existing |
| Membership `entered`/`active` | 36h after last qualify | mark `expired` when due | existing |
| `binance_oi_rotation_watchlist_history` | — | **14 days** by `observed_at` | includes expired rows |
| `binance_oi_rotation_observations` | — | **30 days** by interval/observed | research snapshot |
| `binance_oi_rotation_raw_oi_history` | — | **30 days** | largest table |
| `binance_oi_rotation_events` | — | **90 days** | qualify ledger |
| `binance_oi_rotation_scans` | — | **30 days** | idempotency window |
| `discord_oi_deliveries` | — | **30 days** | notify audit |

Defaults are configuration, not strategy logic. All day counts are env-overridable (see §4).

### 2.2 Prune job ownership

- Runs inside **`binance_oi_rotation_worker`** (or a function it calls), not NT `data-oi`, not stopped orchestrator.
- Cadence: at least once per hour (e.g. after `run_due_scan`, or on a wall-clock gate if no scan due).
- Fail-open: log error, do not crash the scan loop.
- Single RW connection; one transaction per prune cycle preferred.
- Metric log line: `[oi-prune] observations=-N raw_oi=-N watchlist_hist=-N events=-N scans=-N db_mb=…`

### 2.3 Prune safety

- Never delete rows with timestamps inside the soft membership window if that would break “current active” reads — use retention **≥** `WATCHLIST_HOURS` (14d ≫ 36h ✓).
- Prefer `DELETE WHERE <ts> < now() - interval` indexed/filterable columns already present.
- Do not `VACUUM` every cycle (optional weekly / manual ops).
- Do not touch main RA `market_data` DB; only `BINANCE_OI_DB_PATH`.

### 2.4 Static membership skip (P1)

Goal: static forever-covered bases should not inflate `watchlist_history` active set.

| On qualify of asset in static seed | Persist? |
| --- | --- |
| `observations` (universe snapshot) | **Yes** (always) |
| `events` (qualified rotation event) | **Yes** (research) |
| Feed candidate list | **Yes** if ranked (consumers may ignore) |
| `watchlist_history` `entered`/`active` | **No** (P1) — or state excluded from active query |

Static seed resolution order:

1. `BINANCE_OI_STATIC_SEED_PATH` JSON array of base assets, if set.
2. Else best-effort Propr `TRADEABLE_ASSETS` where `type == CRYPTO` (optional import).
3. Else empty → feature no-ops (membership behavior unchanged).

NT consumer **must still static-subtract** (ADR-013 P0a) even if P1 is off — defense in depth.

### 2.5 What stays unchanged

- Full-universe scan eligibility and qualify gates.
- 1h full + 15m liquid cadences.
- Atomic feed publish contract (`schema_version`, `expires_at`, candidates).
- Event identity / `ON CONFLICT DO NOTHING` dedupe within interval.
- Deep-backfill enqueue behavior for true new non-overlap entries (when membership is written).
- Scanner is not a trade signal.

## 3. Consumer contract (NT)

Authoritative NT behavior: **ADR-013**.

Summary for RA authors:

```text
data-oi loads active membership (expires_at > now)
  → drops bases in CRYPTO_STATIC ∪ PRIORITY ∪ HIP3
  → applies ROTATING_LIMIT to novel only
  → merges into watchlist after priority, before static fill
```

RA prune must not break that read: active query remains

```sql
SELECT asset, max(expires_at) AS exp
FROM binance_oi_rotation_watchlist_history
WHERE state IN ('entered', 'active') AND expires_at > now()
GROUP BY asset
ORDER BY exp DESC
```

## 4. Configuration

| Env | Default | Meaning |
| --- | --- | --- |
| `BINANCE_OI_ROTATION_WATCHLIST_HOURS` | `36` | Soft membership TTL (existing) |
| `BINANCE_OI_ROTATION_FEED_EXPIRY_HOURS` | `6` | Feed soft TTL (existing) |
| `BINANCE_OI_PRUNE_ENABLED` | `1` | Master prune switch |
| `BINANCE_OI_WATCHLIST_HISTORY_RETENTION_DAYS` | `14` | Hard prune history table |
| `BINANCE_OI_OBSERVATIONS_RETENTION_DAYS` | `30` | Hard prune observations |
| `BINANCE_OI_RAW_OI_RETENTION_DAYS` | `30` | Hard prune raw OI |
| `BINANCE_OI_EVENTS_RETENTION_DAYS` | `90` | Hard prune events |
| `BINANCE_OI_SCANS_RETENTION_DAYS` | `30` | Hard prune scans (+ deliveries) |
| `BINANCE_OI_STATIC_SEED_PATH` | unset | Optional JSON bases for P1 skip |
| `BINANCE_OI_STATIC_MEMBERSHIP_SKIP` | `0` until P1 ships; then default `1` | Skip active membership for seed |

## 5. Implementation map

| File | Change |
| --- | --- |
| `config.py` | retention + prune + static-skip envs |
| `binance_oi_prune.py` (new) or scanner helpers | `prune_binance_oi_db` |
| `binance_oi_rotation_worker.py` | call prune on cadence |
| `binance_oi_rotation_scanner.py` | P1 membership skip in `_update_watchlist*` |
| `test_binance_oi_prune.py` (new) | retention deletes |
| `test_binance_oi_rotation_scanner.py` | extend for static skip |

Patch order and deploy: NT [`IMPL-013`](../../nautilus-trading-os/specs/IMPL-013-oi-rotating-static-subtract-and-ttl.md) slices B then C.

## 6. Acceptance

1. Unit: old observation row deleted; row within retention kept.
2. Unit: active membership with `expires_at` in future still returned after prune.
3. Live: `[oi-prune]` log lines; `binance_oi.db` size stable or down over ≥48h continuous scanner uptime.
4. P1: seeded static asset produces event without remaining in active membership query.
5. NT smoke (after ADR-013 P0a): rotating list novel-heavy; scanner restart alone does not require NT code change for prune.

## 7. Rollback

- `BINANCE_OI_PRUNE_ENABLED=0` — stop deletes immediately.
- `BINANCE_OI_STATIC_MEMBERSHIP_SKIP=0` — restore membership writes for all qualifiers.
- NT `OI_ROTATING_STATIC_SUBTRACT=0` — independent consumer rollback.

## 8. Out of scope

- Changing OI percentile / volume gates.
- Multi-exchange OI aggregation.
- Pruning NT Parquet catalog (owned by `CATALOG_RETENTION_DAYS` in data-oi).
- Reviving stopped RA orchestrator solely for prune (worker is enough).
