import time
import httpx
import duckdb
from datetime import datetime, timezone, date
import config

def get_historical_dvol(currency: str, start_days_ago: int = 90) -> list:
    """
    Fetches historical daily DVOL index data for BTC or ETH.
    Returns a list of tuples: (date_str, close_value)
    """
    # DVOL is fetched from Deribit API
    url = f"{config.DERIBIT_BASE_URL}/public/get_volatility_index_data"
    
    end_ts = int(time.time()) * 1000
    start_ts = int(time.time() - start_days_ago * 86400) * 1000
    
    # Try different resolution formats defensively
    resolutions = ["D", "1D", "1d"]
    data = None
    
    for res in resolutions:
        params = {
            "currency": currency,
            "resolution": res,
            "start_timestamp": start_ts,
            "end_timestamp": end_ts
        }
        try:
            print(f"Requesting DVOL history for {currency} with resolution '{res}'...")
            response = httpx.get(url, params=params, timeout=15.0)
            if response.status_code == 200:
                res_json = response.json()
                if "result" in res_json and "data" in res_json["result"]:
                    data = res_json["result"]["data"]
                    print(f"Successfully fetched {len(data)} DVOL data points for {currency} using resolution '{res}'")
                    break
            else:
                print(f"Failed to fetch DVOL index with resolution '{res}': {response.status_code} - {response.text}")
        except Exception as e:
            print(f"Exception during fetching DVOL for resolution '{res}': {e}")
            
    if not data:
        print(f"Error: Could not retrieve DVOL history for {currency} using any resolution.")
        return []
        
    records = []
    for candle in data:
        # Candle is typically [timestamp, open, high, low, close]
        if len(candle) >= 5:
            ts = candle[0]
            close_val = float(candle[4])
            # Convert millisecond timestamp to date object
            dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).date()
            records.append((dt, close_val))
            
    return records

def backfill():
    """Bootstraps the daily_options_summary table in DuckDB with historical DVOL data."""
    print("Starting historical DVOL backfill for IV Rank calculation...")
    
    # Initialize the database and schemas
    config.init_db()
    
    conn = config.get_db_connection(read_only=False)
    try:
        for currency in ["BTC", "ETH"]:
            records = get_historical_dvol(currency, start_days_ago=90)
            if not records:
                print(f"No historical records found for {currency}.")
                continue
                
            inserted_count = 0
            for dt, dvol_val in records:
                # Insert or update: Since DuckDB doesn't support ON CONFLICT easily in older versions,
                # we can delete the record if it exists, then insert, or use INSERT OR REPLACE.
                # DuckDB supports INSERT OR REPLACE INTO.
                conn.execute("""
                    INSERT OR REPLACE INTO daily_options_summary (
                        date, underlying, atm_iv, put_call_ratio, skew_25d, open_interest, volume
                    ) VALUES (?, ?, ?, NULL, NULL, NULL, NULL)
                """, (dt, currency, dvol_val))
                inserted_count += 1
                
            print(f"Backfilled {inserted_count} daily records for {currency}.")
            
        conn.commit()
        print("Historical backfill completed successfully.")
    except Exception as e:
        print(f"Exception during backfill database operation: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    backfill()
