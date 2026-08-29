"""Bootstrap bounded 15-minute CoinAnalyze history for trend-acceleration research."""

import argparse
import json
import time
from datetime import datetime, timezone

import config
from api_clients.coinalyze import CoinAnalyzeClient

_coin_client = CoinAnalyzeClient()


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
    ohlcv = _ohlcv_map(_coin_client.fetch_batched("ohlcv-history", symbols, other_params={"interval": "15min", "from": str(from_epoch), "to": str(now_epoch)}, cutoff_id="bootstrap"))
    oi = _history_map(_coin_client.fetch_batched("open-interest-history", symbols, other_params={"interval": "15min", "from": str(from_epoch), "to": str(now_epoch)}, cutoff_id="bootstrap"))
    funding = _history_map(_coin_client.fetch_batched("funding-rate-history", symbols, other_params={"interval": "15min", "from": str(from_epoch), "to": str(now_epoch)}, cutoff_id="bootstrap"))

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
        # Bootstrap now writes ONLY to source_observations (legacy futures_data dropped)
        for row in rows:
            ts, und, sym, _, _, _, _, _, _, o, h, l, c, v = row
            obs_id = f"coinalyze:{sym}:{ts.isoformat()}"
            payload = {"open": o, "high": h, "low": l, "close": c, "volume": v, "open_interest": row[3], "funding_rate": row[4]}
            conn.execute("""
                INSERT OR IGNORE INTO source_observations (
                    observation_id, source, venue, native_symbol, asset, market_kind, interval,
                    source_start, source_end, retrieved_at, retrieval_kind, payload_json
                ) VALUES (?, 'coinalyze', 'aggregate_perp', ?, ?, 'perpetual', '15m', ?, ?, ?, 'bootstrap', ?)
            """, (obs_id, sym, und, ts, ts, ts, json.dumps(payload, default=str)))
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
