import time
import httpx
import duckdb
from datetime import datetime, timezone, timedelta
import config

def fetch_deribit_data(endpoint: str, params: dict = None, client: httpx.Client = None) -> dict:
    """Fetches data from Deribit public REST API."""
    url = f"{config.DERIBIT_BASE_URL}/public/{endpoint}"
    retries = 3
    for attempt in range(retries):
        try:
            if client is not None:
                response = client.get(url, params=params, timeout=15.0)
            else:
                response = httpx.get(url, params=params, timeout=15.0)
                
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:
                print("Deribit Rate limit (429) hit. Sleeping for 2 seconds...")
                time.sleep(2.0)
            else:
                print(f"Error fetching from Deribit {endpoint}: {response.status_code} - {response.text}")
                return {}
        except Exception as e:
            print(f"Exception during Deribit request to {endpoint}: {e}")
            if attempt < retries - 1:
                time.sleep(2.0)
            else:
                return {}
    return {}

def get_spot_price(currency: str, client: httpx.Client = None) -> float:
    """Fetches the current index price for the given currency (BTC or ETH)."""
    index_name = f"{currency.lower()}_usd"
    res = fetch_deribit_data("get_index_price", {"index_name": index_name}, client=client)
    if res and "result" in res:
        return float(res["result"].get("index_price", 0.0))
    return 0.0

def parse_deribit_expiry(expiry_str: str) -> datetime:
    """Parses Deribit date string (e.g., '26JUN26' or '9JUN26') to a datetime object."""
    try:
        # format is DDMMMYY (e.g. 26JUN26)
        return datetime.strptime(expiry_str, "%d%b%y").replace(tzinfo=timezone.utc)
    except Exception as e:
        print(f"Failed to parse expiry date '{expiry_str}': {e}")
        return None

def parse_instrument(instrument_name: str):
    """
    Parses instrument name into underlying, expiry, strike, option_type.
    Example: BTC-26JUN26-70000-C -> BTC, datetime, 70000.0, C
    """
    parts = instrument_name.split("-")
    if len(parts) != 4:
        return None, None, None, None
    underlying = parts[0]
    expiry_dt = parse_deribit_expiry(parts[1])
    try:
        strike = float(parts[2])
    except ValueError:
        strike = 0.0
    option_type = parts[3]
    return underlying, expiry_dt, strike, option_type

def ingest_deribit():
    """Ingests options data from Deribit and writes to DuckDB."""
    print("Starting Deribit options ingestion...")
    
    now = datetime.now(timezone.utc)
    max_expiry_date = now + timedelta(days=60)
    
    # We will accumulate option metrics to write to DuckDB
    option_records = []
    
    with httpx.Client() as client:
        # Get current index prices
        spot_prices = {
            "BTC": get_spot_price("BTC", client=client),
            "ETH": get_spot_price("ETH", client=client),
            "SOL": get_spot_price("SOL", client=client)
        }
        
        print(f"Index Prices: BTC={spot_prices['BTC']}, ETH={spot_prices['ETH']}, SOL={spot_prices['SOL']}")
        
        for currency in ["BTC", "ETH", "SOL"]:
            spot = spot_prices[currency]
            if spot == 0.0:
                print(f"Could not retrieve spot price for {currency}. Skipping options chain.")
                continue
                
            print(f"Fetching options book summary for {currency}...")
            summary_res = fetch_deribit_data("get_book_summary_by_currency", {
                "currency": currency,
                "kind": "option"
            }, client=client)
            
            if not summary_res or "result" not in summary_res:
                print(f"Empty summary returned for {currency}.")
                continue
                
            raw_options = summary_res["result"]
            print(f"Found {len(raw_options)} raw options for {currency}. Filtering...")
            
            # Filter options on client-side before requesting tickers (to prevent rate limits)
            filtered_raw = []
            for opt in raw_options:
                name = opt.get("instrument_name", "")
                underlying, expiry, strike, opt_type = parse_instrument(name)
                
                if not underlying or not expiry or strike == 0.0:
                    continue
                    
                # Expiry filter: within 60 days
                if expiry > max_expiry_date:
                    continue
                    
                # Strike filter: within ±20% of current spot price (captures all options with delta >= 0.05)
                if not (spot * 0.80 <= strike <= spot * 1.20):
                    continue
                    
                filtered_raw.append((opt, expiry, strike, opt_type))
                
            print(f"Filtered to {len(filtered_raw)} instruments within 60d expiry and ±35% strike. Fetching tickers for Greeks...")
            
            # Fetch tickers for filtered instruments
            count = 0
            for opt_dict, expiry, strike, opt_type in filtered_raw:
                name = opt_dict["instrument_name"]
                
                # Fetch ticker for Greeks
                ticker_res = fetch_deribit_data("ticker", {"instrument_name": name}, client=client)
                if not ticker_res or "result" not in ticker_res:
                    continue
                    
                result = ticker_res["result"]
                greeks = result.get("greeks", {})
                
                # Delta filter: absolute delta >= 0.05
                delta = greeks.get("delta")
                if delta is None:
                    continue
                    
                if abs(float(delta)) < 0.05:
                    continue
                    
                # Extra metrics
                mark_price = float(result.get("mark_price", 0.0))
                mark_iv = float(result.get("mark_iv", 0.0))
                open_interest = float(result.get("open_interest", 0.0))
                
                # Use 24h volume from book summary if not in ticker
                volume = float(result.get("stats", {}).get("volume", opt_dict.get("volume", 0.0)))
                
                gamma = float(greeks.get("gamma", 0.0))
                vega = float(greeks.get("vega", 0.0))
                theta = float(greeks.get("theta", 0.0))
                
                record = (
                    now, currency, name, expiry, strike, opt_type,
                    mark_price, mark_iv, open_interest, volume,
                    float(delta), gamma, vega, theta
                )
                option_records.append(record)
                
                count += 1
                # Rate limit mitigation: sleep 50ms between requests
                time.sleep(0.05)
                
            print(f"Successfully processed {count} tickers for {currency}.")

    # Write records to DuckDB
    if option_records:
        db_conn = config.get_db_connection(read_only=False)
        try:
            db_conn.executemany("""
                INSERT INTO option_chains (
                    timestamp, underlying, instrument_name, expiry, strike, option_type,
                    mark_price, mark_iv, open_interest, volume, delta, gamma, vega, theta
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, option_records)
            db_conn.commit()
            print(f"Deribit options ingestion completed. Inserted {len(option_records)} rows into option_chains.")
        except Exception as e:
            print(f"Failed to write options to DuckDB: {e}")
        finally:
            db_conn.close()
    else:
        print("No option records matched delta >= 0.05 filters. No data written.")

if __name__ == "__main__":
    config.init_db()
    ingest_deribit()
