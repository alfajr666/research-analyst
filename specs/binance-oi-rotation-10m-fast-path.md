# Binance OI Rotation — 10m Liquid-Tier Fast Path

## Problem Statement

The existing Binance USD-M OI rotation scanner
(`specs/binance-oi-rotation-scanner.md`) discovers economically meaningful
open-interest spikes on **completed one-hour bars**, publishes one atomic
venue-neutral feed, and lets downstream bots (Bybit, MEXC, Propr, testnet)
admit external symbols from that feed without changing their strategy engines.

Hourly cadence is correct for research completeness and full-universe cost
control, but it is slow for **front-loaded** anomalies (example: a name that
prints +70% OI and extreme volume inside the first tens of minutes of an hour).
Operators want those names on the shared feed **as soon as a short completed bar
proves the move**, without:

- a second feed file or schema bots must learn,
- direction / entry / signal fields,
- unbounded API load from rescanning the full perpetual universe every few
  minutes,
- breaking multi-hour Discord digests or the hourly research ledger.

## Solution

Add a **second producer cadence** beside the existing hourly full-universe job:

| Cadence | Universe | Role |
|---------|----------|------|
| **Every completed 10m** (or 15m if Binance history lacks 10m — see below) | **Liquid tier only** | Fast anomaly catch; may refresh the **same** atomic feed |
| **Every completed 1h** | **Full universe** (current job) | Authoritative broad snapshot, full research observations, multi-hour Discord boundary input |
| **Multi-hour Discord** | Unchanged | Still every 6 completed **hours** on UTC boundaries `05/11/17/23` |

**Outcome treatment is identical to today** so bots pick candidates up
naturally:

- Same path: `BINANCE_OI_ROTATION_FEED_PATH` → `data/binance_oi_rotation_feed.json`
- Same atomic publish (`*.tmp` → fsync → `os.replace`)
- Same envelope: `schema_version: 1`, `source: "binance_usdm"`, `scanner_version`,
  `generated_at`, `completed_interval_at`, `expires_at`, `candidates[]`
- Same per-candidate **field names** bots and research already tolerate
  (at minimum `asset` + rank order; keep existing metric keys)
- Same consumer rules: fresh `expires_at`, venue resolve by **base asset**,
  external quota, cold admission, dedupe with local discovery — **no feed-schema
  change required**. NT rotating budget (novel-only after static-subtract) is
  [`ADR-013`](../../nautilus-trading-os/specs/ADR-013-oi-rotating-static-subtract-and-ttl.md);
  OI DB hard prune is [`binance-oi-rotation-retention.md`](./binance-oi-rotation-retention.md).

The scanner remains discovery-only. It does not trade.

## Locked Product Decisions

These were locked in design discussion before this spec:

1. **Dual cadence:** 10m liquid tier + 1h full universe.
2. **Liquid tier ≠ market cap.** Use turnover / activity floors (and optional
   movers), not mcap rank alone. Naive top-N by 24h volume alone collapses to
   majors in calm regimes and can miss the first print of a newly hot alt.
3. **Multi-hour Discord unchanged** (6h window, UTC hour boundaries).
4. **Same feed artifact and bot contract** — no parallel JSON schema.
5. **1h job remains** the full-universe research path; 10m does not replace it.

## User Stories

1. As an operator, I want a closed short bar (≈10m) on liquid names scanned soon
   after it completes, so that RED-class OI+volume spikes reach the shared feed
   tens of minutes earlier than the hourly job.
2. As an operator, I want liquid-tier selection to include names that are
   **heating** (volume/OI activity), not only perennial majors, so that
   rotation alts are eligible without a full-universe 10m scan.
3. As a research analyst, I want the hourly full-universe scan preserved, so that
   thin or late names the liquid tier missed still enter the research ledger and
   feed on the hour.
4. As a downstream bot operator, I want the feed path, envelope, `source`,
   `schema_version`, `expires_at`, and ranked `asset` list to stay valid under
   existing adapters, so that Bybit / MEXC / Propr / testnet need no release to
   consume faster discoveries.
