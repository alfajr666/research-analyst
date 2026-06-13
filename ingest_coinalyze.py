import time
import httpx
import duckdb
from datetime import datetime, timezone
import config

def load_symbols() -> list:
    """Loads symbols from symbols-for-dual-zone.md and formats them for CoinAnalyze."""
    symbols_path = config.BASE_DIR / "symbols-for-dual-zone.md"
    if not symbols_path.exists():
        return ["BTCUSDT_PERP.A", "ETHUSDT_PERP.A"]
        
    symbols = []
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
                
    # Always ensure BTC, ETH, and SOL are in the list
    for default in ["BTCUSDT_PERP.A", "ETHUSDT_PERP.A", "SOLUSDT_PERP.A"]:
        if default not in symbols:
            symbols.append(default)
            
    return symbols



def fetch_coinalyze_data(endpoint: str, params: dict = None, client: httpx.Client = None) -> list:
    """Fetches data from a CoinAnalyze endpoint with rate-limit and error handling."""
    if not config.COINANALYZE_API_KEY:
        print("Warning: COINANALYZE_API_KEY is not configured in .env. Skipping CoinAnalyze ingestion.")
        return []
    
    url = f"{config.COINANALYZE_BASE_URL}/{endpoint}"
    query_params = params.copy() if params else {}
    query_params["api_key"] = config.COINANALYZE_API_KEY
    
    headers = {
        "Accept": "application/json"
    }
    
    retries = 3
    for attempt in range(retries):
        try:
            if client is not None:
                response = client.get(url, params=query_params, headers=headers, timeout=15.0)
            else:
                response = httpx.get(url, params=query_params, headers=headers, timeout=15.0)
                
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:
                retry_after = int(float(response.headers.get("Retry-After", 5)))
                print(f"CoinAnalyze Rate limit (429) hit on {endpoint}. Sleeping for {retry_after} seconds...")
                time.sleep(retry_after)
            else:
                print(f"Error fetching from CoinAnalyze {endpoint}: {response.status_code} - {response.text}")
                return []
        except Exception as e:
            print(f"Exception during request to {endpoint}: {e}")
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
            else:
                return []
    return []

def fetch_coinalyze_data_batched(endpoint: str, params: dict = None, client: httpx.Client = None, batch_size: int = 15) -> list:
    """Fetches data from a CoinAnalyze endpoint in batches of symbols to prevent 429 errors."""
    if not config.COINANALYZE_API_KEY:
        print("Warning: COINANALYZE_API_KEY is not configured in .env. Skipping CoinAnalyze ingestion.")
        return []
        
    symbols_param = params.get("symbols", "") if params else ""
    if not symbols_param:
        return fetch_coinalyze_data(endpoint, params, client)
        
    symbols_list = symbols_param.split(",")
    combined_result = []
    
    for i in range(0, len(symbols_list), batch_size):
        batch = symbols_list[i:i + batch_size]
        batch_str = ",".join(batch)
        
        batch_params = params.copy() if params else {}
        batch_params["symbols"] = batch_str
        
        res = fetch_coinalyze_data(endpoint, batch_params, client)
        if res:
            if isinstance(res, list):
                combined_result.extend(res)
            else:
                print(f"Warning: expected list from {endpoint}, got {type(res)}")
                
        # Sleep 1.5 seconds between batches to stay within rate limits
        if i + batch_size < len(symbols_list):
            time.sleep(1.5)
            
    return combined_result

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
    
    with httpx.Client() as client:
        # 1. Fetch current Open Interest
        oi_data = fetch_coinalyze_data_batched("open-interest", {"symbols": symbols_str}, client=client)
        oi_map = {}
        for item in oi_data:
            sym = item.get("symbol")
            val = item.get("openInterest", item.get("value", 0.0))
            oi_map[sym] = float(val)
            
        # 2. Fetch current Funding Rate (sleep 2.0s first to avoid rate limit)
        time.sleep(2.0)
        funding_data = fetch_coinalyze_data_batched("funding-rate", {"symbols": symbols_str}, client=client)
        funding_map = {}
        for item in funding_data:
            sym = item.get("symbol")
            val = item.get("value", 0.0)
            funding_map[sym] = float(val)
            
        # 3. Fetch current Predicted Funding Rate (sleep 2.0s first)
        time.sleep(2.0)
        pred_funding_data = fetch_coinalyze_data_batched("predicted-funding-rate", {"symbols": symbols_str}, client=client)
        pred_funding_map = {}
        for item in pred_funding_data:
            sym = item.get("symbol")
            val = item.get("value", 0.0)
            pred_funding_map[sym] = float(val)
    
        # 4. Fetch OHLCV History (sleep 2.0s first)
        time.sleep(2.0)
        now_epoch = int(time.time())
        from_epoch = now_epoch - 3600 * 2  # last 2 hours
        ohlcv_data = fetch_coinalyze_data_batched("ohlcv-history", {
            "symbols": symbols_str,
            "interval": "15min",
            "from": str(from_epoch),
            "to": str(now_epoch)
        }, client=client)
        
        ohlcv_map = {}
        for item in ohlcv_data:
            sym = item.get("symbol")
            history = item.get("history", [])
            if history:
                last_candle = history[-1]
                ohlcv_map[sym] = {
                    "open": float(last_candle.get("o", 0.0)),
                    "high": float(last_candle.get("h", 0.0)),
                    "low": float(last_candle.get("l", 0.0)),
                    "close": float(last_candle.get("c", 0.0)),
                    "volume": float(last_candle.get("v", 0.0))
                }
    
        # 5. Fetch Liquidation History (sleep 2.0s first)
        time.sleep(2.0)
        liq_data = fetch_coinalyze_data_batched("liquidation-history", {
            "symbols": symbols_str,
            "interval": "15min",
            "from": str(from_epoch),
            "to": str(now_epoch)
        }, client=client)
        
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
    
        # 6. Fetch Long/Short Ratio History (sleep 2.0s first)
        time.sleep(2.0)
        ls_data = fetch_coinalyze_data_batched("long-short-ratio-history", {
            "symbols": symbols_str,
            "interval": "15min",
            "from": str(from_epoch),
            "to": str(now_epoch)
        }, client=client)
        
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
        current_time = datetime.now(timezone.utc)
        
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
            
            db_conn.execute("""
                INSERT INTO futures_data (
                    timestamp, underlying, symbol, open_interest, funding_rate, predicted_funding,
                    liquidation_long, liquidation_short, long_short_ratio,
                    open, high, low, close, volume
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                current_time, underlying, sym, oi, fr, pfr,
                liq["long"], liq["short"], ls_ratio,
                ohlcv["open"], ohlcv["high"], ohlcv["low"], ohlcv["close"], ohlcv["volume"]
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
