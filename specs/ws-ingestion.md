# WS Ingestion Spec (ADR)

## Goal

Use public WebSocket ingestion rather than provider polling for the live market
feed. The gateway maintains a continuous, resampled market-data feed for the
rotation watchlist plus permanent assets. Evaluators keep reading the same
`source_observations` tables; only the ingestion path changes.

## Dynamic subscription universe

The symbol-rotation worker ranks the valid Bybit linear USDT-perpetual ticker
snapshot and publishes a versioned feed. The effective universe is the feed's
unexpired sticky watchlist plus `BTC`, `ETH`, `PAXG`, and `QQQUSDT`. If the feed
is unavailable, invalid, or expired, the gateway falls back to those permanent
symbols only. There is no repository static symbol list and no static admission
allowlist.

## Defaults

| Setting | Default | Notes |
| --- | --- | --- |
| `WS_BYBIT_ENABLED` | `true` | Primary public source (Bybit V5). |
| `WS_BINANCE_ENABLED` | `false` | Opt-in, off by default. |
| `WS_STREAM_TIMEFRAMES` | `5m` | 5m kline + markPrice streamed; the auxiliary 15m frame is derived from 5m. Strategy 1h/4h history comes from regime-owned direct REST data. |
| `WS_MARKPRICE_ENABLED` | `true` | markPrice @1s for live state / funding context. |

Streaming 5m kline + markPrice keeps stream counts low (see capacity below).
The regime worker's direct REST 1h/4h cache is separate and does not add a
WebSocket topic.

## Capacity (no exhaustion risk)

- **Bybit V5**: per-connection topic cap is low → **shard** symbols across a pool.
  ~10–20 symbols/connection ⇒ a small pool at the configured 80-symbol cap. Use a
  `ConnectionPool` that balances symbols and reconnects per-shard.
- **Binance** (when enabled): single combined stream supports ≤1024 streams ⇒ one
  connection covers everything. Subscribe paced at ≤5 msg/s at startup.
- Throughput at the default cap: 80 symbols × 2 topics (5m+markPrice) ≈ 160
  streams; markPrice peak ~80/s. Trivial.

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
