"""Binance USD-M perpetual open-interest rotation discovery.

This module owns Binance-native discovery only.  It never changes the legacy
CoinAnalyze scanner artifact and publishes a venue-neutral, atomic feed.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev

import httpx

import config


SOURCE = "binance_usdm"


def completed_hour(now: datetime | None = None) -> datetime:
    """Return the UTC hour whose OHLCV bar has fully closed."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)


def _percentile(value: float, values: list[float]) -> float | None:
    if not values:
        return None
    return sum(item <= value for item in values) / len(values)


def _volume_anomaly(current: float, history: list[float]) -> float | None:
    if len(history) < 2:
        return None
    deviation = pstdev(history)
    if deviation == 0:
        return 0.0
    return (current - mean(history)) / deviation


def _latest_at_or_before(rows: list[dict], timestamp_ms: int) -> dict | None:
    """OI history timestamps are observations, not guaranteed candle-open times."""
    matching = [row for row in rows if int(row["timestamp"]) <= timestamp_ms]
    return max(matching, key=lambda row: int(row["timestamp"])) if matching else None


def _latest_in_window(rows: list[dict], start_ms: int, end_ms: int) -> dict | None:
    matching = [row for row in rows if start_ms <= int(row["timestamp"]) <= end_ms]
    return max(matching, key=lambda row: int(row["timestamp"])) if matching else None


def coinalyze_symbol_from_binance(symbol: str) -> str:
    """Keep the known Binance-to-CoinAnalyze aliases in one producer mapping."""
    aliases = {"SHIB1000USDT": "1000SHIBUSDT_PERP.A", "1000FLOKIUSDT": "FLOKIUSDT_PERP.3"}
    return aliases.get(symbol, f"{symbol}_PERP.A")


def build_observation(
    market: dict,
    ticker: dict | None,
    oi_history: list[dict] | None,
    candles: list[list] | None,
    interval: datetime,
    history_hours: int = config.BINANCE_OI_ROTATION_HISTORY_HOURS,
) -> dict:
    """Build one auditable discovery observation from completed Binance inputs."""
    symbol = market["symbol"]
    asset = market["baseAsset"]
    base = {
        "source": SOURCE,
        "symbol": symbol,
        "asset": asset,
        "quote": market["quoteAsset"],
        "contract_type": market["contractType"].lower(),
        "volume_24h_usd": 0.0,
        "is_eligible": False,
        "rejection_reason": None,
    }
    if not oi_history or not candles:
        return {**base, "is_eligible": False, "rejection_reason": "missing_history"}

    interval_ms = int(interval.timestamp() * 1000)
    prior_ms = int((interval - timedelta(hours=1)).timestamp() * 1000)
    # The current point is the last OI observation before the closed candle ends.
    current_oi = _latest_in_window(oi_history, interval_ms, interval_ms + 3_600_000 - 1)
    prior_oi = _latest_at_or_before(oi_history, interval_ms - 1)
    candle_by_open = {int(candle[0]): candle for candle in candles}
    current_candle = candle_by_open.get(interval_ms)
    prior_candle = candle_by_open.get(prior_ms)
    if not current_oi or not prior_oi or not current_candle or not prior_candle:
        return {**base, "is_eligible": False, "rejection_reason": "incomplete_completed_hour"}

    # The live 24h ticker includes trading after this completed interval. Rebuild
    # its notional from closed candles so eligibility remains point-in-time.
    volume_24h = sum(
        float(candle[7]) for candle in candles
        if interval_ms - 23 * 3_600_000 <= int(candle[0]) <= interval_ms
    )
    base = {**base, "volume_24h_usd": volume_24h}
    if volume_24h < config.BINANCE_OI_ROTATION_MIN_24H_VOLUME_USD:
        return {**base, "rejection_reason": "below_liquidity_floor"}
    base = {**base, "is_eligible": True}

    oi_values = [(int(row["timestamp"]), float(row["sumOpenInterestValue"])) for row in oi_history]
    current_oi_usd = float(current_oi["sumOpenInterestValue"])
    prior_oi_usd = float(prior_oi["sumOpenInterestValue"])
    if prior_oi_usd <= 0:
        return {**base, "is_eligible": False, "rejection_reason": "invalid_prior_oi"}
    oi_pct_changes = [
        (value / previous - 1)
        for (_, previous), (_, value) in zip(oi_values, oi_values[1:])
        if previous > 0
    ]
    current_oi_pct = current_oi_usd / prior_oi_usd - 1
    prior_close = float(prior_candle[4])
    current_close = float(current_candle[4])
    if prior_close <= 0:
        return {**base, "is_eligible": False, "rejection_reason": "invalid_prior_close"}
    volumes = [(int(candle[0]), float(candle[7])) for candle in candles]
    current_volume = float(current_candle[7])
    trailing_volumes = [value for timestamp, value in volumes if timestamp < interval_ms][-history_hours:]
    history_changes = oi_pct_changes[-history_hours:]
    percentile = _percentile(current_oi_pct, history_changes)
    anomaly = _volume_anomaly(current_volume, trailing_volumes)
    if percentile is None or anomaly is None:
        return {**base, "is_eligible": False, "rejection_reason": "insufficient_history"}
    return {
        **base,
        "open_interest_usd": current_oi_usd,
        "oi_change_1h_pct": current_oi_pct,
        "oi_change_1h_usd": current_oi_usd - prior_oi_usd,
        "price_change_1h": current_close / prior_close - 1,
        "volume_1h_usd": current_volume,
        "volume_anomaly": anomaly,
        "oi_spike_percentile": percentile,
    }