5. As a downstream bot operator, I want an empty 10m pass to **not wipe** still-
   valid hourly candidates from the feed, so that a quiet 10m does not starve
   external quotas.
6. As a research analyst, I want 10m and 1h observations stored with clear
   interval identity, so that point-in-time research can separate cadences.
7. As an operator, I want multi-hour Discord digests to keep using **hourly**
   events and the same 6h schedule, so that operator workflow does not change.
8. As an operator, I want optional fast Discord (10m) without forcing spam:
   post when new strong qualifiers appear; skip empty; do not alter multi-hour.
9. As a system operator, I want 10m API cost bounded by liquid-tier size and
   history window, so that the worker cannot fight the hourly job or Binance
   weight limits.
10. As a bot operator, I want feed expiry semantics unchanged (default hours from
    the published interval identity), so that existing staleness checks keep
    working.

## Consumer Contract (must not break)

Verified against live bot adapters:

- `bybit-binance-trading-agent/.../binance_oi_rotation_feed.py`
- `bybit-binance-trading-agent-test/...` (same pattern)
- `mexc-trading-agent/.../binance_oi_rotation_feed.py`
- `propr-trading-agent/.../binance_oi_rotation_feed.py`

Each adapter:

1. Reads **one local file** (`BINANCE_OI_ROTATION_FEED_PATH`).
2. Accepts only `schema_version == 1` and `source == "binance_usdm"`.
3. Rejects when `expires_at <= now`.
4. Walks `candidates` **in order**, maps `asset` → venue symbol, applies
   **external quota**, ignores metric field names for admission.

Therefore this fast path **must not**:

- change `source` or `schema_version` without a coordinated multi-repo bump,
- publish a second file bots do not read,
- clear `candidates` on a no-op 10m scan while a prior feed is still unexpired,
- add required new envelope keys,
- put venue-specific symbols in `asset`.

Metric keys may continue to use the existing `*_1h_*` names for the **hourly**
payload. For 10m-origin candidates published into the same feed, either:

- **(Preferred)** keep the same key names but document that values are the
  **active discovery bar** metrics (bar length indicated by optional
  non-breaking metadata — see below), or
- dual-write both bar-native keys and the existing keys with identical values
  for the active bar so older readers keep working.

Optional **additive** candidate fields (bots ignore unknown keys today):

- `bar_minutes` (e.g. `10` or `15`)
- `discovery_cadence` (`"10m_liquid"` | `"1h_full"`)
- `oi_change_bar_pct` / `oi_change_bar_usd` (explicit); still populate legacy
  keys used by Discord/research formatters when those run on the same object

Do not remove legacy keys from published candidates.

## Architecture

```
                    ┌─────────────────────────┐
                    │  Binance USDM public API │
                    └───────────┬─────────────┘
                                │
          ┌─────────────────────┴─────────────────────┐
          ▼                                           ▼
 ┌─────────────────────┐                   ┌─────────────────────┐
 │ 10m liquid scanner  │                   │ 1h full scanner     │
 │ (new cadence)       │                   │ (existing)          │
 └──────────┬──────────┘                   └──────────┬──────────┘
            │                                         │
            │  observations/events                    │  observations/events
            │  (interval grain = 10m)                 │  (interval grain = 1h)
            ▼                                         ▼
            └─────────────────────┬───────────────────┘
                                  ▼
                     ┌────────────────────────┐
                     │ binance_oi.db          │
                     │ scans / obs / events   │
                     │ (cadence-aware keys)   │
                     └───────────┬────────────┘
                                 │
                                 ▼
                     ┌────────────────────────┐
                     │ Feed merge + atomic    │
                     │ binance_oi_rotation_   │
                     │ feed.json              │
                     └───────────┬────────────┘
                                 │
           ┌─────────────────────┼─────────────────────┐
           ▼                     ▼                     ▼
     Bot adapters          CoinAnalyze inject     Discord
     (unchanged)           (ingest_coinalyze)     10m optional
                                                  1h + multi (unchanged rules)
```

