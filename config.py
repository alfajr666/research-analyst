import os
import stat
from pathlib import Path
import duckdb
from dotenv import load_dotenv

# Project Paths
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def secure_secret_file(path: str | Path = ENV_FILE) -> None:
    """Restrict a local secret file to its owner before services consume it."""
    secret_file = Path(path)
    if not secret_file.exists():
        return
    if not secret_file.is_file():
        raise ValueError(f"Secret path is not a regular file: {secret_file}")
    mode = stat.S_IMODE(secret_file.stat().st_mode)
    if mode & 0o077:
        secret_file.chmod(0o600)


# Load secrets only after their on-disk permissions are restricted.
secure_secret_file()
load_dotenv(ENV_FILE)
DEFAULT_DB_DIR = BASE_DIR / "data"
DEFAULT_DB_DIR.mkdir(parents=True, exist_ok=True)

# API Keys and Credentials
COINANALYZE_API_KEY = os.getenv("COINANALYZE_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ALLOWED_CHAT_IDS = frozenset(
    value.strip() for value in os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if value.strip()
)
TELEGRAM_ALLOWED_USER_IDS = frozenset(
    value.strip() for value in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if value.strip()
)

# Config Settings
DB_PATH = os.getenv("DB_PATH", str(DEFAULT_DB_DIR / "market_data.db"))
# The publisher owns this database. Keeping its delivery ledger separate lets it
# run while the market-data owner holds DuckDB's single-process write lock.
ALPHA_DB_PATH = os.getenv("ALPHA_DB_PATH", str(DEFAULT_DB_DIR / "alpha_events.db"))
INGEST_INTERVAL_MINS = int(os.getenv("INGEST_INTERVAL_MINS", "15"))
MIN_CONVICTION = os.getenv("MIN_CONVICTION", "LOW")
DAILY_BRIEF_TIME_WITA = os.getenv("DAILY_BRIEF_TIME_WITA", "08:00")
FUTURES_RETENTION_DAYS = int(os.getenv("FUTURES_RETENTION_DAYS", "365"))
SCANNER_MIN_24H_VOLUME_USD = float(os.getenv("SCANNER_MIN_24H_VOLUME_USD", "5000000"))
SCANNER_CORE_24H_VOLUME_USD = float(os.getenv("SCANNER_CORE_24H_VOLUME_USD", "100000000"))
SCANNER_MAX_CONTRACTS = int(os.getenv("SCANNER_MAX_CONTRACTS", "50"))
DISCOVERY_TOP_N = int(os.getenv("DISCOVERY_TOP_N", "10"))
DISCOVERY_MIN_RESIDENCY_HOURS = int(os.getenv("DISCOVERY_MIN_RESIDENCY_HOURS", "24"))
DEEP_BACKFILL_BATCH_SIZE = int(os.getenv("DEEP_BACKFILL_BATCH_SIZE", "5"))
DEEP_BACKFILL_LEASE_MINUTES = int(os.getenv("DEEP_BACKFILL_LEASE_MINUTES", "30"))
DEEP_BACKFILL_RETRY_BASE_MINUTES = int(os.getenv("DEEP_BACKFILL_RETRY_BASE_MINUTES", "5"))
BINANCE_OI_ROTATION_ENABLED = os.getenv("BINANCE_OI_ROTATION_ENABLED", "true").lower() == "true"
BINANCE_OI_ROTATION_SCANNER_VERSION = os.getenv("BINANCE_OI_ROTATION_SCANNER_VERSION", "v1")
BINANCE_OI_ROTATION_MIN_24H_VOLUME_USD = float(os.getenv("BINANCE_OI_ROTATION_MIN_24H_VOLUME_USD", "5000000"))
BINANCE_OI_ROTATION_MAX_CONTRACTS = int(os.getenv("BINANCE_OI_ROTATION_MAX_CONTRACTS", "0"))
BINANCE_OI_ROTATION_HISTORY_HOURS = int(os.getenv("BINANCE_OI_ROTATION_HISTORY_HOURS", "168"))
BINANCE_OI_ROTATION_MIN_OI_DELTA_USD = float(os.getenv("BINANCE_OI_ROTATION_MIN_OI_DELTA_USD", "1000000"))
BINANCE_OI_ROTATION_MIN_OI_PERCENTILE = float(os.getenv("BINANCE_OI_ROTATION_MIN_OI_PERCENTILE", "0.95"))
BINANCE_OI_ROTATION_MIN_VOLUME_ANOMALY = float(os.getenv("BINANCE_OI_ROTATION_MIN_VOLUME_ANOMALY", "1.0"))
BINANCE_OI_ROTATION_WATCHLIST_HOURS = int(os.getenv("BINANCE_OI_ROTATION_WATCHLIST_HOURS", "36"))
BINANCE_OI_ROTATION_FEED_EXPIRY_HOURS = int(os.getenv("BINANCE_OI_ROTATION_FEED_EXPIRY_HOURS", "6"))
BINANCE_OI_ROTATION_FEED_PATH = Path(os.getenv("BINANCE_OI_ROTATION_FEED_PATH", str(DEFAULT_DB_DIR / "binance_oi_rotation_feed.json")))
LLM_RESEARCH_ENABLED = os.getenv("LLM_RESEARCH_ENABLED", "false").lower() == "true"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "20"))
LLM_MAX_REPORTS_PER_CYCLE = int(os.getenv("LLM_MAX_REPORTS_PER_CYCLE", "2"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))
LLM_RETRY_BASE_SECONDS = int(os.getenv("LLM_RETRY_BASE_SECONDS", "60"))
LLM_MAX_INPUT_CHARS = int(os.getenv("LLM_MAX_INPUT_CHARS", "24000"))
LLM_MAX_OUTPUT_CHARS = int(os.getenv("LLM_MAX_OUTPUT_CHARS", "6000"))
LLM_MONTHLY_BUDGET_USD = float(os.getenv("LLM_MONTHLY_BUDGET_USD", "0"))
LLM_INCLUDE_IN_TELEGRAM = os.getenv("LLM_INCLUDE_IN_TELEGRAM", "false").lower() == "true"
LLM_PRICING_VERSION = os.getenv("LLM_PRICING_VERSION", "openai-chat-2026-08-v1")
LLM_INPUT_COST_PER_1K_USD = float(os.getenv("LLM_INPUT_COST_PER_1K_USD", "0"))
LLM_OUTPUT_COST_PER_1K_USD = float(os.getenv("LLM_OUTPUT_COST_PER_1K_USD", "0"))