def qualify_and_rank(observations: list[dict]) -> list[dict]:
    """Apply configuration gates and deterministic, direction-neutral ranking."""
    candidates = [
        record for record in observations
        if record.get("is_eligible")
        and record.get("oi_change_1h_usd", 0) >= config.BINANCE_OI_ROTATION_MIN_OI_DELTA_USD
        and record.get("oi_spike_percentile", 0) >= config.BINANCE_OI_ROTATION_MIN_OI_PERCENTILE
        and record.get("volume_anomaly", 0) >= config.BINANCE_OI_ROTATION_MIN_VOLUME_ANOMALY
    ]
    ordered = sorted(
        candidates,
        key=lambda item: (-item["oi_spike_percentile"], -item["oi_change_1h_usd"], -item["volume_anomaly"], item["symbol"]),
    )
    return [{**record, "rank": index} for index, record in enumerate(ordered, 1)]


def build_feed(interval: datetime, candidates: list[dict], generated_at: datetime | None = None) -> dict:
    generated_at = generated_at or datetime.now(timezone.utc)
    expiry = interval + timedelta(hours=config.BINANCE_OI_ROTATION_FEED_EXPIRY_HOURS)
    return {
        "schema_version": 1,
        "source": SOURCE,
        "scanner_version": config.BINANCE_OI_ROTATION_SCANNER_VERSION,
        "generated_at": generated_at.isoformat(),
        "completed_interval_at": interval.isoformat(),
        "expires_at": expiry.isoformat(),
        "candidates": [{
            key: candidate[key] for key in (
                "asset", "symbol", "quote", "contract_type", "rank", "volume_24h_usd",
                "open_interest_usd", "oi_change_1h_pct", "oi_change_1h_usd", "price_change_1h",
                "volume_1h_usd", "volume_anomaly", "oi_spike_percentile",
            )
        } for candidate in candidates],
    }