### Ownership

- Prefer extending the existing OI worker / scanner module family under
  research-analyst (`binance_oi_rotation_*`), dedicated `BINANCE_OI_DB_PATH`.
- Market DB side effects (discovery overlap, deep_backfill enqueue) stay
  **best-effort** and must not block feed publish or Discord (already the
  direction of the hourly path).
- Orchestrator may continue to own/gate the **hourly** run; 10m should run on
  the dedicated OI worker loop (wake ≤60s) without holding `market_data.db`.

## Liquid Tier Definition

**Goal:** cheap enough for 6×/hour scans; wide enough that rotation alts enter
when they heat up; not “BTC/ETH only.”

### Membership (config knobs; initial defaults suggested)

A symbol is in the **10m liquid tier** for a run if it is an active Binance
USDT-linear perpetual and **any** of:

1. **Floor:** rolling 24h quote volume ≥ `BINANCE_OI_10M_MIN_24H_VOLUME_USD`
   (default: same as hourly floor, e.g. `$5M`), **or**
2. **Heat:** 24h quote volume rank improved sharply / short-horizon volume
   acceleration exceeds a config threshold (movers sleeve), **or**
3. **Carry:** asset is already on the native OI rotation watchlist
   (`entered`/`active`) or appeared as a feed candidate within the feed TTL.

Optional filter (config, default off or soft):

- `BINANCE_OI_10M_EXCLUDE_ASSETS` / down-rank list for pure majors if operators
  want alt-heavy fast alerts (bots still see majors from the 1h job).

**Cap:** `BINANCE_OI_10M_MAX_CONTRACTS` (e.g. 80–150) after ranking tier
candidates by 24h volume then heat, to bound API weight.

**Not allowed as sole rule:** top N by 24h volume with no floor/heat/carry —
that locks the tier to majors in quiet regimes.

## Bar Size and Binance Periods

- Product intent: **≈10 minutes** completed bars.
- Implementation must use a Binance-supported `openInterestHist` **period** and
  matching kline interval.
- If **10m is not offered** by Binance for OI history, use **15m** as the fast
  bar (document actual `bar_minutes` in feed metadata) rather than inventing
  misaligned clocks. Aggregating 5m×2 is acceptable if tests prove alignment.
- Never rank an in-progress bar (same completed-bar rule as hourly).

## Features and Qualification (10m)

Mirror hourly philosophy on the short bar:

| Feature | Meaning |
|---------|---------|
| OI notional now | At/just after bar close |
| OI Δ % / Δ $ over the bar | Absolute + relative move |
| Self-relative spike | Percentile or robust score vs **own** trailing short-bar OI Δ history (multi-day) |
| Volume anomaly | Short-bar volume vs own trailing median/mean (prefer robust) |
| 24h liquidity | Point-in-time or ticker floor for eligibility |

**Qualify when** (thresholds config-only):

- in liquid tier and data complete for the closed bar,
- `oi_Δ_$` ≥ `BINANCE_OI_10M_MIN_OI_DELTA_USD` (start lower than 1h floor;
  tune from ledger — e.g. order of `$250k–$500k` as a starting point, not law),
- self-relative spike ≥ configured percentile/z,
- volume anomaly ≥ configured floor,
- OI Δ direction for ranking remains **unsigned economic activity** (no long/short).

**Rank** (stable, deterministic): spike quality → OI Δ $ → volume anomaly → symbol.

Optional anti-spam for Discord only (not feed): require 2-of-3 consecutive
short bars or cooldown per asset; **feed** may still list a single strong bar
so bots see it ASAP.

## Feed Merge Policy (critical)

Bots re-read the file each cycle and trust rank order + expiry. Publish rules:

### A. Hourly full scan (unchanged authority for broad snapshot)

1. Run full universe on completed hour.
2. Persist hourly observations/events/scans.
3. Build candidate list from hourly qualifiers.
4. Atomic publish feed with `completed_interval_at = that hour`,
   `expires_at = interval + FEED_EXPIRY_HOURS` (default 6h).
