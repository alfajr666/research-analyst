"""Bootstrap bounded 15-minute CoinAnalyze history for trend-acceleration research."""

import argparse
import json
import time
from datetime import datetime, timezone

import config
from ingest_coinalyze import fetch_coinalyze_data_batched, load_symbols


def _timestamp(raw_timestamp) -> datetime:
    value = float(raw_timestamp)
    if value > 1e12:
        value /= 1000.0
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _history_map(response: list) -> dict[str, dict[datetime, float]]:
    values = {}
    for item in response:
        history = {}
        for candle in item.get("history", []):
            if candle.get("t") is not None and candle.get("c") is not None:
                history[_timestamp(candle["t"])] = float(candle["c"])
        values[item.get("symbol")] = history
    return values


def _ohlcv_map(response: list) -> dict[str, dict[datetime, dict]]:
    values = {}
    for item in response:
        history = {}
        for candle in item.get("history", []):
            if candle.get("t") is None or candle.get("c") is None:
                continue
            history[_timestamp(candle["t"])] = {
                "open": float(candle.get("o", 0.0)),
                "high": float(candle.get("h", 0.0)),
                "low": float(candle.get("l", 0.0)),
                "close": float(candle["c"]),
                "volume": float(candle.get("v", 0.0)),
            }
        values[item.get("symbol")] = history
    return values


def _underlying(symbol: str) -> str:
    base = symbol.split("_")[0]
    return base.removesuffix("USDT").removesuffix("USD")


def bootstrap(symbols: list[str], days: int):
    """Fetch and replace a bounded research window using historical API endpoints."""
    if not config.COINANALYZE_API_KEY:
        raise RuntimeError("COINANALYZE_API_KEY is required for historical bootstrap")

    now_epoch = int(time.time())
    from_epoch = now_epoch - days * 24 * 3600
    params = {
        "symbols": ",".join(symbols),
        "interval": "15min",
        "from": str(from_epoch),
        "to": str(now_epoch),
    }
    print(f"Fetching {days} days of 15m OHLCV, OI, and funding for {len(symbols)} symbols...")
    ohlcv = _ohlcv_map(fetch_coinalyze_data_batched("ohlcv-history", params, batch_size=20))
    oi = _history_map(fetch_coinalyze_data_batched("open-interest-history", params, batch_size=20))
    funding = _history_map(fetch_coinalyze_data_batched("funding-rate-history", params, batch_size=20))

    rows = []
    for symbol, candles in ohlcv.items():
        for timestamp, candle in candles.items():
            rows.append((
                timestamp,
                _underlying(symbol),
                symbol,
                oi.get(symbol, {}).get(timestamp, 0.0),
                funding.get(symbol, {}).get(timestamp, 0.0),
                0.0,
                0.0,
                0.0,
                1.0,
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"],
                candle["volume"],
            ))

    if not rows:
        raise RuntimeError("CoinAnalyze returned no OHLCV rows for the requested bootstrap")

    conn = config.get_db_connection(read_only=False)
    try:
        symbols_sql = ", ".join("?" for _ in symbols)
        conn.execute(
            f"DELETE FROM futures_data WHERE symbol IN ({symbols_sql}) AND timestamp >= ?",
            [*symbols, datetime.fromtimestamp(from_epoch, tz=timezone.utc)],
        )
        conn.executemany("""
            INSERT INTO futures_data (
                timestamp, underlying, symbol, open_interest, funding_rate, predicted_funding,
                liquidation_long, liquidation_short, long_short_ratio,
                open, high, low, close, volume
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        conn.commit()
    finally:
        conn.close()
    print(f"Bootstrapped {len(rows)} 15m rows across {len(ohlcv)} symbols.")


def _scanner_research_symbols() -> list[str]:
    path = config.DEFAULT_DB_DIR / "scanned_pairs.json"
    if not path.exists():
        return []
    with open(path, "r") as file:
        return json.load(file).get("research_universe", [])


def main():
    parser = argparse.ArgumentParser(description="Bootstrap CoinAnalyze history for trend research")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--symbols", nargs="*", help="CoinAnalyze symbols; defaults to current research universe")
    args = parser.parse_args()
    if args.days < 11:
        parser.error("--days must be at least 11 for the current score windows")

    symbols = args.symbols or _scanner_research_symbols() or load_symbols()
    for benchmark in ("BTCUSDT_PERP.A", "ETHUSDT_PERP.A", "SOLUSDT_PERP.A"):
        if benchmark not in symbols:
            symbols.append(benchmark)
    bootstrap(symbols, args.days)


if __name__ == "__main__":
    main()
