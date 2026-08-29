import json
import os
import stat
from pathlib import Path
from typing import List
import sqlite3
from dotenv import load_dotenv


# Project Paths. Runtime data and secrets live at repository root, not beside code.
BASE_DIR = Path(__file__).resolve().parents[2]
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
# Discord incoming webhooks (optional). Empty URL disables that stream.
DISCORD_ALPHA_WEBHOOK_URL = os.getenv("DISCORD_ALPHA_WEBHOOK_URL", "")
DISCORD_OI_WEBHOOK_URL = os.getenv("DISCORD_OI_WEBHOOK_URL", "")
RAW_SIGNAL_DISCORD_BATCH_ENABLED = os.getenv("RAW_SIGNAL_DISCORD_BATCH_ENABLED", "false").lower() == "true"
RAW_SIGNAL_DISCORD_BATCH_MINUTES = int(os.getenv("RAW_SIGNAL_DISCORD_BATCH_MINUTES", "30"))
RAW_SIGNAL_DISCORD_WEBHOOK_URL = os.getenv("RAW_SIGNAL_DISCORD_WEBHOOK_URL", DISCORD_ALPHA_WEBHOOK_URL)
RAW_BATCH_CLAIM_LEASE_SECONDS = int(os.getenv("RAW_BATCH_CLAIM_LEASE_SECONDS", "120"))
RAW_BATCH_MAX_ATTEMPTS = int(os.getenv("RAW_BATCH_MAX_ATTEMPTS", "5"))
if RAW_SIGNAL_DISCORD_BATCH_MINUTES <= 0 or 60 % RAW_SIGNAL_DISCORD_BATCH_MINUTES:
    raise ValueError("RAW_SIGNAL_DISCORD_BATCH_MINUTES must be a positive divisor of 60")
BINANCE_OI_DISCORD_TOP_N = int(os.getenv("BINANCE_OI_DISCORD_TOP_N", "5"))
BINANCE_OI_DISCORD_MULTI_HOUR_WINDOW = int(os.getenv("BINANCE_OI_DISCORD_MULTI_HOUR_WINDOW", "6"))
BINANCE_OI_DISCORD_SKIP_EMPTY = os.getenv("BINANCE_OI_DISCORD_SKIP_EMPTY", "true").lower() == "true"

# Config Settings
MARKET_DB_PATH = os.getenv("MARKET_DB_PATH", str(DEFAULT_DB_DIR / "market.sqlite3"))
ANALYST_DB_PATH = os.getenv("ANALYST_DB_PATH", str(DEFAULT_DB_DIR / "analyst.sqlite3"))
INGEST_INTERVAL_MINS = int(os.getenv("INGEST_INTERVAL_MINS", "5"))
EVALUATION_TRIGGER_DIR = Path(os.getenv("EVALUATION_TRIGGER_DIR", str(DEFAULT_DB_DIR / "evaluation_triggers")))
EVALUATION_RECOVERY_SCAN_SECONDS = int(os.getenv("EVALUATION_RECOVERY_SCAN_SECONDS", "5"))
EVALUATION_LEASE_SECONDS = int(os.getenv("EVALUATION_LEASE_SECONDS", "600"))
EVALUATION_MAX_RETRIES = int(os.getenv("EVALUATION_MAX_RETRIES", "5"))
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
BINANCE_OI_DB_PATH = os.getenv("BINANCE_OI_DB_PATH", str(DEFAULT_DB_DIR / "binance_oi.db"))

# Tables are deliberately classified here, at the schema boundary.  Startup
# must never repair a database by creating tables owned by the other service.
MARKET_SCHEMA_TABLES = frozenset({
    "option_chains", "daily_options_summary", "brain_outputs", "confluence_alerts",
    "scanner_history", "universe_snapshots", "broad_discovery_snapshots",
    "discovery_watchlist_history", "deep_backfill_jobs", "regime_signals",
    "source_observations", "source_request_log",
})
ANALYST_SCHEMA_TABLES = frozenset({
    "plugin_states", "positions_feed", "pm_advice", "alpha_candidates", "alpha_outcomes",
    "alpha_events", "signal_deliveries", "alpha_event_status_history",
    "alpha_confidence_observations", "research_requests", "research_reports",
    "research_run_metrics", "research_artifacts", "research_evidence", "pipeline_runs",
    "execution_deliveries", "cutoff_runs", "feature_snapshots", "structure_zones",
})


# ADR-013 / retention: hard prune of aged research tables (worker-owned)
BINANCE_OI_PRUNE_ENABLED = os.getenv("BINANCE_OI_PRUNE_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off", "",
)
BINANCE_OI_WATCHLIST_HISTORY_RETENTION_DAYS = int(
    os.getenv("BINANCE_OI_WATCHLIST_HISTORY_RETENTION_DAYS", "14")
)
BINANCE_OI_OBSERVATIONS_RETENTION_DAYS = int(
    os.getenv("BINANCE_OI_OBSERVATIONS_RETENTION_DAYS", "30")
)
BINANCE_OI_RAW_OI_RETENTION_DAYS = int(os.getenv("BINANCE_OI_RAW_OI_RETENTION_DAYS", "30"))
BINANCE_OI_EVENTS_RETENTION_DAYS = int(os.getenv("BINANCE_OI_EVENTS_RETENTION_DAYS", "90"))
BINANCE_OI_SCANS_RETENTION_DAYS = int(os.getenv("BINANCE_OI_SCANS_RETENTION_DAYS", "30"))
# P1: skip entered/active membership for forever-static bases
BINANCE_OI_STATIC_MEMBERSHIP_SKIP = os.getenv(
    "BINANCE_OI_STATIC_MEMBERSHIP_SKIP", "1"
).strip().lower() not in ("0", "false", "no", "off", "")
BINANCE_OI_STATIC_SEED_PATH = os.getenv("BINANCE_OI_STATIC_SEED_PATH", "").strip()

# 10m/15m liquid-tier fast path (additive cadence; see specs/binance-oi-rotation-10m-fast-path.md)
BINANCE_OI_10M_ENABLED = os.getenv("BINANCE_OI_10M_ENABLED", "true").lower() == "true"
BINANCE_OI_10M_BAR_MINUTES = int(os.getenv("BINANCE_OI_10M_BAR_MINUTES", "15"))
BINANCE_OI_10M_MIN_24H_VOLUME_USD = float(os.getenv("BINANCE_OI_10M_MIN_24H_VOLUME_USD", "5000000"))
BINANCE_OI_10M_MAX_CONTRACTS = int(os.getenv("BINANCE_OI_10M_MAX_CONTRACTS", "100"))
BINANCE_OI_10M_MIN_OI_DELTA_USD = float(os.getenv("BINANCE_OI_10M_MIN_OI_DELTA_USD", "250000"))
BINANCE_OI_10M_MIN_OI_PERCENTILE = float(os.getenv("BINANCE_OI_10M_MIN_OI_PERCENTILE", "0.95"))
BINANCE_OI_10M_MIN_VOLUME_ANOMALY = float(os.getenv("BINANCE_OI_10M_MIN_VOLUME_ANOMALY", "1.0"))
BINANCE_OI_10M_HISTORY_BARS = int(os.getenv("BINANCE_OI_10M_HISTORY_BARS", "672"))  # ~7d @15m
BINANCE_OI_10M_DISCORD_ENABLED = os.getenv("BINANCE_OI_10M_DISCORD_ENABLED", "true").lower() == "true"
BINANCE_OI_10M_FEED_MERGE_HOURLY = os.getenv("BINANCE_OI_10M_FEED_MERGE_HOURLY", "true").lower() == "true"