5. Discord 1h (+ multi if boundary). Watchlist/deep_backfill as today.

### B. Short-bar liquid scan

1. Run liquid tier on each new completed short bar.
2. Persist short-bar observations/events with **distinct interval timestamps**
   (and cadence dimension in DB — see Storage).
3. **Feed publish:**
   - If short-bar **has qualifiers:** atomic publish a feed whose `candidates`
     are those qualifiers (rank order). Set `completed_interval_at` to the
     short bar’s open time (ISO). Set `expires_at` with the **same expiry
     helper** bots already honor (`+ FEED_EXPIRY_HOURS`). Additive metadata
     `discovery_cadence` / `bar_minutes` allowed.
   - If short-bar **has zero qualifiers:** **do not replace** an existing
     unexpired feed with an empty candidate list. No-op on the feed file
     (research rows may still persist).
4. Optional Discord 10m/15m notify on non-empty new qualifiers only.
5. Multi-hour Discord **must not** fire on short-bar boundaries.

### C. Merge while both are “hot” (recommended)

When publishing from a short-bar pass that has qualifiers, **union** with any
candidates from the latest unexpired hourly event set (or last hourly feed
snapshot), then re-rank with a deterministic rule:

- Prefer higher spike / $ impact;
- Break ties with cadence priority config (default: stronger bar wins;
  if equal, keep stable symbol order);
- Dedupe by `asset`.

This prevents a fast RED print from dropping SUI that the hour still cares
about, and prevents a quiet 10m from erasing the hour.

Hourly publish remains allowed to rewrite the full list from the hourly
universe (authoritative catch-up).

## Storage

Extend the dedicated OI DB (`BINANCE_OI_DB_PATH`), not `market_data.db`.

Minimum approach (implementation choice, tests lock behavior):

- Add `bar_minutes` (or `interval_label`) to scan/observation/event identity
  **or** use separate scan status keys so 10m and 1h completions do not block
  each other.
- Event dedupe identity must include the completed interval **and** bar grain
  so a 10m RED event and a later 1h RED event are both researchable.
- Raw OI history may store short-period points; retention follows existing OI
  research policy.
- Watchlist TTL / deep_backfill: **same outcome treatment as hourly** when an
  asset **newly** qualifies (one-shot deep backfill if not overlapping another
  discovery pool). Short-bar entries should refresh watchlist expiry the same
  way hourly does so CoinAnalyze inject and bots’ research warmup stay aligned.

## Discord

| Message | Trigger | Change |
|---------|---------|--------|
| Short-bar digest | New short-bar qualifiers (optional, config) | **New**; empty skip |
| 1h digest | After hourly scan | Unchanged rules (`SKIP_EMPTY`, top N) |
| Multi-hour | `hour % 6 == 5` on **hourly** interval only | **Unchanged** |

Copy for short-bar posts should state **closed bar time** and age since close;
do not imply a multi-hour trade validity window. Feed `expires_at` remains a
**consumer TTL**, not a strategy horizon (hourly Discord may later drop or
relabel expiry for humans; out of scope unless done carefully).

## Config Knobs (new; names indicative)

| Knob | Purpose | Suggested default |
|------|---------|-------------------|
| `BINANCE_OI_10M_ENABLED` | Gate fast path | `true` |
| `BINANCE_OI_10M_BAR_MINUTES` | 10 or 15 | `10` with auto-fallback |
| `BINANCE_OI_10M_MIN_24H_VOLUME_USD` | Liquid floor | `5000000` |
| `BINANCE_OI_10M_MAX_CONTRACTS` | API cap | `100` |
| `BINANCE_OI_10M_MIN_OI_DELTA_USD` | $ gate | tune (`250000`–`500000` start) |
| `BINANCE_OI_10M_MIN_OI_PERCENTILE` | Self spike | `0.95` |
| `BINANCE_OI_10M_MIN_VOLUME_ANOMALY` | Vol confirm | `1.0` |
| `BINANCE_OI_10M_HISTORY_BARS` | Trailing baseline length | ~3–7d of short bars |
| `BINANCE_OI_10M_DISCORD_ENABLED` | Fast Discord | `true` |
| `BINANCE_OI_10M_FEED_MERGE_HOURLY` | Union with last hour | `true` |

