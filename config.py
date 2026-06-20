import os
from pathlib import Path
import duckdb
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# Project Paths
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_DIR = BASE_DIR / "data"
DEFAULT_DB_DIR.mkdir(parents=True, exist_ok=True)

# API Keys and Credentials
COINANALYZE_API_KEY = os.getenv("COINANALYZE_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Config Settings
DB_PATH = os.getenv("DB_PATH", str(DEFAULT_DB_DIR / "market_data.db"))
INGEST_INTERVAL_MINS = int(os.getenv("INGEST_INTERVAL_MINS", "15"))
DAILY_BRIEF_TIME_WITA = os.getenv("DAILY_BRIEF_TIME_WITA", "08:00")

# API Base URLs
COINANALYZE_BASE_URL = "https://api.coinalyze.net/v1"
DERIBIT_BASE_URL = "https://www.deribit.com/api/v2"

def get_db_connection(read_only: bool = False):
    """
    Returns a connection to the DuckDB database.
    Note: DuckDB allows only one writer process. If running multiple threads/scripts,
    we must ensure sequential operations or use write locks.
    """
    db_file = Path(DB_PATH)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_file), read_only=read_only)
    conn.execute("PRAGMA memory_limit='128MB';")
    conn.execute("PRAGMA threads=2;")
    return conn

def init_db():
    """Initializes the database schema if it doesn't exist."""
    conn = get_db_connection(read_only=False)
    try:
        # Create futures_data table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS futures_data (
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                underlying VARCHAR,
                symbol VARCHAR,
                open_interest DOUBLE,
                funding_rate DOUBLE,
                predicted_funding DOUBLE,
                liquidation_long DOUBLE,
                liquidation_short DOUBLE,
                long_short_ratio DOUBLE,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                close DOUBLE,
                volume DOUBLE
            );
        """)

        # Create option_chains table (15-min snapshots)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS option_chains (
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                underlying VARCHAR,
                instrument_name VARCHAR,
                expiry TIMESTAMP,
                strike DOUBLE,
                option_type VARCHAR,
                mark_price DOUBLE,
                mark_iv DOUBLE,
                open_interest DOUBLE,
                volume DOUBLE,
                delta DOUBLE,
                gamma DOUBLE,
                vega DOUBLE,
                theta DOUBLE
            );
        """)

        # Create daily_options_summary table (for IV Rank & daily stats)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_options_summary (
                date DATE PRIMARY KEY,
                underlying VARCHAR,
                atm_iv DOUBLE,
                put_call_ratio DOUBLE,
                skew_25d DOUBLE,
                open_interest DOUBLE,
                volume DOUBLE
            );
        """)

        # Create brain_outputs table (brain module: previous tags + last one-liner per underlying)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS brain_outputs (
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                underlying VARCHAR,
                tags_json VARCHAR,
                summary_line VARCHAR
            );
        """)

        # Create confluence_alerts table for alert deduplication/cooldown
        conn.execute("""
            CREATE TABLE IF NOT EXISTS confluence_alerts (
                underlying VARCHAR,
                alert_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                price DOUBLE,
                poc DOUBLE,
                ema26 DOUBLE,
                ema99 DOUBLE,
                val DOUBLE,
                vah DOUBLE,
                hvns VARCHAR,
                lvns VARCHAR,
                PRIMARY KEY (underlying, alert_time)
            );
        """)

        # Migration: add columns if upgrading from old schema
        for col in ["val DOUBLE", "vah DOUBLE", "hvns VARCHAR", "lvns VARCHAR"]:
            conn.execute(f"ALTER TABLE confluence_alerts ADD COLUMN IF NOT EXISTS {col};")

        # Create scanner_history table for hourly rotating volume/OI scanner
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scanner_history (
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                rank INTEGER,
                underlying VARCHAR,
                symbol VARCHAR,
                volume_7d_usd DOUBLE,
                open_interest_usd DOUBLE,
                vol_to_oi_ratio DOUBLE,
                volume_spike_multiple DOUBLE,
                price_change_24h DOUBLE,
                is_accumulating BOOLEAN,
                PRIMARY KEY (timestamp, symbol)
            );
        """)

        # Migration: add new columns for scanner schema updates
        conn.execute("ALTER TABLE scanner_history ADD COLUMN IF NOT EXISTS price_change_1h DOUBLE;")
        
        # Create an index on timestamp/underlying for fast analysis
        conn.execute("CREATE INDEX IF NOT EXISTS idx_futures_ts ON futures_data (timestamp, underlying);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_options_ts ON option_chains (timestamp, underlying);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_brain_ts ON brain_outputs (timestamp, underlying);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_ts ON confluence_alerts (alert_time, underlying);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scanner_ts ON scanner_history (timestamp, symbol);")
        
        conn.commit()
    finally:
        conn.close()

if __name__ == "__main__":
    print(f"Initializing database at {DB_PATH}...")
    init_db()
    print("Database initialized successfully.")