# Research execution delivery is opt-in per target. Research never receives
# exchange credentials; these paths are only shared inbox directories.
EXECUTION_OUTBOX_DIR = Path(os.getenv("EXECUTION_OUTBOX_DIR", str(DEFAULT_DB_DIR / "execution_outbox")))
EXECUTION_TARGETS = {
    "bybit": {
        "enabled": os.getenv("EXECUTION_BYBIT_ENABLED", "false").lower() == "true",
        "asset_allowlist": frozenset(value.strip().upper() for value in os.getenv("EXECUTION_BYBIT_ASSET_ALLOWLIST", "").split(",") if value.strip()),
    },
    "bybit-test": {
        "enabled": os.getenv("EXECUTION_BYBIT_TEST_ENABLED", "false").lower() == "true",
        "asset_allowlist": frozenset(value.strip().upper() for value in os.getenv("EXECUTION_BYBIT_TEST_ASSET_ALLOWLIST", "").split(",") if value.strip()),
    },
    "mexc": {
        "enabled": os.getenv("EXECUTION_MEXC_ENABLED", "false").lower() == "true",
        "asset_allowlist": frozenset(value.strip().upper() for value in os.getenv("EXECUTION_MEXC_ASSET_ALLOWLIST", "").split(",") if value.strip()),
    },
    "propr": {
        "enabled": os.getenv("EXECUTION_PROPR_ENABLED", "false").lower() == "true",
        "tradeable_assets_path": Path(os.getenv("EXECUTION_PROPR_TRADEABLE_ASSETS_PATH", str(DEFAULT_DB_DIR / "propr_tradeable_assets.json"))),
    },
}


