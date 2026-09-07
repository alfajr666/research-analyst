# WS Ingestion Spec (ADR)

## Goal

Use public WebSocket ingestion rather than provider polling for the live market
feed. The gateway maintains a continuous, resampled market-data feed for the
static universe
static universe (plus optional rotation). Evaluators keep reading the same
`source_observations` tables; only the ingestion path changes.

## Static universe (persistent, approved snapshot)

The agreed symbol list is sourced from the approved `propr_python.tradeable_assets`
CRYPTO snapshot and persisted in the repo at
**`symbols/static_universe.json`** (version-controlled, survives restarts/prunes). It stores
canonical bases (e.g. `BTC`); `config.expand_perp_symbols(base, venue)` maps them to
`BTCUSDT` perps per venue at load time.

- Loaded via `config.load_static_symbols()` (env override `STATIC_SYMBOLS` comma list or
  `STATIC_SYMBOLS_PATH`).
- The approved snapshot yields **97 bases** (not 150). To reach 150, extend the JSON —
  do not invent symbols. The file records `count` and `cap_target` for audit.
- `WS_SYMBOL_SOURCE` selects the eval universe: `static` (approved list), `rotated`
  (rotation feed only), or `both` (union).

## Defaults

| Setting | Default | Notes |
| --- | --- | --- |
| `WS_BYBIT_ENABLED` | `true` | Primary public source (Bybit V5). |
| `WS_BINANCE_ENABLED` | `false` | Opt-in, off by default. |
| `WS_SYMBOL_SOURCE` | `static` | `static` \| `rotated` \| `both`; static from `symbols/static_universe.json`. |
| `WS_STREAM_TIMEFRAMES` | `5m` | 5m kline + markPrice streamed; canonical 15m/1h/4h bars resampled from 5m locally. The engine may seed strategy 1h/4h history from regime-owned direct REST data. |
| `WS_MARKPRICE_ENABLED` | `true` | markPrice @1s for live state / funding context. |

Streaming 5m kline + markPrice matches the strategy-facing "higher TF is
resampled" rule (15m/1h/4h are derived from the 5m base), and keeps stream
counts low (see capacity below). The regime worker's direct REST 4h cache is
separate and does not add a WebSocket topic.

## Capacity (no exhaustion risk)

- **Bybit V5**: per-connection topic cap is low → **shard** symbols across a pool.
  ~10–20 symbols/connection ⇒ ~8–15 connections for 150 symbols. Use a
  `ConnectionPool` that balances symbols and reconnects per-shard.
- **Binance** (when enabled): single combined stream supports ≤1024 streams ⇒ one
  connection covers everything. Subscribe paced at ≤5 msg/s at startup.
- Throughput: 97 symbols × 2 topics (5m+markPrice) ≈ 194 streams; markPrice peak ~97/s. Trivial.

## Components

```
ws_gateway.py
  ConnectionPool
    - per-exchange manager (Bybit sharded / Binance combined)
    - auto-reconnect, ping/pong, startup subscribe pacing
    - on (re)connect: gap-fill missed 5m window from REST before resuming
  StreamRouter
    - normalize raw msg -> {symbol, tf, kind: ohlcv|markprice, payload}
    - shard affinity by symbol hash
  IngestBuffer
    - micro-batch writes, bounded queue, backpressure to DB writer
    - stamp source = 'bybit_ws' | 'binance_ws'
    - stamp data_purity (preserve evaluator gates; failover keeps purity tag)
ResampleWorker (separate tick loop)
     - on each 5m close: aggregate -> 15m -> 1h -> 4h for canonical market data; the engine's hybrid strategy HTF loader may use direct REST seed history before this tail
    - writes derived bars into source_observations with derived provenance
```

## Data contract

- Raw 5m bars and markPrice are written with `source`/`data_purity` stamps so
  `strategy_plugins._get_bar_purity` can verify feed purity.
- Derived (15m/1h/4h) bars stamped `source='resampled'`,
  `data_purity` inherited from the 5m parent's purity.
- HTF swing/FVG/OB detectors (`structure_zones`) read the resampled 1h/4h bars
  into memory. They do not require a persisted zone table.

## Tiered retention (prune TTL)

SQLite is embedded; WS is a continuous firehose, so keep a
tiered prune (extends existing `prune_db`):

| Data | Keep | Rationale |
| --- | --- | --- |
| 5m / 15m (resampled) | 30–90 days | main eval horizon |
| HTF 1h / 4h bars | 365 days | regime and strategy context; zones are recomputed in memory |
| `ws_gap_fill_log`, connection health | 30 days | ops audit |

Nightly `VACUUM` retained.

## Boundary

No exchange credentials, no order placement. WS is read-only market data only.