# Static agreed symbol universe from the approved tradeable-assets snapshot.
# Persisted in the repo at symbols/static_universe.json so it is version-controlled and
# survives restarts/prunes. Canonical bases (e.g. BTC); expand to XUSDT perps at load time.
STATIC_SYMBOLS_PATH = os.getenv("STATIC_SYMBOLS_PATH", str(BASE_DIR / "symbols" / "static_universe.json"))
STATIC_SYMBOLS_OVERRIDE = os.getenv("STATIC_SYMBOLS", "").strip()
# Universe mode for WS/eval: "static" (approved list only), "rotated" (rotation feed only),
# "both" (static + rotated union).
WS_SYMBOL_SOURCE = os.getenv("WS_SYMBOL_SOURCE", "static").strip().lower()
# WS provider toggles. Bybit is the default public source; Binance is opt-in/off.
WS_BYBIT_ENABLED = os.getenv("WS_BYBIT_ENABLED", "true").lower() == "true"
WS_BINANCE_ENABLED = os.getenv("WS_BINANCE_ENABLED", "false").lower() == "true"
COINANALYZE_EVAL_ENABLED = os.getenv("COINANALYZE_EVAL_ENABLED", "false").lower() == "true"
COMPACT_STRATEGY_ASSETS = frozenset(("BTC", "ETH", "PAXG", "QQQ"))
COMPACT_STRATEGY_IDS = frozenset((
    "failed-break-v3", "bb-rsi-meanrev-v1",
    "williams-fractal-scalp-v1", "ema9-continuation-stochrsi-v1",
))
DUAL_ZONE_STRATEGY_ID = "dual-zone-follower-v1"
DUAL_ZONE_EXIT_EMA_LENGTH = int(os.getenv("DUAL_ZONE_EXIT_EMA_LENGTH", "7"))
DUAL_ZONE_ANCHOR_EMA_LENGTH = int(os.getenv("DUAL_ZONE_ANCHOR_EMA_LENGTH", "26"))
DUAL_ZONE_TREND_EMA_LENGTH = int(os.getenv("DUAL_ZONE_TREND_EMA_LENGTH", "99"))
DUAL_ZONE_A_ENTRY_DISTANCE_PCT = float(os.getenv("DUAL_ZONE_A_ENTRY_DISTANCE_PCT", "1.0"))
DUAL_ZONE_A_TARGET_DISTANCE_PCT = float(os.getenv("DUAL_ZONE_A_TARGET_DISTANCE_PCT", "3.0"))
DUAL_ZONE_A_STOP_DISTANCE_PCT = float(os.getenv("DUAL_ZONE_A_STOP_DISTANCE_PCT", "1.0"))
DUAL_ZONE_B_ENTRY_DISTANCE_PCT = float(os.getenv("DUAL_ZONE_B_ENTRY_DISTANCE_PCT", "1.5"))
DUAL_ZONE_B_TARGET_DISTANCE_PCT = float(os.getenv("DUAL_ZONE_B_TARGET_DISTANCE_PCT", "5.0"))
DUAL_ZONE_B_STOP_DISTANCE_PCT = float(os.getenv("DUAL_ZONE_B_STOP_DISTANCE_PCT", "1.0"))
DUAL_ZONE_SHORT_STRATEGY_ID = "dual-zone-short-follower-v1"
DUAL_ZONE_SHORT_A_ENTRY_DISTANCE_PCT = float(os.getenv("DUAL_ZONE_SHORT_A_ENTRY_DISTANCE_PCT", "1.0"))
DUAL_ZONE_SHORT_A_TARGET_DISTANCE_PCT = float(os.getenv("DUAL_ZONE_SHORT_A_TARGET_DISTANCE_PCT", "3.0"))
DUAL_ZONE_SHORT_A_STOP_DISTANCE_PCT = float(os.getenv("DUAL_ZONE_SHORT_A_STOP_DISTANCE_PCT", "1.0"))
DUAL_ZONE_SHORT_B_ENTRY_DISTANCE_PCT = float(os.getenv("DUAL_ZONE_SHORT_B_ENTRY_DISTANCE_PCT", "1.5"))
DUAL_ZONE_SHORT_B_TARGET_DISTANCE_PCT = float(os.getenv("DUAL_ZONE_SHORT_B_TARGET_DISTANCE_PCT", "5.0"))
DUAL_ZONE_SHORT_B_STOP_DISTANCE_PCT = float(os.getenv("DUAL_ZONE_SHORT_B_STOP_DISTANCE_PCT", "1.0"))
WS_STREAM_TIMEFRAMES = os.getenv("WS_STREAM_TIMEFRAMES", "1m,5m").strip().lower().split(",")
WS_MARKPRICE_ENABLED = os.getenv("WS_MARKPRICE_ENABLED", "true").lower() == "true"
# Shard size for Bybit (per-connection topic cap). Binance uses one combined conn.
WS_BYBIT_SHARD = int(os.getenv("WS_BYBIT_SHARD", "20"))
WS_BACKFILL_HOURS = int(os.getenv("WS_BACKFILL_HOURS", "6"))
# Source names stamped on native bars (purity = "pure_ws", accepted by emit gate).
BYBIT_WS_SOURCE = "bybit_ws"
BINANCE_WS_SOURCE = "binance_ws"
WS_DATA_PURITY = "pure_ws"


def load_static_symbols() -> List[str]:
    """Return canonical base symbols for the static universe (uppercased)."""
    import json as _json
    if STATIC_SYMBOLS_OVERRIDE:
        return [s.strip().upper() for s in STATIC_SYMBOLS_OVERRIDE.split(",") if s.strip()]
    p = Path(STATIC_SYMBOLS_PATH)
    if p.exists():
        try:
            data = _json.loads(p.read_text())
            syms = data.get("symbols") or data.get("crypto_static") or []
            return [str(s).upper() for s in syms]
        except Exception:
            return []
    return []