def validate_telegram_allowlist() -> None:
    """Command polling is unsafe unless at least one sender restriction exists."""
    if not TELEGRAM_ALLOWED_CHAT_IDS and not TELEGRAM_ALLOWED_USER_IDS:
        raise ValueError(
            "Telegram command polling requires TELEGRAM_ALLOWED_CHAT_IDS or TELEGRAM_ALLOWED_USER_IDS"
        )

# Freqtrade historical data path (for regime signal module)
FREQTRADE_DATA_DIR = os.getenv(
    "FREQTRADE_DATA_DIR",
    "/home/ubuntu/freqtrade-trading-bot/backtest/pair_trading/freqtrade_cache_91/binanceusdm/futures"
)

# Directory for persisted HMM model pickles
HMM_MODELS_DIR = DEFAULT_DB_DIR / "hmm_models"
HMM_MODELS_DIR.mkdir(parents=True, exist_ok=True)

# API Base URLs
COINANALYZE_BASE_URL = "https://api.coinalyze.net/v1"
DERIBIT_BASE_URL = "https://www.deribit.com/api/v2"
BINANCE_FUTURES_BASE_URL = os.getenv("BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com")

def get_db_connection(read_only: bool = False, db_path: str | Path | None = None):
    """
    Returns a connection to the DuckDB database.
    Note: DuckDB allows only one writer process. If running multiple threads/scripts,
    we must ensure sequential operations or use write locks.
    We implement a retry mechanism to handle transient lock contention.
    """
    import time
    db_file = Path(db_path or DB_PATH)
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