Hourly knobs remain as today.

## Scheduling

- Worker wake: keep **≤60s** (already enough).
- On each wake: if new **short** completed bar missing scan → run liquid path;
  if new **hour** missing scan → run full path.
- Short path must finish well under 10m on the liquid cap; if it overruns,
  skip/queue next bar with metrics (do not overlap two full liquid pulls).
- Hourly path may still take several minutes; must not be blocked forever by
  short path (separate scan locks per cadence).

## Implementation Decisions

- Single feed file; atomic publish only.
- `source` stays `binance_usdm`; `schema_version` stays `1`.
- Completed bars only; no mid-bar ranking.
- Liquid tier = floor ∪ heat ∪ carry, plus max contracts — not mcap top-N.
- Empty short-bar does not publish empty candidates over a live feed.
- Multi-hour Discord remains hourly-boundary only.
- Deep backfill / watchlist side effects match hourly “new qualifier” semantics.
- CoinAnalyze `ingest_coinalyze.load_symbols` keeps reading the same feed; faster
  non-empty publishes automatically expand research symbols when unexpired.
- Bot quotas and cold admission stay **consumer-side** (unchanged).
- Thresholds are config; validate against the event ledger before calling them
  “final.”

## Testing Decisions

### Producer

- Completed short-bar alignment; never uses in-progress bar.
- Liquid tier: floor admits non-majors; top-N-only behavior is rejected by tests
  as insufficient when heat/carry fixtures exist.
- Qualification/ranking determinism on short bars.
- Empty short-bar **does not** clobber unexpired non-empty feed.
- Non-empty short-bar publishes schema v1 / source / expires_at / asset order.
- Merge with hourly candidates dedupes by asset and is order-stable.
- Hourly full scan still writes complete universe observations and can refresh
  feed.
- 10m and 1h scan completion flags are independent.
- Watchlist/deep_backfill enqueue once on first entry (mock market DB).

### Consumer (no bot repo change expected)

- Existing bot unit tests for feed adapters remain green against sample feeds
  produced by the short-bar publisher (fixture with `schema_version`,
  `binance_usdm`, future `expires_at`, ranked `asset`s).
- Research-analyst tests assert feed path and atomicity unchanged.

### Discord

- Multi-hour triggers only on hourly boundary fixtures.
- Short-bar notify skipped when empty; idempotent delivery keys include bar
  interval.

## Out of Scope

- Sub-minute or tick OI streaming.
- Directional signals, entries, sizing, or execution.
- Replacing bot-local discovery or changing external quotas in bot repos.
- Second feed file or `schema_version` bump (unless a later coordinated migrate).
- Changing multi-hour window math or killing the hourly full-universe job.
- Paid aggregated OI vendors.
- Guaranteeing the first 10m of a brand-new microcap with no volume history
  (carry/heat may still lag one bar — accepted).

## Rollout

1. Land producer + tests in research-analyst; flag `BINANCE_OI_10M_ENABLED`.
2. Shadow: persist short-bar events without feed publish; compare lead time vs
   hourly on historical RED-class hours.
3. Enable feed merge publish; confirm bots show new assets on next cycle without
   deploys (same path).
4. Enable optional short-bar Discord.
5. Tune $ / percentile / tier caps from live false-positive rate.

## Further Notes

- Spec parent: `specs/binance-oi-rotation-scanner.md` remains the consumer and
  product constitution; this document is an **additive cadence** with an explicit
  non-break contract.
- Runtime note: parent spec text mentions “two completed hours” feed residency in
  places; **code and bots use `expires_at` (default +6h)**. This fast path must
  follow **code/bot `expires_at`**, not the stale “two hours” sentence.
- Success metric: median minutes from first economically large short-bar OI
  print to feed `generated_at`, vs hourly-only baseline, without increasing bot
  adapter errors or empty-feed stomps.