def expand_perp_symbols(bases: List[str], venue: str = "bybit") -> List[str]:
    """Expand canonical bases to perp contract symbols per venue.

    bybit: BTC -> BTCUSDT (linear USDT perp). binance: BTC -> BTCUSDT.
    """
    if venue == "binance":
        return [f"{b}USDT" for b in bases]
    return [f"{b}USDT" for b in bases]


def init_binance_oi_db(db_path: str | Path | None = None):
    """Initialize the Binance OI rotation tables in a dedicated DB file.
    This separates it from the main market DB to reduce lock contention.
    Supports dual cadence (1h + 10m/15m) by including bar_minutes in identity keys.
    """
    target = str(db_path or BINANCE_OI_DB_PATH)
    conn = get_db_connection(read_only=False, db_path=target)
    try:
        # New schema (with bar_minutes in relevant PKs)
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
                bar_minutes INTEGER DEFAULT 60,
                PRIMARY KEY (source, completed_interval_at, scanner_version, symbol, bar_minutes)
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS binance_oi_rotation_events (
                source VARCHAR, asset VARCHAR, completed_interval_at TIMESTAMP WITH TIME ZONE,
                scanner_version VARCHAR, symbol VARCHAR, rank INTEGER,
                metrics_json VARCHAR, observed_at TIMESTAMP WITH TIME ZONE,
                bar_minutes INTEGER DEFAULT 60,
                PRIMARY KEY (source, asset, completed_interval_at, scanner_version, bar_minutes)
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
                bar_minutes INTEGER DEFAULT 60,
                PRIMARY KEY (source, symbol, observed_at)
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_binance_oi_rotation_raw_oi_history_interval ON binance_oi_rotation_raw_oi_history (completed_interval_at, symbol);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_binance_oi_rotation_watchlist_ts ON binance_oi_rotation_watchlist_history (asset, observed_at);")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS binance_oi_rotation_scans (
                source VARCHAR, completed_interval_at TIMESTAMP WITH TIME ZONE,
                scanner_version VARCHAR, status VARCHAR, completed_at TIMESTAMP WITH TIME ZONE,
                bar_minutes INTEGER DEFAULT 60,
                PRIMARY KEY (source, completed_interval_at, scanner_version, bar_minutes)
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_oi_deliveries (
                delivery_key VARCHAR PRIMARY KEY,
                kind VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                attempted_at TIMESTAMP WITH TIME ZONE NOT NULL,
                completed_at TIMESTAMP WITH TIME ZONE,
                response_body VARCHAR,
                error_message VARCHAR
            );
        """)

        # One-time migration for pre-10m DBs: add column + backfill + recreate tables with widened PK to avoid cadence collisions at :00 boundaries.
        def _has_column(table: str, col: str) -> bool:
            try:
                info = conn.execute(f"PRAGMA table_info({table})").fetchall()
                return any(r[1] == col for r in info)
            except Exception:
                return False

        needs_migrate = (
            not _has_column("binance_oi_rotation_scans", "bar_minutes")
            or not _has_column("binance_oi_rotation_observations", "bar_minutes")
            or not _has_column("binance_oi_rotation_events", "bar_minutes")
        )
        if needs_migrate:
            # Add columns where missing (for raw too)
            for tbl in ("binance_oi_rotation_observations", "binance_oi_rotation_events", "binance_oi_rotation_scans", "binance_oi_rotation_raw_oi_history"):
                if not _has_column(tbl, "bar_minutes"):
                    try:
                        conn.execute(f"ALTER TABLE {tbl} ADD COLUMN bar_minutes INTEGER DEFAULT 60;")
                    except Exception:
                        pass
            # Backfill legacy rows
            for tbl in ("binance_oi_rotation_observations", "binance_oi_rotation_events", "binance_oi_rotation_scans", "binance_oi_rotation_raw_oi_history"):
                try:
                    conn.execute(f"UPDATE {tbl} SET bar_minutes = 60 WHERE bar_minutes IS NULL;")
                except Exception:
                    pass

            # Recreate core identity tables with bar_minutes inside PK so 15m@12:00 and 60m@12:00 coexist.
            # We copy data; drop old after.
            for (old_name, create_sql, copy_sql) in [
                ("binance_oi_rotation_scans",
                 """CREATE TABLE binance_oi_rotation_scans_new (
                        source VARCHAR, completed_interval_at TIMESTAMP WITH TIME ZONE,
                        scanner_version VARCHAR, status VARCHAR, completed_at TIMESTAMP WITH TIME ZONE,
                        bar_minutes INTEGER DEFAULT 60,
                        PRIMARY KEY (source, completed_interval_at, scanner_version, bar_minutes)
                    )""",
                 """INSERT INTO binance_oi_rotation_scans_new SELECT source, completed_interval_at, scanner_version, status, completed_at, COALESCE(bar_minutes,60) FROM binance_oi_rotation_scans"""),
                ("binance_oi_rotation_observations",
                 """CREATE TABLE binance_oi_rotation_observations_new (
                        source VARCHAR, completed_interval_at TIMESTAMP WITH TIME ZONE,
                        scanner_version VARCHAR, symbol VARCHAR, asset VARCHAR,
                        quote VARCHAR, contract_type VARCHAR, is_eligible BOOLEAN,
                        rejection_reason VARCHAR, volume_24h_usd DOUBLE,
                        open_interest_usd DOUBLE, oi_change_1h_pct DOUBLE,
                        oi_change_1h_usd DOUBLE, price_change_1h DOUBLE,
                        volume_1h_usd DOUBLE, volume_anomaly DOUBLE,
                        oi_spike_percentile DOUBLE, observed_at TIMESTAMP WITH TIME ZONE,
                        bar_minutes INTEGER DEFAULT 60,
                        PRIMARY KEY (source, completed_interval_at, scanner_version, symbol, bar_minutes)
                    )""",
                 """INSERT INTO binance_oi_rotation_observations_new SELECT source, completed_interval_at, scanner_version, symbol, asset, quote, contract_type, is_eligible, rejection_reason, volume_24h_usd, open_interest_usd, oi_change_1h_pct, oi_change_1h_usd, price_change_1h, volume_1h_usd, volume_anomaly, oi_spike_percentile, observed_at, COALESCE(bar_minutes,60) FROM binance_oi_rotation_observations"""),
                ("binance_oi_rotation_events",
                 """CREATE TABLE binance_oi_rotation_events_new (
                        source VARCHAR, asset VARCHAR, completed_interval_at TIMESTAMP WITH TIME ZONE,
                        scanner_version VARCHAR, symbol VARCHAR, rank INTEGER,
                        metrics_json VARCHAR, observed_at TIMESTAMP WITH TIME ZONE,
                        bar_minutes INTEGER DEFAULT 60,
                        PRIMARY KEY (source, asset, completed_interval_at, scanner_version, bar_minutes)
                    )""",
                 """INSERT INTO binance_oi_rotation_events_new SELECT source, asset, completed_interval_at, scanner_version, symbol, rank, metrics_json, observed_at, COALESCE(bar_minutes,60) FROM binance_oi_rotation_events"""),
            ]:
                try:
                    conn.execute(create_sql)
                    conn.execute(copy_sql)
                    conn.execute(f"DROP TABLE {old_name}")
                    conn.execute(f"ALTER TABLE {old_name}_new RENAME TO {old_name}")
                except Exception as e:
                    # If anything fails, leave tables as-is (new columns added); worst case same-ts different-bar may need manual.
                    print(f"OI schema migrate partial for {old_name}: {e}")

            # Recreate indexes after possible recreate
            conn.execute("CREATE INDEX IF NOT EXISTS idx_binance_oi_rotation_observations_ts ON binance_oi_rotation_observations (completed_interval_at, symbol);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_binance_oi_rotation_raw_oi_history_interval ON binance_oi_rotation_raw_oi_history (completed_interval_at, symbol);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_binance_oi_rotation_watchlist_ts ON binance_oi_rotation_watchlist_history (asset, observed_at);")
            conn.commit()
        conn.commit()
    finally:
        conn.close()

STRATEGY_ENABLED_IDS = tuple(
    s.strip() for s in os.getenv(
        "STRATEGY_ENABLED_IDS",
        "failed-break-v3,bb-rsi-meanrev-v1,williams-fractal-scalp-v1,"
        "ema9-continuation-stochrsi-v1"
    ).split(",") if s.strip()
)

# Evaluation intervals the active strategy plugins run on. 1m/5m are streamed
# directly by ws_gateway; 15m is resampled from 5m. HTF (1h/4h) remains an
# enrichment layer only (resampled, not evaluated standalone).
EVAL_INTERVALS = [s.strip() for s in os.getenv("EVAL_INTERVALS", "1m,5m,15m").split(",") if s.strip()]

# Runtime active/inactive toggle (phase 6). Empty => all enabled strategies are
# active. Set to an explicit allowlist to override (e.g. "accumulation-base-v2,
# impulse-ignition-v2"). The `plugin_states` table can also override per-strategy
# at runtime without a restart.
STRATEGY_ACTIVE_IDS = tuple(
    s.strip() for s in os.getenv("STRATEGY_ACTIVE_IDS", "").split(",") if s.strip()
)

# Phase 7: LLM position-management sidecar (emit-only). Enabled for this deployment.
PM_SIDECAR_ENABLED = os.getenv("PM_SIDECAR_ENABLED", "true").lower() in ("1", "true", "yes", "on")
PM_CADENCE_MINUTES = int(os.getenv("PM_CADENCE_MINUTES", "5"))
PM_LLM_TIMEOUT_S = int(os.getenv("PM_LLM_TIMEOUT_S", "20"))
PM_LLM_RETRIES = int(os.getenv("PM_LLM_RETRIES", "1"))
PM_REASON_MAX_CHARS = int(os.getenv("PM_REASON_MAX_CHARS", "120"))

# Phase 8: tiered prune retention, days per interval. <=0 disables that tier.
# 1m is high-volume/low-value -> short; 5m/15m medium; HTF (1h/4h) long.
PRUNE_1M_DAYS = int(os.getenv("PRUNE_1M_DAYS", "7"))
PRUNE_5M_DAYS = int(os.getenv("PRUNE_5M_DAYS", "30"))
PRUNE_15M_DAYS = int(os.getenv("PRUNE_15M_DAYS", "90"))
PRUNE_1H_DAYS = int(os.getenv("PRUNE_1H_DAYS", "365"))
PRUNE_4H_DAYS = int(os.getenv("PRUNE_4H_DAYS", "365"))
PRUNE_INTERVAL_DAYS = {
    "1m": PRUNE_1M_DAYS, "5m": PRUNE_5M_DAYS, "15m": PRUNE_15M_DAYS,
    "1h": PRUNE_1H_DAYS, "4h": PRUNE_4H_DAYS,
}

# Phase 9: rotation feed (exports active binance_oi_rotation members to the WS
# universe feed). Disabled by default; also requires WS_SYMBOL_SOURCE=rotated|both.
ROTATION_FEED_ENABLED = os.getenv("ROTATION_FEED_ENABLED", "false").lower() in ("1", "true", "yes", "on")

# Trade-intent outbox → bybit-executor (see bybit-executor/AGENTS.md "Trade Intent
# Contract", schema_version 1). The internal alpha event is the advisory record
# (Discord); this envelope is the executor-consumable intent.
#
# Shared handoff: the analyst WRITES to INTENT_INBOX and the executor READS the same
# directory. The executor's own default is <bybit-executor>/data/intents, so set
# BYBIT_EXECUTOR_DIR to the executor repo root and INTENT_INBOX resolves there with no
# guessing. If the executor overrides its INTENT_INBOX, set this to the same absolute
# path. Deliberately OFF by default (INTENT_DELIVERY_ENABLED).
BYBIT_EXECUTOR_DIR = os.getenv("BYBIT_EXECUTOR_DIR", "")
if BYBIT_EXECUTOR_DIR:
    _default_intent_inbox = Path(BYBIT_EXECUTOR_DIR) / "data" / "intents"
else:
    _default_intent_inbox = DEFAULT_DB_DIR / "intent_outbox"
INTENT_DELIVERY_ENABLED = os.getenv("INTENT_DELIVERY_ENABLED", "false").lower() in ("1", "true", "yes", "on")
INTENT_INBOX = Path(os.getenv("INTENT_INBOX", str(_default_intent_inbox)))
INTENT_SOURCE = os.getenv("INTENT_SOURCE", "research-analyst")
INTENT_EXCHANGE_ID = os.getenv("INTENT_EXCHANGE_ID", "bybit")
INTENT_ACCOUNT_ID = os.getenv("INTENT_ACCOUNT_ID", "hyro")
INTENT_TAKE_PROFIT_MODE = os.getenv("INTENT_TAKE_PROFIT_MODE", "fixed_full_close")
INTENT_VALIDITY_MINUTES = int(os.getenv("INTENT_VALIDITY_MINUTES", "5"))
INTENT_MIN_RR = float(os.getenv("INTENT_MIN_RR", "2.0"))
INTENT_MIN_STOP_DISTANCE_PCT = float(os.getenv("INTENT_MIN_STOP_DISTANCE_PCT", "0.001"))
INTENT_MAX_STOP_DISTANCE_PCT = float(os.getenv("INTENT_MAX_STOP_DISTANCE_PCT", "0.05"))
DATA_FRESHNESS_MAX_SECONDS = float(os.getenv("DATA_FRESHNESS_MAX_SECONDS", "600"))
CLASH_MIN_SCORE_MARGIN = float(os.getenv("CLASH_MIN_SCORE_MARGIN", "2.0"))
STRATEGY_PRIORITY = {}
INTENT_MAX_STOP_DISTANCE_PCT = float(os.getenv("INTENT_MAX_STOP_DISTANCE_PCT", "0.05"))
# Per-strategy routing to executor profiles (exchange/account). JSON map keyed by
# strategy_id; each value may override any of: exchange_id, account_id, source,
# take_profit_mode, validity_minutes. Strategies not listed fall back to
# the INTENT_* defaults above. Compact strategies are always forced to the
# deployment's Hyro Bybit account by intent_outbox.
_INTENT_ROUTING_RAW = os.getenv("INTENT_ROUTING", "{}")
try:
    INTENT_ROUTING = json.loads(_INTENT_ROUTING_RAW) if isinstance(_INTENT_ROUTING_RAW, str) else _INTENT_ROUTING_RAW
    if not isinstance(INTENT_ROUTING, dict):
        INTENT_ROUTING = {}
except (ValueError, TypeError):
    INTENT_ROUTING = {}
INTENT_ROUTING.setdefault("dual-zone-follower-v1", {"exchange_id": "bybit", "account_id": "fundamo"})
INTENT_ROUTING.setdefault("dual-zone-short-follower-v1", {"exchange_id": "bybit", "account_id": "fundamo"})

# PM sidecar <-> bybit-executor handoff (the executor's PM Decision Contract).
# EXECUTOR_SNAPSHOT_DIR: where the executor writes its 1m position snapshots
#   (<dir>/<exchange_id>/<account_id>/latest.json). When set, the PM sidecar reads
#   OPEN/PENDING positions from there instead of the local positions_feed table.
# EXECUTOR_DECISION_DIR: the executor's POSITION_DECISION_DIR — the PM sidecar
#   writes one PMDecision file (<decision_id>.json) per advice there. If unset, the
#   sidecar stays DB-only (no executor delivery).
if BYBIT_EXECUTOR_DIR:
    _default_exec_snapshots = Path(BYBIT_EXECUTOR_DIR) / "data" / "position-snapshots"
    _default_exec_decisions = Path(BYBIT_EXECUTOR_DIR) / "data" / "position-decisions"
else:
    _default_exec_snapshots = ""
    _default_exec_decisions = ""
EXECUTOR_SNAPSHOT_DIR = os.getenv("EXECUTOR_SNAPSHOT_DIR", str(_default_exec_snapshots)) if _default_exec_snapshots else os.getenv("EXECUTOR_SNAPSHOT_DIR", "")
EXECUTOR_DECISION_DIR = os.getenv("EXECUTOR_DECISION_DIR", str(_default_exec_decisions)) if _default_exec_decisions else os.getenv("EXECUTOR_DECISION_DIR", "")
PM_REDUCE_FRACTION = float(os.getenv("PM_REDUCE_FRACTION", "0.5"))
PM_DECISION_VALIDITY_MINUTES = int(os.getenv("PM_DECISION_VALIDITY_MINUTES", "30"))

# accumulation-base-v2 knobs (specs/strategy-accumulation-base-v2.md)
# Defaults grilled 2026-08-18 — independent prefixes; tighter coil / emit floor.
ACC_V2_N = int(os.getenv("ACC_V2_N", "12"))
ACC_V2_K = float(os.getenv("ACC_V2_K", "2.0"))
ACC_V2_G = float(os.getenv("ACC_V2_G", "0.25"))
ACC_V2_D_MAX = float(os.getenv("ACC_V2_D_MAX", "0.50"))
ACC_V2_R_MAX = float(os.getenv("ACC_V2_R_MAX", "2.5"))
ACC_V2_S_MIN = float(os.getenv("ACC_V2_S_MIN", "0.55"))
ACC_V2_N_TOP = int(os.getenv("ACC_V2_N_TOP", "3"))

# impulse-ignition-v2 knobs (specs/strategy-impulse-ignition-v2.md)
IGN_V2_N = int(os.getenv("IGN_V2_N", "12"))
IGN_V2_K = float(os.getenv("IGN_V2_K", "2.0"))
IGN_V2_P = int(os.getenv("IGN_V2_P", "20"))
IGN_V2_C_RATIO = float(os.getenv("IGN_V2_C_RATIO", "0.85"))
IGN_V2_G = float(os.getenv("IGN_V2_G", "0.25"))
IGN_V2_E = float(os.getenv("IGN_V2_E", "0.35"))
IGN_V2_R_MAX = float(os.getenv("IGN_V2_R_MAX", "2.5"))
IGN_V2_S_MIN = float(os.getenv("IGN_V2_S_MIN", "0.55"))
IGN_V2_N_TOP = int(os.getenv("IGN_V2_N_TOP", "3"))

# continuation-breakout-v2 knobs (specs/strategy-continuation-breakout-v2.md)
CONT_V2_P = int(os.getenv("CONT_V2_P", "12"))
CONT_V2_T_MIN = float(os.getenv("CONT_V2_T_MIN", "1.0"))
CONT_V2_N = int(os.getenv("CONT_V2_N", "12"))
CONT_V2_K = float(os.getenv("CONT_V2_K", "2.0"))
CONT_V2_RETR_MAX = float(os.getenv("CONT_V2_RETR_MAX", "0.40"))
CONT_V2_G = float(os.getenv("CONT_V2_G", "0.25"))
CONT_V2_E = float(os.getenv("CONT_V2_E", "0.35"))
CONT_V2_X_BARS = int(os.getenv("CONT_V2_X_BARS", "96"))
CONT_V2_X_MAX = float(os.getenv("CONT_V2_X_MAX", "3.0"))
CONT_V2_R_MAX = float(os.getenv("CONT_V2_R_MAX", "2.5"))
CONT_V2_S_MIN = float(os.getenv("CONT_V2_S_MIN", "0.55"))
CONT_V2_N_TOP = int(os.getenv("CONT_V2_N_TOP", "3"))
# early | balanced | confirmed — snapshot/config only; not part of strategy_id
CONT_V2_WEIGHT_PROFILE = os.getenv("CONT_V2_WEIGHT_PROFILE", "balanced")

# rsi-reclaim-v1 knobs (specs/strategy-rsi-reclaim-v1.md)
RSI_RECLAIM_EMA_FAST = int(os.getenv("RSI_RECLAIM_EMA_FAST", "20"))
RSI_RECLAIM_EMA_MID = int(os.getenv("RSI_RECLAIM_EMA_MID", "50"))
RSI_RECLAIM_RSI_LEN = int(os.getenv("RSI_RECLAIM_RSI_LEN", "14"))
RSI_RECLAIM_RSI_MAX = float(os.getenv("RSI_RECLAIM_RSI_MAX", "45.0"))
RSI_RECLAIM_RSI_MIN = float(os.getenv("RSI_RECLAIM_RSI_MIN", "55.0"))
RSI_RECLAIM_PULLBACK_TOL = float(os.getenv("RSI_RECLAIM_PULLBACK_TOL", "0.0008"))
RSI_RECLAIM_BODY_ATR_MIN = float(os.getenv("RSI_RECLAIM_BODY_ATR_MIN", "0.20"))
RSI_RECLAIM_SEP_MIN = float(os.getenv("RSI_RECLAIM_SEP_MIN", "0.003"))
RSI_RECLAIM_SEP_MAX = float(os.getenv("RSI_RECLAIM_SEP_MAX", "0.04"))
RSI_RECLAIM_R_MAX = float(os.getenv("RSI_RECLAIM_R_MAX", "2.5"))
RSI_RECLAIM_S_MIN = float(os.getenv("RSI_RECLAIM_S_MIN", "0.55"))
RSI_RECLAIM_N_TOP = int(os.getenv("RSI_RECLAIM_N_TOP", "3"))

# liquidity-sweep-reversal-v1 (LSR) — per specs/strategy-liquidity-sweep-reversal-v1.md
# All LSR_V1_* are opt-in via STRATEGY_ENABLED_IDS
LSR_V1_S_MIN = float(os.getenv("LSR_V1_S_MIN", "0.55"))
LSR_V1_N_TOP = int(os.getenv("LSR_V1_N_TOP", "3"))
LSR_V1_R_MAX = float(os.getenv("LSR_V1_R_MAX", "3.0"))
LSR_V1_SWEEP_MIN_ATR = float(os.getenv("LSR_V1_SWEEP_MIN_ATR", "0.10"))
LSR_V1_SWEEP_MAX_ATR = float(os.getenv("LSR_V1_SWEEP_MAX_ATR", "1.00"))
LSR_V1_STOP_ATR_BUF = float(os.getenv("LSR_V1_STOP_ATR_BUF", "0.15"))
LSR_V1_RETRACE_PCT = float(os.getenv("LSR_V1_RETRACE_PCT", "0.50"))
LSR_V1_BOS_WINDOW = int(os.getenv("LSR_V1_BOS_WINDOW", "8"))
LSR_V1_ENTRY_HORIZON_MIN = int(os.getenv("LSR_V1_ENTRY_HORIZON_MIN", "120"))
LSR_V1_TARGET_R = float(os.getenv("LSR_V1_TARGET_R", "2.0"))
LSR_V1_REQUIRE_DISPLACEMENT = os.getenv("LSR_V1_REQUIRE_DISPLACEMENT", "false").lower() == "true"
LSR_V1_REQUIRE_CLOSE_LOCATION = os.getenv("LSR_V1_REQUIRE_CLOSE_LOCATION", "false").lower() == "true"
LSR_V1_FVG_SNAP_ATR = float(os.getenv("LSR_V1_FVG_SNAP_ATR", "0.25"))
LSR_V1_USE_15M_EPHEMERAL_FVG = os.getenv("LSR_V1_USE_15M_EPHEMERAL_FVG", "true").lower() == "true"

# LLM delivery-order booster cap (ADR); does not alter event confidence
LLM_BOOST_CAP = float(os.getenv("LLM_BOOST_CAP", "0.10"))

# CA + venue-aggregate failover for 15m backbone (specs/ca-truth-venue-agg-failover.md)
MARKET_FAILOVER_ENABLED = os.getenv("MARKET_FAILOVER_ENABLED", "false").lower() == "true"
LEGACY_SCANNER_ENABLED = os.getenv("LEGACY_SCANNER_ENABLED", "false").lower() == "true"

# Emit classification (normative, see spec)
PRICE_STRUCTURE_STRATEGY_IDS = {
    "accumulation-base-v2", "rsi-reclaim-v1",
    "liquidity-sweep-reversal-v1", "bb-rsi-meanrev-v1", "failed-break-v3",
    "williams-fractal-scalp-v1", "ema9-continuation-stochrsi-v1",
}
MIXED_STRATEGY_IDS = {
    "impulse-ignition-v2", "continuation-breakout-v2",
}

FAILOVER_SOURCE_NAME = os.getenv("FAILOVER_SOURCE_NAME", "venue_agg_v1")
FAILOVER_CATCHUP_HOURS = int(os.getenv("FAILOVER_CATCHUP_HOURS", "2"))
FAILOVER_WATCHLIST_CAP = int(os.getenv("FAILOVER_WATCHLIST_CAP", "20"))
FAILOVER_MAX_REQUESTS_PER_CYCLE = int(os.getenv("FAILOVER_MAX_REQUESTS_PER_CYCLE", "80"))
FAILOVER_CIRCUIT_AGE_MIN = int(os.getenv("FAILOVER_CIRCUIT_AGE_MIN", "30"))
FAILOVER_CIRCUIT_CLEAR_AGE_MIN = int(os.getenv("FAILOVER_CIRCUIT_CLEAR_AGE_MIN", "20"))
FAILOVER_CIRCUIT_429_RATE = float(os.getenv("FAILOVER_CIRCUIT_429_RATE", "0.50"))
FAILOVER_CIRCUIT_CLEAR_429_RATE = float(os.getenv("FAILOVER_CIRCUIT_CLEAR_429_RATE", "0.25"))
FAILOVER_CIRCUIT_WINDOW_MIN = int(os.getenv("FAILOVER_CIRCUIT_WINDOW_MIN", "30"))

# CA shaping when limited (specs/ca-limited-takeover.md) — reduces non-critical load to aid recovery + takeover
CA_SHAPE_ON_CIRCUIT = os.getenv("CA_SHAPE_ON_CIRCUIT", "true").lower() == "true"
CA_SHAPE_SKIP_SECONDARY = os.getenv("CA_SHAPE_SKIP_SECONDARY", "true").lower() == "true"

# Failover data completeness (specs/ca-limited-takeover.md)
FAILOVER_FUNDING_PRIORITY = os.getenv("FAILOVER_FUNDING_PRIORITY", "true").lower() == "true"

# Rate limiting & budgeting (see specs/external-api-rate-limiting.md)
COINANALYZE_RPS = float(os.getenv("COINANALYZE_RPS", "0.08"))
COINANALYZE_MAX_CONCURRENT = int(os.getenv("COINANALYZE_MAX_CONCURRENT", "5"))
COINANALYZE_DEFAULT_RETRY_AFTER = int(os.getenv("COINANALYZE_DEFAULT_RETRY_AFTER", "5"))

LLM_RESEARCH_ENABLED = os.getenv("LLM_RESEARCH_ENABLED", "false").lower() == "true"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
# Base URL for OpenAI-compatible routers (e.g. local 9router). Empty -> api.openai.com.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "20"))
LLM_MAX_REPORTS_PER_CYCLE = int(os.getenv("LLM_MAX_REPORTS_PER_CYCLE", "2"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))
LLM_RETRY_BASE_SECONDS = int(os.getenv("LLM_RETRY_BASE_SECONDS", "60"))
LLM_MAX_INPUT_CHARS = int(os.getenv("LLM_MAX_INPUT_CHARS", "24000"))
LLM_MAX_OUTPUT_CHARS = int(os.getenv("LLM_MAX_OUTPUT_CHARS", "6000"))
LLM_MONTHLY_BUDGET_USD = float(os.getenv("LLM_MONTHLY_BUDGET_USD", "0"))
LLM_INCLUDE_IN_TELEGRAM = os.getenv("LLM_INCLUDE_IN_TELEGRAM", "false").lower() == "true"
LLM_INCLUDE_IN_DISCORD = os.getenv("LLM_INCLUDE_IN_DISCORD", os.getenv("LLM_INCLUDE_IN_TELEGRAM", "false")).lower() == "true"
LLM_PRICING_VERSION = os.getenv("LLM_PRICING_VERSION", "openai-chat-2026-08-v1")
LLM_INPUT_COST_PER_1K_USD = float(os.getenv("LLM_INPUT_COST_PER_1K_USD", "0"))
LLM_OUTPUT_COST_PER_1K_USD = float(os.getenv("LLM_OUTPUT_COST_PER_1K_USD", "0"))
# LLM_BOOST_CAP defined with strategy v2 knobs above (delivery priority only)

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
BYBIT_LINEAR_BASE_URL = os.getenv("BYBIT_LINEAR_BASE_URL", "https://api.bybit.com")

def get_db_connection(read_only: bool = False, db_path: str | Path | None = None):
    """
    Returns a SQLite connection configured for concurrent service processes.
    Writers should be minimized; prefer read_only=True for non-orchestrator code.
    """
    db_file = Path(db_path or MARKET_DB_PATH)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    if read_only:
        conn = sqlite3.connect(f"file:{db_file.resolve()}?mode=ro", uri=True, timeout=30.0)
    else:
        conn = sqlite3.connect(str(db_file), timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    if not read_only:
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_market_db(db_path: str | Path | None = None):
    """Orchestrator market schema (delegates to guarded init_db for compat)."""
    init_db(db_path or MARKET_DB_PATH, force_market=True)

def init_analyst_db(db_path: str | Path | None = None):
    target = db_path or ANALYST_DB_PATH
    init_db(target, force_alpha=True)
    conn = get_db_connection(db_path=target)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS cutoff_runs (
            cutoff_id VARCHAR PRIMARY KEY, cutoff_at TIMESTAMP WITH TIME ZONE NOT NULL,
            status VARCHAR NOT NULL, started_at TIMESTAMP WITH TIME ZONE NOT NULL,
            finalized_at TIMESTAMP WITH TIME ZONE, source_observation_ids VARCHAR, error VARCHAR)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS feature_snapshots (
            snapshot_id VARCHAR PRIMARY KEY, cutoff_id VARCHAR NOT NULL, asset VARCHAR NOT NULL,
            feature_set VARCHAR NOT NULL, version VARCHAR NOT NULL,
            computed_at TIMESTAMP WITH TIME ZONE NOT NULL, payload_json VARCHAR NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS structure_zones (
            zone_id VARCHAR PRIMARY KEY, cutoff_id VARCHAR NOT NULL, asset VARCHAR NOT NULL,
            kind VARCHAR NOT NULL, direction VARCHAR, strength DOUBLE, low DOUBLE, high DOUBLE,
            state VARCHAR, source_evidence_ids VARCHAR, confidence_status VARCHAR,
            created_at TIMESTAMP WITH TIME ZONE)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS raw_signals (
            raw_signal_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
            asset TEXT NOT NULL, direction TEXT NOT NULL, observed_at TEXT NOT NULL,
            valid_until TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS raw_signal_status_history (
            status_id TEXT PRIMARY KEY, raw_signal_id TEXT NOT NULL, hard_gate_status TEXT,
            score_status TEXT, clash_status TEXT, executor_intent_status TEXT, reason TEXT,
            recorded_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS discord_signal_batches (
            window_start TEXT PRIMARY KEY, window_end TEXT NOT NULL, status TEXT NOT NULL,
            candidate_count INTEGER NOT NULL, message_count INTEGER NOT NULL DEFAULT 0,
            claimed_at TEXT, sent_at TEXT, response_body TEXT, error_message TEXT,
            attempts INTEGER NOT NULL DEFAULT 0)""")
        conn.commit()
    finally:
        conn.close()