def init_db(db_path: str | Path | None = None):
    """Initializes the database schema if it doesn't exist."""
    conn = get_db_connection(read_only=False, db_path=db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR PRIMARY KEY,
                applied_at TIMESTAMP WITH TIME ZONE NOT NULL
            );
        """)
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
        # An emitted event is represented by its deterministic alpha_id. Rows
        # created before an event is emitted retain their own stable ID and link
        # to the promoted event without changing their identity.
        conn.execute("ALTER TABLE alpha_candidates ADD COLUMN IF NOT EXISTS promoted_alpha_id VARCHAR;")

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

        # Phase 0 event ledger migration. Keeping this DDL here, rather than in
        # SignalPublisher, gives every process the same authoritative schema.
        migration = "2026-08-16-phase0-event-ledger"
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (migration,)
        ).fetchone()
        if applied is None:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alpha_events (
                    dedupe_key VARCHAR PRIMARY KEY,
                    alpha_id VARCHAR NOT NULL,
                    strategy_id VARCHAR NOT NULL,
                    asset VARCHAR NOT NULL,
                    direction VARCHAR NOT NULL,
                    setup_class VARCHAR NOT NULL,
                    phase VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    valid_until TIMESTAMP WITH TIME ZONE NOT NULL,
                    event_json VARCHAR NOT NULL,
                    persisted_at TIMESTAMP WITH TIME ZONE NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signal_deliveries (
                    delivery_id VARCHAR PRIMARY KEY,
                    dedupe_key VARCHAR NOT NULL,
                    channel VARCHAR NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    status VARCHAR NOT NULL,
                    attempted_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    completed_at TIMESTAMP WITH TIME ZONE,
                    next_retry_at TIMESTAMP WITH TIME ZONE,
                    response_body VARCHAR,
                    error_message VARCHAR,
                    UNIQUE(dedupe_key, channel, attempt_number)
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alpha_event_status_history (
                    status_event_id VARCHAR PRIMARY KEY,
                alpha_id VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                recorded_at TIMESTAMP WITH TIME ZONE NOT NULL,
                reason VARCHAR NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_alpha_events_alpha_id ON alpha_events (alpha_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_alpha_event_status_history_alpha_id ON alpha_event_status_history (alpha_id, recorded_at);")
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, CURRENT_TIMESTAMP)", (migration,)
            )

        metrics_migration = "2026-08-16-phase0-operational-metrics"
        metrics_applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (metrics_migration,)
        ).fetchone()
        if metrics_applied is None:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_runs (
                    run_id VARCHAR PRIMARY KEY,
                    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    completed_at TIMESTAMP WITH TIME ZONE,
                    status VARCHAR NOT NULL,
                    data_freshness_seconds DOUBLE,
                    lock_failures INTEGER NOT NULL DEFAULT 0,
                    outbox_depth INTEGER NOT NULL DEFAULT 0,
                    report_queue_age_seconds DOUBLE,
                    error_message VARCHAR,
                    details_json VARCHAR NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started_at ON pipeline_runs (started_at);")
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, CURRENT_TIMESTAMP)", (metrics_migration,)
            )

        execution_migration = "2026-08-17-research-execution-deliveries"
        if conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (execution_migration,)).fetchone() is None:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS execution_deliveries (
                    alpha_id VARCHAR NOT NULL,
                    target VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    reason VARCHAR,
                    inbox_path VARCHAR,
                    written_at TIMESTAMP WITH TIME ZONE,
                    acknowledged_at TIMESTAMP WITH TIME ZONE,
                    bot_trade_id VARCHAR,
                    bot_order_id VARCHAR,
                    PRIMARY KEY (alpha_id, target)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_execution_deliveries_status ON execution_deliveries (status, written_at)")
            conn.execute("INSERT INTO schema_migrations VALUES (?, CURRENT_TIMESTAMP)", (execution_migration,))

        confidence_migration = "2026-08-17-alpha-confidence-observations"
        if conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (confidence_migration,)).fetchone() is None:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alpha_confidence_observations (
                    alpha_id VARCHAR PRIMARY KEY,
                    confidence DOUBLE NOT NULL,
                    components_json VARCHAR,
                    observation_status VARCHAR NOT NULL,
                    reason VARCHAR,
                    observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    recorded_at TIMESTAMP WITH TIME ZONE NOT NULL
                )
            """)
            conn.execute("INSERT INTO schema_migrations VALUES (?, CURRENT_TIMESTAMP)", (confidence_migration,))

        research_migration = "2026-08-16-phase1-research-ledger"
        research_applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (research_migration,)
        ).fetchone()
        if research_applied is None:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS research_requests (
                    request_id VARCHAR PRIMARY KEY, subject_type VARCHAR NOT NULL,
                    subject_id VARCHAR NOT NULL, request_kind VARCHAR NOT NULL,
                    as_of TIMESTAMP WITH TIME ZONE NOT NULL, input_hash VARCHAR NOT NULL,
                    status VARCHAR NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL, started_at TIMESTAMP WITH TIME ZONE,
                     completed_at TIMESTAMP WITH TIME ZONE, next_attempt_at TIMESTAMP WITH TIME ZONE,
                     error_code VARCHAR, error_message VARCHAR, request_input_json VARCHAR,
                    UNIQUE(subject_type, subject_id, request_kind, input_hash)
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS research_artifacts (
                    artifact_id VARCHAR PRIMARY KEY, request_id VARCHAR NOT NULL,
                    schema_version INTEGER NOT NULL, model_provider VARCHAR NOT NULL,
                    model_id VARCHAR NOT NULL, prompt_version VARCHAR NOT NULL,
                    generated_at TIMESTAMP WITH TIME ZONE NOT NULL, verdict VARCHAR NOT NULL,
                    report_json VARCHAR NOT NULL, input_json VARCHAR NOT NULL,
                    provider_usage_json VARCHAR, FOREIGN KEY (request_id) REFERENCES research_requests(request_id)
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS research_evidence (
                    evidence_id VARCHAR PRIMARY KEY, artifact_id VARCHAR NOT NULL,
                    source_type VARCHAR NOT NULL, source_ref VARCHAR NOT NULL,
                    observed_at TIMESTAMP WITH TIME ZONE, retrieved_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    excerpt VARCHAR NOT NULL, FOREIGN KEY (artifact_id) REFERENCES research_artifacts(artifact_id)
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_research_requests_pending ON research_requests (status, created_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_research_artifacts_request ON research_artifacts (request_id, generated_at);")
            conn.execute("INSERT INTO schema_migrations VALUES (?, CURRENT_TIMESTAMP)", (research_migration,))

        research_workflow_migration = "2026-08-16-phase4-research-workflow"
        if conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (research_workflow_migration,)).fetchone() is None:
            conn.execute("ALTER TABLE research_requests ADD COLUMN IF NOT EXISTS request_input_json VARCHAR;")
            conn.execute("INSERT INTO schema_migrations VALUES (?, CURRENT_TIMESTAMP)", (research_workflow_migration,))

        research_metrics_migration = "2026-08-16-phase3-research-metrics"
        if conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (research_metrics_migration,)).fetchone() is None:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS research_run_metrics (
                    metric_id VARCHAR PRIMARY KEY, recorded_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    queue_depth INTEGER NOT NULL, oldest_pending_seconds DOUBLE,
                    monthly_cost_usd DOUBLE NOT NULL, completed_count INTEGER NOT NULL,
                    rejected_count INTEGER NOT NULL, latency_seconds DOUBLE,
                    oldest_report_seconds DOUBLE
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_research_run_metrics_recorded ON research_run_metrics (recorded_at);")
            conn.execute("INSERT INTO schema_migrations VALUES (?, CURRENT_TIMESTAMP)", (research_metrics_migration,))

        research_metrics_upgrade = "2026-08-16-phase3-research-metrics-v2"
        if conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (research_metrics_upgrade,)).fetchone() is None:
            conn.execute("ALTER TABLE research_run_metrics ADD COLUMN IF NOT EXISTS oldest_report_seconds DOUBLE;")
            conn.execute("INSERT INTO schema_migrations VALUES (?, CURRENT_TIMESTAMP)", (research_metrics_upgrade,))

        # Append-only hourly broad-universe observations used to reproduce each
        # discovery decision, including contracts that were not selected.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS broad_discovery_snapshots (
                observed_at             TIMESTAMP WITH TIME ZONE,
                symbol                  VARCHAR,
                asset                   VARCHAR,
                liquidity_tier          VARCHAR,
                is_eligible             BOOLEAN,
                data_fresh              BOOLEAN,
                history_warmed          BOOLEAN,
                volume_24h_usd          DOUBLE,
                open_interest_usd       DOUBLE,
                volume_zscore           DOUBLE,
                oi_change_1h            DOUBLE,
                price_change_1h         DOUBLE,
                price_change_24h        DOUBLE,
                price_range_percentile  DOUBLE,
                funding_rate            DOUBLE,
                funding_zscore          DOUBLE,
                long_short_ratio_change DOUBLE,
                fresh_breakout          BOOLEAN,
                post_breakout_pullback  BOOLEAN,
                exhausted_expansion     BOOLEAN,
                ignition_score          DOUBLE,
                continuation_score      DOUBLE,
                ignition_rank           INTEGER,
                continuation_rank       INTEGER,
                PRIMARY KEY (observed_at, symbol)
            );
        """)

        # Watchlist rows are events, never mutable state. The latest row for a
        # pool/symbol is its current state; prior qualification remains auditable.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discovery_watchlist_history (
                event_id                 VARCHAR PRIMARY KEY,
                observed_at              TIMESTAMP WITH TIME ZONE,
                pool                     VARCHAR,
                symbol                   VARCHAR,
                asset                    VARCHAR,
                state                    VARCHAR,
                rank                     INTEGER,
                score                    DOUBLE,
                entered_at               TIMESTAMP WITH TIME ZONE,
                deep_backfill_required   BOOLEAN,
                expiry_reason            VARCHAR
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_discovery_snapshot_ts ON broad_discovery_snapshots (observed_at, symbol);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_discovery_watchlist_ts ON discovery_watchlist_history (pool, symbol, observed_at);")

        # Mutable execution state for durable deep-history bootstrap work. The
        # append-only watchlist table remains the audit record of qualification.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deep_backfill_jobs (
                symbol        VARCHAR PRIMARY KEY,
                status        VARCHAR NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
                attempts      INTEGER NOT NULL DEFAULT 0,
                next_retry_at TIMESTAMP WITH TIME ZONE NOT NULL,
                last_error    VARCHAR,
                created_at    TIMESTAMP WITH TIME ZONE NOT NULL,
                updated_at    TIMESTAMP WITH TIME ZONE NOT NULL,
                started_at    TIMESTAMP WITH TIME ZONE,
                completed_at  TIMESTAMP WITH TIME ZONE
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deep_backfill_due ON deep_backfill_jobs (status, next_retry_at);")

        # Binance-native, immutable observations and qualifying OI rotation events.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS binance_oi_rotation_observations (
                source VARCHAR, completed_interval_at TIMESTAMP WITH TIME ZONE,
                scanner_version VARCHAR, symbol VARCHAR, asset VARCHAR,
                quote VARCHAR, contract_type VARCHAR, is_eligible BOOLEAN,
                rejection_reason VARCHAR, volume_24h_usd DOUBLE,
                open_interest_usd DOUBLE, oi_change_1h_pct DOUBLE,
                oi_change_1h_usd DOUBLE, price_change_1h DOUBLE,
                volume_1h_usd DOUBLE, volume_anomaly DOUBLE,
                oi_spike_percentile DOUBLE, observed_at TIMESTAMP WITH TIME ZONE,
                PRIMARY KEY (source, completed_interval_at, scanner_version, symbol)
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS binance_oi_rotation_events (
                source VARCHAR, asset VARCHAR, completed_interval_at TIMESTAMP WITH TIME ZONE,
                scanner_version VARCHAR, symbol VARCHAR, rank INTEGER,
                metrics_json VARCHAR, observed_at TIMESTAMP WITH TIME ZONE,
                PRIMARY KEY (source, asset, completed_interval_at, scanner_version)
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS binance_oi_rotation_watchlist_history (
                source VARCHAR, asset VARCHAR, symbol VARCHAR,
                observed_at TIMESTAMP WITH TIME ZONE, state VARCHAR,
                expires_at TIMESTAMP WITH TIME ZONE, deep_backfill_required BOOLEAN,
                overlap_annotated BOOLEAN, PRIMARY KEY (source, asset, observed_at)
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_binance_oi_rotation_observations_ts ON binance_oi_rotation_observations (completed_interval_at, symbol);")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS binance_oi_rotation_raw_oi_history (
                source VARCHAR, symbol VARCHAR, observed_at TIMESTAMP WITH TIME ZONE,
                open_interest_usd DOUBLE, completed_interval_at TIMESTAMP WITH TIME ZONE,
                PRIMARY KEY (source, symbol, observed_at)
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_binance_oi_rotation_raw_oi_history_interval ON binance_oi_rotation_raw_oi_history (completed_interval_at, symbol);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_binance_oi_rotation_watchlist_ts ON binance_oi_rotation_watchlist_history (asset, observed_at);")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS binance_oi_rotation_scans (
                source VARCHAR, completed_interval_at TIMESTAMP WITH TIME ZONE,
                scanner_version VARCHAR, status VARCHAR, completed_at TIMESTAMP WITH TIME ZONE,
                PRIMARY KEY (source, completed_interval_at, scanner_version)
            );
        """)

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