def publish_feed_atomic(feed: dict, path: Path | None = None) -> None:
    """Replace the feed only after a fully flushed JSON document exists."""
    path = Path(path or config.BINANCE_OI_ROTATION_FEED_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(feed, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class BinanceClient:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(base_url=config.BINANCE_FUTURES_BASE_URL, timeout=20.0)

    def get(self, path: str, params: dict | None = None):
        response = self.client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def eligible_markets(self) -> tuple[list[dict], dict[str, dict]]:
        exchange_info = self.get("/fapi/v1/exchangeInfo")
        tickers = {item["symbol"]: item for item in self.get("/fapi/v1/ticker/24hr")}
        markets = [
            item for item in exchange_info["symbols"]
            if item["status"] == "TRADING" and item["contractType"] == "PERPETUAL"
            and item["quoteAsset"] == "USDT" and item["marginAsset"] == "USDT"
        ]
        return markets, tickers

    def history(self, symbol: str, interval: datetime) -> tuple[list[dict], list[list]]:
        start = interval - timedelta(hours=config.BINANCE_OI_ROTATION_HISTORY_HOURS + 2)
        end = interval + timedelta(hours=1) - timedelta(milliseconds=1)
        params = {"symbol": symbol, "period": "1h", "startTime": int(start.timestamp() * 1000), "endTime": int(end.timestamp() * 1000), "limit": config.BINANCE_OI_ROTATION_HISTORY_HOURS + 3}
        oi_history = self.get("/futures/data/openInterestHist", params)
        candles = self.get("/fapi/v1/klines", {"symbol": symbol, "interval": "1h", "startTime": params["startTime"], "endTime": params["endTime"], "limit": params["limit"]})
        return oi_history, candles


def _persist(conn, interval: datetime, observations: list[dict], candidates: list[dict], raw_oi_history: dict[str, list[dict]], observed_at: datetime, scan_complete: bool) -> None:
    version = config.BINANCE_OI_ROTATION_SCANNER_VERSION
    rows = [(
        SOURCE, interval, version, item["symbol"], item["asset"], item["quote"], item["contract_type"],
        item["is_eligible"], item.get("rejection_reason"), item["volume_24h_usd"], item.get("open_interest_usd"),
        item.get("oi_change_1h_pct"), item.get("oi_change_1h_usd"), item.get("price_change_1h"),
        item.get("volume_1h_usd"), item.get("volume_anomaly"), item.get("oi_spike_percentile"), observed_at,
    ) for item in observations]
    conn.executemany("""INSERT INTO binance_oi_rotation_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING""", rows)
    raw_rows = [
        (SOURCE, symbol, datetime.fromtimestamp(int(row["timestamp"]) / 1000, tz=timezone.utc), float(row["sumOpenInterestValue"]), interval)
        for symbol, rows in raw_oi_history.items()
        for row in rows
    ]
    if raw_rows:
        conn.executemany("""INSERT INTO binance_oi_rotation_raw_oi_history VALUES (?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING""", raw_rows)
    for item in candidates:
        conn.execute("""INSERT INTO binance_oi_rotation_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING""", (SOURCE, item["asset"], interval, version, item["symbol"], item["rank"], json.dumps(item, sort_keys=True), observed_at))
    conn.execute("""INSERT INTO binance_oi_rotation_scans VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (source, completed_interval_at, scanner_version) DO UPDATE SET
            status = excluded.status, completed_at = excluded.completed_at""",
        (SOURCE, interval, version, "complete" if scan_complete else "incomplete", observed_at))


def _update_watchlist(conn, interval: datetime, candidates: list[dict]) -> None:
    """Retain rotation research context without duplicating another pool's warmup."""
    expires_at = interval + timedelta(hours=config.BINANCE_OI_ROTATION_WATCHLIST_HOURS)
    selected_assets = {item["asset"] for item in candidates}
    current = conn.execute("""
        SELECT asset, symbol, state, expires_at
        FROM binance_oi_rotation_watchlist_history
        WHERE source = ?
        QUALIFY ROW_NUMBER() OVER (PARTITION BY source, asset ORDER BY observed_at DESC) = 1
    """, (SOURCE,)).fetchall()
    for asset, symbol, state, expiry in current:
        if state != "expired" and asset not in selected_assets and interval >= expiry:
            conn.execute("""INSERT INTO binance_oi_rotation_watchlist_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING""", (SOURCE, asset, symbol, interval, "expired", expiry, False, False))
    for item in candidates:
        overlap = conn.execute("""
            SELECT 1 FROM (
                SELECT state FROM discovery_watchlist_history
                WHERE asset = ?
                QUALIFY ROW_NUMBER() OVER (PARTITION BY pool, symbol ORDER BY observed_at DESC) = 1
            ) WHERE state != 'expired'
            LIMIT 1
        """, (item["asset"],)).fetchone() is not None
        prior = next((row for row in current if row[0] == item["asset"]), None)
        new_entry = prior is None or prior[2] == "expired"
        deep_backfill_required = new_entry and not overlap
        conn.execute("""INSERT INTO binance_oi_rotation_watchlist_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING""", (SOURCE, item["asset"], item["symbol"], interval, "entered" if new_entry else "active", expires_at, deep_backfill_required, overlap))
        if deep_backfill_required:
            coinalyze_symbol = coinalyze_symbol_from_binance(item["symbol"])
            job_exists = conn.execute("SELECT 1 FROM deep_backfill_jobs WHERE symbol = ?", (coinalyze_symbol,)).fetchone()
            if not job_exists:
                from two_pool_discovery import enqueue_deep_backfill_jobs
                enqueue_deep_backfill_jobs(conn, interval, [coinalyze_symbol])


def run_scanner(now: datetime | None = None, client: BinanceClient | None = None) -> dict:
    """Fetch one closed hour, persist its research record, and publish its feed."""
    interval = completed_hour(now)
    observed_at = now or datetime.now(timezone.utc)
    client = client or BinanceClient()
    markets, tickers = client.eligible_markets()
    markets.sort(key=lambda market: float(tickers.get(market["symbol"], {}).get("quoteVolume", 0)), reverse=True)
    observations = []
    raw_oi_history: dict[str, list[dict]] = {}
    fetched_contracts = 0
    for market in markets:
        ticker = tickers.get(market["symbol"])
        if config.BINANCE_OI_ROTATION_MAX_CONTRACTS > 0 and fetched_contracts >= config.BINANCE_OI_ROTATION_MAX_CONTRACTS:
            observations.append({"source": SOURCE, "symbol": market["symbol"], "asset": market["baseAsset"], "quote": market["quoteAsset"], "contract_type": market["contractType"].lower(), "volume_24h_usd": 0.0, "is_eligible": False, "rejection_reason": "api_budget_exceeded"})
            continue
        try:
            fetched_contracts += 1
            oi_history, candles = client.history(market["symbol"], interval)
            raw_oi_history[market["symbol"]] = oi_history
            observations.append(build_observation(market, ticker, oi_history, candles, interval))
        except httpx.HTTPError:
            observations.append({"source": SOURCE, "symbol": market["symbol"], "asset": market["baseAsset"], "quote": market["quoteAsset"], "contract_type": market["contractType"].lower(), "volume_24h_usd": 0.0, "is_eligible": False, "rejection_reason": "market_data_request_failed"})
    candidates = qualify_and_rank(observations)
    conn = config.get_db_connection()
    try:
        scan_complete = not any(item.get("rejection_reason") == "market_data_request_failed" for item in observations)
        _persist(conn, interval, observations, candidates, raw_oi_history, observed_at, scan_complete)
        _update_watchlist(conn, interval, candidates)
        conn.commit()
    finally:
        conn.close()
    feed = build_feed(interval, candidates, observed_at)
    publish_feed_atomic(feed)
    return feed