def init_alpha_db(db_path: str | Path | None = None):
    """Publisher alpha ledger schema (delegates to guarded init_db)."""
    init_db(db_path, force_alpha=True)


def init_db(db_path: str | Path | None = None, *, force_market: bool = False, force_alpha: bool = False):
    """Initializes the database schema if it doesn't exist.
    With no explicit target, initialize both service-owned databases. Explicit
    targets are kept for tests and migration tooling.
    """
    if db_path is None and not force_market and not force_alpha:
        init_db(MARKET_DB_PATH, force_market=True)
        init_db(ANALYST_DB_PATH, force_alpha=True)
        return
    target = str(db_path or MARKET_DB_PATH)
    alpha_target = str(ANALYST_DB_PATH)
    is_alpha = force_alpha or (
        not force_market
        and (target in {alpha_target, str(ANALYST_DB_PATH)} or Path(target).name in {"alpha_events.db", "analyst.db"})
    )
    conn = get_db_connection(read_only=False, db_path=db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR PRIMARY KEY,
                applied_at TIMESTAMP WITH TIME ZONE NOT NULL
            );
        """)
        # Phase 6: runtime active/inactive toggle for strategy plugins.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plugin_states (
                strategy_id VARCHAR PRIMARY KEY,
                state VARCHAR NOT NULL CHECK (state IN ('active', 'inactive', 'paused')),
                updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
                reason VARCHAR,
                updated_by VARCHAR
            );
        """)

        # Phase 7: LLM position-management sidecar (emit-only). `positions_feed` is
        # executor-owned (PM reads it); `pm_advice` is PM-owned (executor consumes).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions_feed (
                position_id VARCHAR PRIMARY KEY,
                symbol VARCHAR NOT NULL,
                asset VARCHAR NOT NULL,
                side VARCHAR NOT NULL,
                entry DOUBLE NOT NULL,
                size DOUBLE,
                opened_at TIMESTAMP WITH TIME ZONE NOT NULL,
                strategy_id VARCHAR NOT NULL,
                current_pnl DOUBLE,
                status VARCHAR NOT NULL DEFAULT 'open',
                updated_at TIMESTAMP WITH TIME ZONE NOT NULL
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pm_advice (
                advice_id VARCHAR PRIMARY KEY,
                position_id VARCHAR NOT NULL,
                strategy_id VARCHAR NOT NULL,
                asset VARCHAR NOT NULL,
                action VARCHAR NOT NULL CHECK (action IN ('hold', 'exit', 'reduce')),
                reason VARCHAR,
                htf_bias VARCHAR,
                rr DOUBLE,
                cutoff_at TIMESTAMP WITH TIME ZONE NOT NULL,
                observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pm_advice_pos ON pm_advice (position_id, cutoff_at);")
        if not is_alpha:
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
        existing_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(confluence_alerts)").fetchall()
        }
        for col in ["val DOUBLE", "vah DOUBLE", "hvns VARCHAR", "lvns VARCHAR"]:
            name = col.split()[0]
            if name not in existing_columns:
                conn.execute(f"ALTER TABLE confluence_alerts ADD COLUMN {col};")

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
                    price_change_1h DOUBLE,
                    is_accumulating BOOLEAN,
                PRIMARY KEY (timestamp, symbol)
            );
        """)

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
                feature_snapshot   VARCHAR,
                promoted_alpha_id  VARCHAR
            );
        """)
        # An emitted event is represented by its deterministic alpha_id. Rows
        # created before an event is emitted retain their own stable ID and link
        # to the promoted event without changing their identity.

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
        
        # Create indexes for fast analysis (market only; futures_data removed post-drop)
        if not is_alpha:
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

        # Binance OI tables moved to dedicated BINANCE_OI_DB_PATH (see init_binance_oi_db)
        # to reduce lock contention with main market data.

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
        
        
        if not is_alpha:
            # Data platform v2 tables (append-only source layer + cutoff + features)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS source_observations (
                    observation_id VARCHAR PRIMARY KEY,
                    source VARCHAR NOT NULL,
                    venue VARCHAR NOT NULL,
                    native_symbol VARCHAR NOT NULL,
                    asset VARCHAR NOT NULL,
                    market_kind VARCHAR NOT NULL,
                    interval VARCHAR NOT NULL,
                    source_start TIMESTAMP WITH TIME ZONE NOT NULL,
                    source_end TIMESTAMP WITH TIME ZONE NOT NULL,
                    retrieved_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    retrieval_kind VARCHAR,
                    payload_json VARCHAR NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_src_obs_range ON source_observations (asset, interval, source_end);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS cutoff_runs (
                    cutoff_id VARCHAR PRIMARY KEY,
                    cutoff_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    status VARCHAR NOT NULL CHECK (status IN ('running','finalized','failed')),
                    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    finalized_at TIMESTAMP WITH TIME ZONE,
                    source_observation_ids VARCHAR,
                    error VARCHAR
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cutoff_at ON cutoff_runs (cutoff_at);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS source_request_log (
                    request_id VARCHAR PRIMARY KEY,
                    cutoff_id VARCHAR,
                    source VARCHAR NOT NULL,
                    request_type VARCHAR NOT NULL,
                    weight INTEGER,
                    budget_remaining INTEGER,
                    selected_universe_json VARCHAR,
                    status VARCHAR NOT NULL,
                    requested_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    completed_at TIMESTAMP WITH TIME ZONE,
                    response_meta_json VARCHAR
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS feature_snapshots (
                    snapshot_id VARCHAR PRIMARY KEY,
                    cutoff_id VARCHAR NOT NULL,
                    asset VARCHAR NOT NULL,
                    feature_set VARCHAR NOT NULL,
                    version VARCHAR NOT NULL,
                    computed_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    payload_json VARCHAR NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_feat_cut ON feature_snapshots (cutoff_id, asset, feature_set);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS structure_zones (
                    zone_id VARCHAR PRIMARY KEY,
                    cutoff_id VARCHAR NOT NULL,
                    asset VARCHAR NOT NULL,
                    kind VARCHAR NOT NULL,
                    direction VARCHAR,
                    strength DOUBLE,
                    low DOUBLE,
                    high DOUBLE,
                    state VARCHAR,
                    source_evidence_ids VARCHAR,
                    confidence_status VARCHAR,
                    created_at TIMESTAMP WITH TIME ZONE
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_zones_cut ON structure_zones (cutoff_id, asset);")
        
        # Keep the two service stores physically independent even though the
        # schema declarations above share this compact initialization routine.
        owned = ANALYST_SCHEMA_TABLES if is_alpha else MARKET_SCHEMA_TABLES
        for table in (ANALYST_SCHEMA_TABLES | MARKET_SCHEMA_TABLES) - owned:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        
        conn.commit()
    finally:
        conn.close()

def import_legacy_futures_as_source_observations(db_path: str | Path | None = None) -> int:
    """One-shot migration: append legacy futures_data rows into source_observations (idempotent).
    Run once at cutover; subsequent inits are no-op. To be removed post futures_data drop.
    """
    conn = get_db_connection(read_only=False, db_path=db_path)
    try:
        # Only if we have legacy data and no legacy_import rows yet
        already = conn.execute(
            "SELECT 1 FROM source_observations WHERE retrieval_kind = 'legacy_import' LIMIT 1"
        ).fetchone()
        if already:
            return 0
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()]
        if "futures_data" not in tables:
            return 0
        rows = conn.execute("""
            SELECT timestamp, underlying, symbol, open, high, low, close, volume
            FROM futures_data
            ORDER BY timestamp
        """).fetchall()
        if not rows:
            return 0
        imported = 0
        for ts, underlying, symbol, o, h, l, c, v in rows:
            obs_id = f"legacy:{underlying}:{ts}"
            payload = {
                "open": o, "high": h, "low": l, "close": c, "volume": v,
                "open_interest": None, "funding_rate": None
            }
            try:
                conn.execute("""
                    INSERT INTO source_observations VALUES (?, 'coinalyze', 'aggregate_perp', ?, ?, 'perpetual', '15m', ?, ?, ?, 'legacy_import', ?)
                    ON CONFLICT (observation_id) DO NOTHING
                """, (obs_id, symbol, underlying or symbol.split("USDT")[0], ts, ts, ts, json.dumps(payload)))
                imported += 1
            except Exception:
                pass
        conn.commit()
        return imported
    finally:
        conn.close()


def drop_legacy_futures_data(db_path: str | Path | None = None) -> bool:
    """Post-cutover drop of legacy futures_data table.
    Verifies source_observations has data first. Returns True if dropped.
    """
    conn = get_db_connection(read_only=False, db_path=db_path)
    try:
        # Verify source has coverage
        has_source = conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] > 0
        if not has_source:
            print("Refusing drop: no data in source_observations yet")
            return False
        # Check if futures still exists
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()]
        if "futures_data" not in tables:
            return False
        conn.execute("DROP TABLE IF EXISTS futures_data")
        # Also drop index if any
        try:
            conn.execute("DROP INDEX IF EXISTS idx_futures_ts")
        except Exception:
            pass
        conn.commit()
        print("Dropped legacy futures_data table (source_observations is now sole market source)")
        return True
    finally:
        conn.close()


if __name__ == "__main__":
    print(f"Initializing market database at {MARKET_DB_PATH}...")
    init_db()
    print("Database initialized successfully.")
