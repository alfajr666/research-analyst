import json
import time
import duckdb
from datetime import datetime, timezone

import config
from api_clients.coinalyze import CoinAnalyzeClient

_coin_client = CoinAnalyzeClient()

def load_symbols() -> list:
    """Loads symbols from symbols-for-dual-zone.md, merges with scanned pairs, and formats for CoinAnalyze."""
    import json
    symbols_path = config.BASE_DIR / "symbols-for-dual-zone.md"
    symbols = []
    
    # 1. Load static symbols from file
    if symbols_path.exists():
        with open(symbols_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                
                # Manual mapping for symbols with different names on CoinAnalyze
                if line == "SHIB1000USDT":
                    symbols.append("1000SHIBUSDT_PERP.A")
                    continue
                if line == "1000FLOKIUSDT":
                    symbols.append("FLOKIUSDT_PERP.3")  # Binance FLOKI perp
                    continue
                    
                # CoinAnalyze expects SYMBOL_PERP.A for aggregated perps
                if "_" in line:
                    symbols.append(line)
                else:
                    symbols.append(f"{line}_PERP.A")
                    
    # 2. Merge dynamic scanned symbols from data/scanned_pairs.json (if enabled/exists)
    json_path = config.DEFAULT_DB_DIR / "scanned_pairs.json"
    if json_path.exists():
        try:
            with open(json_path, "r") as f:
                scan_data = json.load(f)
                scanned_symbols = []
                # Add accumulation alerts first (watchlist priorities)
                for item in scan_data.get("accumulation_alerts", []):
                    scanned_symbols.append(item["symbol"])
                # Add top 10 rankings
                for item in scan_data.get("rankings", []):
                    scanned_symbols.append(item["symbol"])
                # Keep the scanner's liquid universe in the research data set,
                # not only the alert-oriented top 10.
                scanned_symbols.extend(scan_data.get("research_universe", []))
                
                # Merge into watchlist (avoid duplicates)
                merged_count = 0
                for s in scanned_symbols:
                    if s not in symbols:
                        symbols.append(s)
                        merged_count += 1
                if merged_count > 0:
                    print(f"Watchlist: Dynamically injected {merged_count} hot symbols from hourly scan.")
        except Exception as e:
            print(f"Error loading scanned pairs: {e}")

    # Rotation candidates are a separate, versioned artifact. A stale or malformed
    # feed never expands the CoinAnalyze research workload.
    rotation_path = config.BINANCE_OI_ROTATION_FEED_PATH
    if rotation_path.exists():
        try:
            with open(rotation_path, "r", encoding="utf-8") as f:
                rotation_feed = json.load(f)
            expires_at = datetime.fromisoformat(rotation_feed["expires_at"])
            if rotation_feed.get("source") == "binance_usdm" and expires_at > datetime.now(timezone.utc):
                from binance_oi_rotation_scanner import coinalyze_symbol_from_binance
                rotation_symbols = [coinalyze_symbol_from_binance(item["symbol"]) for item in rotation_feed.get("candidates", [])]
                added = [symbol for symbol in rotation_symbols if symbol not in symbols]
                symbols.extend(added)
                if added:
                    print(f"Watchlist: Injected {len(added)} fresh Binance OI rotation candidates.")
        except (KeyError, TypeError, ValueError, OSError) as e:
            print(f"Error loading Binance OI rotation feed: {e}")
            
    # Always ensure BTC, ETH, and SOL are in the list
    for default in ["BTCUSDT_PERP.A", "ETHUSDT_PERP.A", "SOLUSDT_PERP.A"]:
        if default not in symbols:
            symbols.append(default)
            
    return symbols



def fetch_coinalyze_data_batched(endpoint: str, params: dict = None, batch_size: int = 30) -> list:
    """Delegates to the professional client (batching is handled inside)."""
    symbols = (params or {}).get("symbols", "").split(",") if params else []
    return _coin_client.fetch_batched(endpoint, symbols, other_params=params, batch_size=batch_size, cutoff_id="ingest")

def get_latest_history_value(history_list: list, key: str, default=0.0) -> float:
    """Safely extracts the last value from a history list by trying multiple key candidates."""
    if not history_list:
        return default
    last_item = history_list[-1]
    
    # If key is 'ratio', check multiple possible keys returned by CoinAnalyze
    if key == "ratio":
        for k in ["c", "value", "ratio", "r"]:
            if k in last_item and last_item[k] is not None:
                return float(last_item[k])
    
    return float(last_item.get(key, default))

def ingest_coinalyze():
    """Ingests current futures/perps market data from CoinAnalyze and stores it in DuckDB."""
    print("Starting CoinAnalyze ingestion...")
    symbols = load_symbols()
    symbols_str = ",".join(symbols)

    # Gap-fill diagnostic (1 query to avoid lock thrash). Only log summary to keep logs clean.
    db_conn = config.get_db_connection(read_only=True)
    try:
        have = {r[0] for r in db_conn.execute(
            "SELECT DISTINCT native_symbol FROM source_observations WHERE source='coinalyze'"
        ).fetchall()}
        gap_count = sum(1 for sym in symbols if sym not in have)
        if gap_count:
            print(f"Gap-fill candidates: {gap_count} (new/missing coverage; backfills handle history)")
    finally:
        db_conn.close()
    
    # Limit ohlcv-history to core assets only (freshness critical path).
    # Avoids hammering the rate-limited endpoint for 90+ symbols every cycle.
    # Core bars (used for cutoffs/materialization) keep max(source_end) advancing.
    core_assets = getattr(config, "OPENMARKET_PERMANENT_ASSETS", ("BTC", "ETH", "SOL"))
    ohlcv_syms = [s for s in symbols if any(s.upper().startswith(a) for a in core_assets)] or symbols[:5]
    ohlcv_str = ",".join(ohlcv_syms)
    
    # 1. Fetch OHLCV FIRST + larger batch for the (now small) set.
    now_epoch = int(time.time())
    from_epoch = now_epoch - 3600 * 2
    ohlcv_data = fetch_coinalyze_data_batched("ohlcv-history", {
        "symbols": ohlcv_str,
        "interval": "15min",
        "from": str(from_epoch),
        "to": str(now_epoch)
    }, batch_size=50)
    
    ohlcv_map = {}
    for item in ohlcv_data:
        sym = item.get("symbol")
        history = item.get("history", [])
        if history:
            last_candle = history[-1]
            raw_ts = last_candle.get("t")
            if raw_ts is not None:
                try:
                    ts_val = float(raw_ts)
                    if ts_val > 1e12:
                        ts_val /= 1000.0
                    candle_ts = datetime.fromtimestamp(ts_val, tz=timezone.utc)
                except (ValueError, TypeError, OSError):
                    candle_ts = datetime.now(timezone.utc)
            else:
                candle_ts = datetime.now(timezone.utc)
            ohlcv_map[sym] = {
                "timestamp": candle_ts,
                "open": float(last_candle.get("o", 0.0)),
                "high": float(last_candle.get("h", 0.0)),
                "low": float(last_candle.get("l", 0.0)),
                "close": float(last_candle.get("c", 0.0)),
                "volume": float(last_candle.get("v", 0.0))
            }

    # 2. Fetch current Open Interest
    oi_data = fetch_coinalyze_data_batched("open-interest", {"symbols": symbols_str}, batch_size=30)
    oi_map = {}
    for item in oi_data:
        sym = item.get("symbol")
        val = item.get("openInterest", item.get("value", 0.0))
        oi_map[sym] = float(val)
        
    # 3. Fetch current Funding Rate (rate limiting handled by CoinAnalyzeClient)
    funding_data = fetch_coinalyze_data_batched("funding-rate", {"symbols": symbols_str}, batch_size=30)
    funding_map = {}
    for item in funding_data:
        sym = item.get("symbol")
        val = item.get("value", 0.0)
        funding_map[sym] = float(val)
        
    # 4. Fetch current Predicted Funding Rate
    pred_funding_data = fetch_coinalyze_data_batched("predicted-funding-rate", {"symbols": symbols_str}, batch_size=30)
    pred_funding_map = {}
    for item in pred_funding_data:
        sym = item.get("symbol")
        val = item.get("value", 0.0)
        pred_funding_map[sym] = float(val)

    # 5. Fetch Liquidation History (larger batch)
    liq_data = fetch_coinalyze_data_batched("liquidation-history", {
        "symbols": symbols_str,
        "interval": "15min",
        "from": str(from_epoch),
        "to": str(now_epoch)
    }, batch_size=40)
    
    liq_map = {}
    for item in liq_data:
        sym = item.get("symbol")
        history = item.get("history", [])
        if history:
            last_liq = history[-1]
            liq_map[sym] = {
                "long": float(last_liq.get("l", 0.0)),
                "short": float(last_liq.get("s", 0.0))
            }

    # 6. Fetch Long/Short Ratio History (larger batch)
    ls_data = fetch_coinalyze_data_batched("long-short-ratio-history", {
        "symbols": symbols_str,
        "interval": "15min",
        "from": str(from_epoch),
        "to": str(now_epoch)
    }, batch_size=40)
    
    ls_map = {}
    for item in ls_data:
        sym = item.get("symbol")
        history = item.get("history", [])
        if history:
            ls_map[sym] = get_latest_history_value(history, "ratio", default=1.0)

    # 7. Write consolidated records to DuckDB
    db_conn = config.get_db_connection(read_only=False)
    try:
        inserted_count = 0
        
        for sym in symbols:
            # Parse underlying asset name (e.g. BTCUSDT_PERP.A -> BTC)
            base_sym = sym.split("_")[0]
            if base_sym.endswith("USDT"):
                underlying = base_sym[:-4]
            elif base_sym.endswith("USD"):
                underlying = base_sym[:-3]
            else:
                underlying = base_sym
                
            # Skip writing if we don't have valid close price or it's 0.0 (no active data)
            ohlcv = ohlcv_map.get(sym)
            if not ohlcv or ohlcv["close"] == 0.0:
                continue
            
            oi = oi_map.get(sym, 0.0)
            fr = funding_map.get(sym, 0.0)
            pfr = pred_funding_map.get(sym, 0.0)
            
            liq = liq_map.get(sym, {"long": 0.0, "short": 0.0})
            ls_ratio = ls_map.get(sym, 1.0)
            
            row_ts = ohlcv.get("timestamp", datetime.now(timezone.utc))
            
            # No longer writing to legacy futures_data (dropped in post-cutover phase).
            # All data goes to source_observations (append-only).

            # Append-only source_observations (v2 platform, per spec)
            obs_id = f"coinalyze:{sym}:{row_ts.isoformat()}"
            payload = {
                "open": ohlcv["open"], "high": ohlcv["high"], "low": ohlcv["low"], "close": ohlcv["close"],
                "volume": ohlcv["volume"], "open_interest": oi, "funding_rate": fr,
                "predicted_funding": pfr, "liquidation_long": liq["long"], "liquidation_short": liq["short"],
                "long_short_ratio": ls_ratio
            }
            db_conn.execute("""
                INSERT OR IGNORE INTO source_observations (
                    observation_id, source, venue, native_symbol, asset, market_kind, interval,
                    source_start, source_end, retrieved_at, retrieval_kind, payload_json
                ) VALUES (?, 'coinalyze', 'aggregate_perp', ?, ?, 'perpetual', '15m', ?, ?, ?, 'live', ?)
            """, (
                obs_id, sym, underlying, row_ts, row_ts, row_ts, json.dumps(payload, default=str)
            ))
            inserted_count += 1
            print(f"CoinAnalyze Ingested {sym}: price={ohlcv['close']}, OI={oi}, FR={fr:.6f}, predicted_FR={pfr:.6f}")
            
        db_conn.commit()
        print(f"CoinAnalyze ingestion completed. Inserted {inserted_count} rows.")
    except Exception as e:
        print(f"Failed to write CoinAnalyze data to DuckDB: {e}")
    finally:
        db_conn.close()

if __name__ == "__main__":
    # Ensure config initializes tables first
    config.init_db()
    ingest_coinalyze()
