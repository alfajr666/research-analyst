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
MIN_CONVICTION = os.getenv("MIN_CONVICTION", "LOW")
DAILY_BRIEF_TIME_WITA = os.getenv("DAILY_BRIEF_TIME_WITA", "08:00")
FUTURES_RETENTION_DAYS = int(os.getenv("FUTURES_RETENTION_DAYS", "365"))
SCANNER_MIN_24H_VOLUME_USD = float(os.getenv("SCANNER_MIN_24H_VOLUME_USD", "5000000"))
SCANNER_CORE_24H_VOLUME_USD = float(os.getenv("SCANNER_CORE_24H_VOLUME_USD", "100000000"))
SCANNER_MAX_CONTRACTS = int(os.getenv("SCANNER_MAX_CONTRACTS", "50"))

# Freqtrade historical data path (for regime signal module)
FREQTRADE_DATA_DIR = os.getenv(
    "FREQTRADE_DATA_DIR",
    "/home/gilang/Documents/Project/freqtrade-trading-bot/freqtrade/user_data/data/binanceusdm/futures"
)

# Directory for persisted HMM model pickles
HMM_MODELS_DIR = DEFAULT_DB_DIR / "hmm_models"
HMM_MODELS_DIR.mkdir(parents=True, exist_ok=True)

# API Base URLs
COINANALYZE_BASE_URL = "https://api.coinalyze.net/v1"
DERIBIT_BASE_URL = "https://www.deribit.com/api/v2"

def get_db_connection(read_only: bool = False):
    """
    Returns a connection to the DuckDB database.
    Note: DuckDB allows only one writer process. If running multiple threads/scripts,
    we must ensure sequential operations or use write locks.
    We implement a retry mechanism to handle transient lock contention.
    """
    import time
    db_file = Path(DB_PATH)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    
    max_retries = 10
    retry_delay = 2.0
    for attempt in range(max_retries):
        try:
            conn = duckdb.connect(str(db_file), read_only=read_only)
            conn.execute("PRAGMA memory_limit='128MB';")
            conn.execute("PRAGMA threads=2;")
            return conn
        except duckdb.Error as e:
            if attempt == max_retries - 1:
                raise e
            print(f"Database connection attempt {attempt + 1} failed (locked/busy). Retrying in {retry_delay}s... Error: {e}")
            time.sleep(retry_delay)


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

        # Point-in-time scanner universe for leakage-free liquidity-tier research.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS universe_snapshots (
                observed_at       TIMESTAMP WITH TIME ZONE,
                binance_symbol    VARCHAR,
                coinalyze_symbol  VARCHAR,
                underlying        VARCHAR,
                volume_24h_usd    DOUBLE,
                last_price        DOUBLE,
                liquidity_tier    VARCHAR,
                selected_for_scan BOOLEAN,
                PRIMARY KEY (observed_at, binance_symbol)
            );
        """)

        # Immutable research candidates, including candidates that never trigger.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alpha_candidates (
                candidate_id       VARCHAR PRIMARY KEY,
                observed_at        TIMESTAMP WITH TIME ZONE,
                asset              VARCHAR,
                source_symbol      VARCHAR,
                direction          VARCHAR,
                setup_class        VARCHAR,
                phase              VARCHAR,
                strategy_id        VARCHAR,
                liquidity_tier     VARCHAR,
                status             VARCHAR,
                valid_until        TIMESTAMP WITH TIME ZONE,
                entry_condition    VARCHAR,
                invalidation_price DOUBLE,
                targets            VARCHAR,
                feature_snapshot   VARCHAR
            );
        """)

        # Outcomes are separate from candidates so point-in-time inputs stay immutable.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alpha_outcomes (
                candidate_id             VARCHAR PRIMARY KEY,
                evaluated_at             TIMESTAMP WITH TIME ZONE,
                entry_at                 TIMESTAMP WITH TIME ZONE,
                entry_price              DOUBLE,
                outcome                  VARCHAR,
                expiry_at                TIMESTAMP WITH TIME ZONE,
                return_15m               DOUBLE,
                return_1h                DOUBLE,
                return_4h                DOUBLE,
                max_favorable_excursion  DOUBLE,
                max_adverse_excursion    DOUBLE,
                estimated_cost           DOUBLE,
                net_return               DOUBLE,
                details                  VARCHAR,
                FOREIGN KEY (candidate_id) REFERENCES alpha_candidates(candidate_id)
            );
        """)
        
        # Create an index on timestamp/underlying for fast analysis
        conn.execute("CREATE INDEX IF NOT EXISTS idx_futures_ts ON futures_data (timestamp, underlying);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_options_ts ON option_chains (timestamp, underlying);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_brain_ts ON brain_outputs (timestamp, underlying);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_ts ON confluence_alerts (alert_time, underlying);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scanner_ts ON scanner_history (timestamp, symbol);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_universe_ts ON universe_snapshots (observed_at, binance_symbol);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_ts ON alpha_candidates (observed_at, setup_class);")

        # Create regime_signals table (HMM + dual VWAP daily signals)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS regime_signals (
                date             DATE,
                underlying       VARCHAR,
                signal           VARCHAR,
                no_signal_reason VARCHAR,
                conviction       VARCHAR,
                conviction_score INTEGER,
                regime           VARCHAR,
                regime_conf      DOUBLE,
                weekly_vwap      DOUBLE,
                monthly_vwap     DOUBLE,
                ema12            DOUBLE,
                ema25            DOUBLE,
                ema_aligned      BOOLEAN,
                acceptance       INTEGER,
                close_price      DOUBLE,
                sl               DOUBLE,
                tp1              DOUBLE,
                tp2              DOUBLE,
                PRIMARY KEY (date, underlying)
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_regime_date ON regime_signals (date, underlying);")
        
        # Migration: add sl, tp1, tp2 columns if table already exists
        for col in ["sl DOUBLE", "tp1 DOUBLE", "tp2 DOUBLE"]:
            conn.execute(f"ALTER TABLE regime_signals ADD COLUMN IF NOT EXISTS {col};")
        
        conn.commit()
    finally:
        conn.close()

if __name__ == "__main__":
    print(f"Initializing database at {DB_PATH}...")
    init_db()
    print("Database initialized successfully.")
