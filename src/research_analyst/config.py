import json
import math
import os
import stat
import warnings
from datetime import date, datetime
from pathlib import Path
from typing import List
import sqlite3
from dotenv import load_dotenv


# Python 3.12 deprecated sqlite3's implicit date/datetime adapters. Store
# timezone-aware values as explicit ISO-8601 text at every connection boundary.
sqlite3.register_adapter(date, lambda value: value.isoformat())
sqlite3.register_adapter(datetime, lambda value: value.isoformat())


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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Discord incoming webhooks (optional). Empty URL disables that stream.
DISCORD_ALPHA_WEBHOOK_URL = os.getenv("DISCORD_ALPHA_WEBHOOK_URL", "")
RAW_SIGNAL_DISCORD_BATCH_ENABLED = os.getenv("RAW_SIGNAL_DISCORD_BATCH_ENABLED", "false").lower() == "true"
RAW_SIGNAL_DISCORD_BATCH_MINUTES = int(os.getenv("RAW_SIGNAL_DISCORD_BATCH_MINUTES", "30"))
RAW_SIGNAL_DISCORD_WEBHOOK_URL = os.getenv("RAW_SIGNAL_DISCORD_WEBHOOK_URL", DISCORD_ALPHA_WEBHOOK_URL)
RAW_BATCH_CLAIM_LEASE_SECONDS = int(os.getenv("RAW_BATCH_CLAIM_LEASE_SECONDS", "120"))
RAW_BATCH_MAX_ATTEMPTS = int(os.getenv("RAW_BATCH_MAX_ATTEMPTS", "5"))
RAW_BATCH_RETRY_BACKOFF_SECONDS = int(os.getenv("RAW_BATCH_RETRY_BACKOFF_SECONDS", "30"))
if RAW_SIGNAL_DISCORD_BATCH_MINUTES <= 0 or 60 % RAW_SIGNAL_DISCORD_BATCH_MINUTES:
    raise ValueError("RAW_SIGNAL_DISCORD_BATCH_MINUTES must be a positive divisor of 60")
# Config Settings
MARKET_DB_PATH = os.getenv("MARKET_DB_PATH", str(DEFAULT_DB_DIR / "market.sqlite3"))
ANALYST_DB_PATH = os.getenv("ANALYST_DB_PATH", str(DEFAULT_DB_DIR / "analyst.sqlite3"))
ENTRY_POLICY_MODE = os.getenv("ENTRY_POLICY_MODE", "shadow").strip().lower()
if ENTRY_POLICY_MODE not in {"off", "shadow", "enforce"}:
    raise ValueError("ENTRY_POLICY_MODE must be off, shadow, or enforce")
REGIME_SESSION_MODE = os.getenv("REGIME_SESSION_MODE", "shadow").strip().lower()
REGIME_DB_PATH = os.getenv("REGIME_DB_PATH", str(DEFAULT_DB_DIR / "regime.sqlite3"))
REGIME_SESSION_GRACE_SECONDS = int(os.getenv("REGIME_SESSION_GRACE_SECONDS", "30"))
REGIME_SESSION_COOLDOWN_MINUTES = int(os.getenv("REGIME_SESSION_COOLDOWN_MINUTES", "30"))
REGIME_SESSION_FAMILY_ON_THRESHOLD = float(os.getenv("REGIME_SESSION_FAMILY_ON_THRESHOLD", "0.35"))
REGIME_SESSION_FAMILY_OFF_THRESHOLD = float(os.getenv("REGIME_SESSION_FAMILY_OFF_THRESHOLD", "0.25"))
if REGIME_SESSION_MODE not in {"off", "shadow", "enforce"}:
    raise ValueError("REGIME_SESSION_MODE must be off, shadow, or enforce")
if REGIME_SESSION_GRACE_SECONDS < 0:
    raise ValueError("REGIME_SESSION_GRACE_SECONDS must not be negative")
if REGIME_SESSION_COOLDOWN_MINUTES < 0:
    raise ValueError("REGIME_SESSION_COOLDOWN_MINUTES must not be negative")
if not 0 <= REGIME_SESSION_FAMILY_OFF_THRESHOLD <= REGIME_SESSION_FAMILY_ON_THRESHOLD <= 1:
    raise ValueError("REGIME_SESSION_FAMILY_OFF_THRESHOLD and ON_THRESHOLD must be ordered in [0, 1]")
REGIME_SCORE_ADX_NORMALIZATION = float(os.getenv("REGIME_SCORE_ADX_NORMALIZATION", "50"))
REGIME_SCORE_ADX_LENGTH = int(os.getenv("REGIME_SCORE_ADX_LENGTH", "14"))
REGIME_SCORE_ADX_SMOOTHING = int(os.getenv("REGIME_SCORE_ADX_SMOOTHING", "14"))
REGIME_SCORE_VOL_WINDOW_BARS = int(os.getenv("REGIME_SCORE_VOL_WINDOW_BARS", "12"))
REGIME_SCORE_TRANSITION_CENTER_UTC_MINUTE = int(os.getenv("REGIME_SCORE_TRANSITION_CENTER_UTC_MINUTE", "780"))
REGIME_SCORE_TRANSITION_WIDTH_MINUTES = int(os.getenv("REGIME_SCORE_TRANSITION_WIDTH_MINUTES", "60"))
REGIME_SCORE_TRANSITION_MIN_DISCOUNT = float(os.getenv("REGIME_SCORE_TRANSITION_MIN_DISCOUNT", "0.5"))
REGIME_SCORE_REVERSAL_MIN_PRIOR_TREND = float(os.getenv("REGIME_SCORE_REVERSAL_MIN_PRIOR_TREND", "0.55"))
REGIME_SCORE_REVERSAL_DECAY_MIN = float(os.getenv("REGIME_SCORE_REVERSAL_DECAY_MIN", "0.15"))
REGIME_SCORE_RETENTION_DAYS = int(os.getenv("REGIME_SCORE_RETENTION_DAYS", "30"))
REGIME_GATE_RETENTION_DAYS = int(os.getenv("REGIME_GATE_RETENTION_DAYS", "90"))
REGIME_PROVENANCE_MAX_IDS = max(32, int(os.getenv("REGIME_PROVENANCE_MAX_IDS", "128")))
REGIME_1H_FETCH_DAYS = int(os.getenv("REGIME_1H_FETCH_DAYS", "4"))
REGIME_1H_RETAIN_DAYS = int(os.getenv("REGIME_1H_RETAIN_DAYS", "3"))
REGIME_1H_READINESS_BARS = int(os.getenv("REGIME_1H_READINESS_BARS", "57"))
REGIME_1H_REQUEST_TIMEOUT_SECONDS = int(os.getenv("REGIME_1H_REQUEST_TIMEOUT_SECONDS", "20"))
if REGIME_1H_FETCH_DAYS < 4 or REGIME_1H_RETAIN_DAYS < 3 or REGIME_1H_READINESS_BARS < 57:
    raise ValueError("direct regime 1h history settings cannot lower the v2 readiness contract")
if REGIME_1H_FETCH_DAYS < REGIME_1H_RETAIN_DAYS:
    raise ValueError("REGIME_1H_FETCH_DAYS must cover REGIME_1H_RETAIN_DAYS")
REGIME_4H_FETCH_DAYS = int(os.getenv("REGIME_4H_FETCH_DAYS", "15"))
REGIME_4H_RETAIN_DAYS = int(os.getenv("REGIME_4H_RETAIN_DAYS", "14"))
REGIME_4H_READINESS_BARS = int(os.getenv("REGIME_4H_READINESS_BARS", "57"))
REGIME_4H_REQUEST_TIMEOUT_SECONDS = int(os.getenv("REGIME_4H_REQUEST_TIMEOUT_SECONDS", "20"))
if REGIME_4H_FETCH_DAYS < 15 or REGIME_4H_RETAIN_DAYS < 14 or REGIME_4H_READINESS_BARS < 57:
    raise ValueError("direct regime 4h history settings cannot lower the v1 readiness contract")
if REGIME_4H_FETCH_DAYS < REGIME_4H_RETAIN_DAYS:
    raise ValueError("REGIME_4H_FETCH_DAYS must cover REGIME_4H_RETAIN_DAYS")
DIRECT_HTF_1H_SEED_BARS = int(os.getenv("DIRECT_HTF_1H_SEED_BARS", "240"))
DIRECT_HTF_4H_SEED_BARS = int(os.getenv("DIRECT_HTF_4H_SEED_BARS", "240"))
if DIRECT_HTF_1H_SEED_BARS < 57 or DIRECT_HTF_4H_SEED_BARS < 57:
    raise ValueError("direct HTF seed bars cannot lower the regime readiness contract")
DIRECT_HTF_1H_RETAIN_DAYS = max(
    REGIME_1H_RETAIN_DAYS, 14, (DIRECT_HTF_1H_SEED_BARS + 23) // 24 + 4
)
DIRECT_HTF_4H_RETAIN_DAYS = max(
    REGIME_4H_RETAIN_DAYS, 45, (DIRECT_HTF_4H_SEED_BARS + 5) // 6 + 5
)
DIRECT_HTF_1H_FETCH_DAYS = max(REGIME_1H_FETCH_DAYS, DIRECT_HTF_1H_RETAIN_DAYS + 1)
DIRECT_HTF_4H_FETCH_DAYS = max(REGIME_4H_FETCH_DAYS, DIRECT_HTF_4H_RETAIN_DAYS + 1)
EVALUATION_TRIGGER_DIR = Path(os.getenv("EVALUATION_TRIGGER_DIR", str(DEFAULT_DB_DIR / "evaluation_triggers")))
EVALUATION_RECOVERY_SCAN_SECONDS = int(os.getenv("EVALUATION_RECOVERY_SCAN_SECONDS", "5"))
EVALUATION_LEASE_SECONDS = int(os.getenv("EVALUATION_LEASE_SECONDS", "600"))
EVALUATION_MAX_RETRIES = int(os.getenv("EVALUATION_MAX_RETRIES", "5"))
EXECUTION_BACKFILL_HOURS = int(os.getenv("EXECUTION_BACKFILL_HOURS", "24"))

# Tables are deliberately classified here, at the schema boundary.  Startup
# must never repair a database by creating tables owned by the other service.
MARKET_SCHEMA_TABLES = frozenset({
    "option_chains", "daily_options_summary", "brain_outputs", "confluence_alerts",
    "scanner_history", "universe_snapshots", "broad_discovery_snapshots",
    "discovery_watchlist_history", "regime_signals",
    "source_observations", "source_request_log",
})
ANALYST_SCHEMA_TABLES = frozenset({
    "plugin_states", "alpha_candidates",
    "alpha_events", "signal_deliveries", "alpha_event_status_history",
    "alpha_confidence_observations", "research_requests", "research_reports",
    "research_run_metrics", "research_artifacts", "research_evidence", "pipeline_runs",
    "execution_deliveries", "cutoff_runs", "feature_snapshots", "structure_zones",
    "entry_policy_observations", "regime_scores", "regime_gate_decisions",
})


# Static agreed symbol universe from the approved tradeable-assets snapshot.
# Persisted in the repo at symbols/static_universe.json so it is version-controlled and
# survives restarts/prunes. Canonical bases (e.g. BTC); expand to XUSDT perps at load time.
STATIC_SYMBOLS_PATH = os.getenv("STATIC_SYMBOLS_PATH", str(BASE_DIR / "symbols" / "static_universe.json"))
STATIC_SYMBOLS_OVERRIDE = os.getenv("STATIC_SYMBOLS", "").strip()
# Upstream performance rotation.
SYMBOL_ROTATION_ENABLED = os.getenv("SYMBOL_ROTATION_ENABLED", "true").lower() in ("1", "true", "yes", "on")
SYMBOL_ROTATION_REFRESH_HOURS = int(os.getenv("SYMBOL_ROTATION_REFRESH_HOURS", os.getenv("SYMBOL_ROTATION_CADENCE_HOURS", "4")))
SYMBOL_ROTATION_CADENCE_HOURS = SYMBOL_ROTATION_REFRESH_HOURS  # compatibility alias
SYMBOL_ROTATION_LOOKBACK_HOURS = int(os.getenv("SYMBOL_ROTATION_LOOKBACK_HOURS", "24"))
SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT = int(os.getenv("SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT", "30"))
SYMBOL_ROTATION_BAR_INTERVAL = os.getenv("SYMBOL_ROTATION_BAR_INTERVAL", "5m").strip()
SYMBOL_ROTATION_FEED_PATH = Path(os.getenv("SYMBOL_ROTATION_FEED_PATH", str(DEFAULT_DB_DIR / "symbol_rotation_feed.json")))
SYMBOL_ROTATION_SOURCE_MAX_AGE_HOURS = float(os.getenv("SYMBOL_ROTATION_SOURCE_MAX_AGE_HOURS", "6"))
SYMBOL_ROTATION_WATCHLIST_TTL_HOURS = float(os.getenv("SYMBOL_ROTATION_WATCHLIST_TTL_HOURS", "72"))
SYMBOL_ROTATION_WATCHLIST_MAX_SYMBOLS = int(os.getenv("SYMBOL_ROTATION_WATCHLIST_MAX_SYMBOLS", "80"))
if SYMBOL_ROTATION_REFRESH_HOURS <= 0:
    raise ValueError("SYMBOL_ROTATION_REFRESH_HOURS must be positive")
if SYMBOL_ROTATION_LOOKBACK_HOURS <= 0:
    raise ValueError("SYMBOL_ROTATION_LOOKBACK_HOURS must be positive")
if SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT <= 0 or SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT % 2:
    raise ValueError("SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT must be a positive even number")
if not SYMBOL_ROTATION_BAR_INTERVAL:
    raise ValueError("SYMBOL_ROTATION_BAR_INTERVAL must not be empty")
if not math.isfinite(SYMBOL_ROTATION_SOURCE_MAX_AGE_HOURS) or SYMBOL_ROTATION_SOURCE_MAX_AGE_HOURS <= 0:
    raise ValueError("SYMBOL_ROTATION_SOURCE_MAX_AGE_HOURS must be finite and positive")
if not math.isfinite(SYMBOL_ROTATION_WATCHLIST_TTL_HOURS) or SYMBOL_ROTATION_WATCHLIST_TTL_HOURS <= 0:
    raise ValueError("SYMBOL_ROTATION_WATCHLIST_TTL_HOURS must be positive")
if SYMBOL_ROTATION_WATCHLIST_MAX_SYMBOLS < 4:
    raise ValueError("SYMBOL_ROTATION_WATCHLIST_MAX_SYMBOLS must include four permanent symbols")
if SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT > SYMBOL_ROTATION_WATCHLIST_MAX_SYMBOLS - 4:
    warnings.warn(
        "SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT exceeds available sticky watchlist slots; "
        "new selections will be deterministically truncated",
        RuntimeWarning,
        stacklevel=2,
    )
# WS provider toggles. Bybit is the default public source; Binance is opt-in/off.
WS_BYBIT_ENABLED = os.getenv("WS_BYBIT_ENABLED", "true").lower() == "true"
WS_BINANCE_ENABLED = os.getenv("WS_BINANCE_ENABLED", "false").lower() == "true"
COMPACT_STRATEGY_ASSETS = frozenset(("BTC", "ETH", "PAXG", "QQQ"))
COMPACT_STRATEGY_IDS = frozenset((
    "failed-break-v3", "bb-rsi-meanrev-v1",
    "williams-fractal-scalp-v1", "ema9-continuation-stochrsi-v1",
    "ema9-adx-stochrsi-state-v1",
))
FUNDAMO_STRATEGY_IDS = frozenset((
    "ema20-pullback-h4-trend-v1", "ema-stack-15m-adx-stochrsi-5m-v1",
    "gold-trend-ema-bb-stoch-v1", "mtf-exhaustion-reversal-v1", "trend-wall-v1",
    "ema99-double-touch-stochrsi-state-v1", "ema7-26-cross-hammer-shooting-star-1h-adx-v1",
))
EMA99_RETEST_STRATEGY_ID = "ema99-retest-adx-v1"
EMA99_RETEST_FAST_EMA_LENGTH = int(os.getenv("EMA99_RETEST_FAST_EMA_LENGTH", "26"))
EMA99_RETEST_SLOW_EMA_LENGTH = int(os.getenv("EMA99_RETEST_SLOW_EMA_LENGTH", "99"))
EMA99_RETEST_RSI_LENGTH = int(os.getenv("EMA99_RETEST_RSI_LENGTH", "14"))
EMA99_RETEST_ATR_LENGTH = int(os.getenv("EMA99_RETEST_ATR_LENGTH", "14"))
EMA99_RETEST_ATR_STOP_MULTIPLIER = float(os.getenv("EMA99_RETEST_ATR_STOP_MULTIPLIER", "2.0"))
EMA99_RETEST_ADX_TIMEFRAME = os.getenv("EMA99_RETEST_ADX_TIMEFRAME", "1h")
EMA99_RETEST_ADX_LENGTH = int(os.getenv("EMA99_RETEST_ADX_LENGTH", "14"))
EMA99_RETEST_ADX_SMOOTHING = int(os.getenv("EMA99_RETEST_ADX_SMOOTHING", "14"))
EMA99_RETEST_MIN_ADX = float(os.getenv("EMA99_RETEST_MIN_ADX", "25.0"))
EMA99_RETEST_MAX_RETEST_DISTANCE_PCT = float(os.getenv("EMA99_RETEST_MAX_RETEST_DISTANCE_PCT", "0.1"))
EMA99_RETEST_LONG_EXIT_RSI = float(os.getenv("EMA99_RETEST_LONG_EXIT_RSI", "72.0"))
EMA99_RETEST_SHORT_EXIT_RSI = float(os.getenv("EMA99_RETEST_SHORT_EXIT_RSI", "28.0"))
EMA99_RETEST_EXIT_SPREAD_PCT = float(os.getenv("EMA99_RETEST_EXIT_SPREAD_PCT", "0.5"))
EMA20_USE_SESSION_FILTER = os.getenv("EMA20_USE_SESSION_FILTER", "true").lower() == "true"
EMA20_EXCHANGE_TIMEZONE = os.getenv("EMA20_EXCHANGE_TIMEZONE", "UTC")
EMA9_TRIGGER_MEMORY_BARS = int(os.getenv("EMA9_TRIGGER_MEMORY_BARS", "30"))
EMA_STACK_USE_ADX = os.getenv("EMA_STACK_USE_ADX", "true").lower() == "true"
EMA_STACK_MIN_ADX = float(os.getenv("EMA_STACK_MIN_ADX", "20.0"))
EMA9_ADX_STRATEGY_ID = "ema9-adx-stochrsi-state-v1"
EMA9_ADX_ADX_LENGTH = int(os.getenv("EMA9_ADX_ADX_LENGTH", "14"))
EMA9_ADX_ADX_MIN = float(os.getenv("EMA9_ADX_ADX_MIN", "20.0"))
EMA9_ADX_RSI_LENGTH = int(os.getenv("EMA9_ADX_RSI_LENGTH", "14"))
EMA9_ADX_STOCH_LENGTH = int(os.getenv("EMA9_ADX_STOCH_LENGTH", "14"))
EMA9_ADX_K_LENGTH = int(os.getenv("EMA9_ADX_K_LENGTH", "3"))
EMA9_ADX_D_LENGTH = int(os.getenv("EMA9_ADX_D_LENGTH", "3"))
EMA9_ADX_EMA_LENGTH = int(os.getenv("EMA9_ADX_EMA_LENGTH", "9"))
EMA9_ADX_ATR_LENGTH = int(os.getenv("EMA9_ADX_ATR_LENGTH", "14"))
EMA9_ADX_ATR_MULTIPLIER = float(os.getenv("EMA9_ADX_ATR_MULTIPLIER", "2.0"))
EMA9_ADX_STRUCTURE_BARS = int(os.getenv("EMA9_ADX_STRUCTURE_BARS", "15"))
EMA9_ADX_EXTENSION_OFFSET_PCT = float(os.getenv("EMA9_ADX_EXTENSION_OFFSET_PCT", "5.0"))
EMA9_ADX_EXTENSION_LONG_K = float(os.getenv("EMA9_ADX_EXTENSION_LONG_K", "80.0"))
EMA9_ADX_EXTENSION_SHORT_K = float(os.getenv("EMA9_ADX_EXTENSION_SHORT_K", "20.0"))
EMA9_ADX_MOMENTUM_LONG_K = float(os.getenv("EMA9_ADX_MOMENTUM_LONG_K", "80.0"))
EMA9_ADX_MOMENTUM_SHORT_K = float(os.getenv("EMA9_ADX_MOMENTUM_SHORT_K", "20.0"))
EMA9_ADX_MOMENTUM_LONG_RSI = float(os.getenv("EMA9_ADX_MOMENTUM_LONG_RSI", "75.0"))
EMA9_ADX_MOMENTUM_SHORT_RSI = float(os.getenv("EMA9_ADX_MOMENTUM_SHORT_RSI", "25.0"))
EMA9_ADX_ENTRY_VALIDITY_MINUTES = int(os.getenv("EMA9_ADX_ENTRY_VALIDITY_MINUTES", "5"))
EMA99_DOUBLE_TOUCH_STRATEGY_ID = "ema99-double-touch-stochrsi-state-v1"
EMA99_DOUBLE_TOUCH_EMA_LENGTH = int(os.getenv("EMA99_DOUBLE_TOUCH_EMA_LENGTH", "99"))
EMA99_DOUBLE_TOUCH_PROXIMITY_PCT = float(os.getenv("EMA99_DOUBLE_TOUCH_PROXIMITY_PCT", "0.5"))
EMA99_DOUBLE_TOUCH_RSI1_LENGTH = int(os.getenv("EMA99_DOUBLE_TOUCH_RSI1_LENGTH", "14"))
EMA99_DOUBLE_TOUCH_RSI1_MIN = float(os.getenv("EMA99_DOUBLE_TOUCH_RSI1_MIN", "40.0"))
EMA99_DOUBLE_TOUCH_RSI1_MAX = float(os.getenv("EMA99_DOUBLE_TOUCH_RSI1_MAX", "60.0"))
EMA99_DOUBLE_TOUCH_STOCH_RSI_LENGTH = int(os.getenv("EMA99_DOUBLE_TOUCH_STOCH_RSI_LENGTH", "14"))
EMA99_DOUBLE_TOUCH_STOCH_LENGTH = int(os.getenv("EMA99_DOUBLE_TOUCH_STOCH_LENGTH", "14"))
EMA99_DOUBLE_TOUCH_K_LENGTH = int(os.getenv("EMA99_DOUBLE_TOUCH_K_LENGTH", "3"))
EMA99_DOUBLE_TOUCH_D_LENGTH = int(os.getenv("EMA99_DOUBLE_TOUCH_D_LENGTH", "3"))
EMA99_DOUBLE_TOUCH_OVERBOUGHT = float(os.getenv("EMA99_DOUBLE_TOUCH_OVERBOUGHT", "80.0"))
EMA99_DOUBLE_TOUCH_OVERSOLD = float(os.getenv("EMA99_DOUBLE_TOUCH_OVERSOLD", "20.0"))
EMA99_DOUBLE_TOUCH_FAST_EMA = int(os.getenv("EMA99_DOUBLE_TOUCH_FAST_EMA", "7"))
EMA99_DOUBLE_TOUCH_SLOW_EMA = int(os.getenv("EMA99_DOUBLE_TOUCH_SLOW_EMA", "26"))
EMA99_DOUBLE_TOUCH_CROSS_LOOKBACK = int(os.getenv("EMA99_DOUBLE_TOUCH_CROSS_LOOKBACK", "10"))
EMA99_DOUBLE_TOUCH_RSI5_LENGTH = int(os.getenv("EMA99_DOUBLE_TOUCH_RSI5_LENGTH", "14"))
EMA99_DOUBLE_TOUCH_ATR_LENGTH = int(os.getenv("EMA99_DOUBLE_TOUCH_ATR_LENGTH", "14"))
EMA99_DOUBLE_TOUCH_ATR_MULTIPLIER = float(os.getenv("EMA99_DOUBLE_TOUCH_ATR_MULTIPLIER", "1.0"))
EMA99_DOUBLE_TOUCH_ADX_LENGTH = int(os.getenv("EMA99_DOUBLE_TOUCH_ADX_LENGTH", "14"))
EMA99_DOUBLE_TOUCH_ADX_MIN = float(os.getenv("EMA99_DOUBLE_TOUCH_ADX_MIN", "20.0"))
EMA99_DOUBLE_TOUCH_LONG_TP_RSI = float(os.getenv("EMA99_DOUBLE_TOUCH_LONG_TP_RSI", "70.0"))
EMA99_DOUBLE_TOUCH_SHORT_TP_RSI = float(os.getenv("EMA99_DOUBLE_TOUCH_SHORT_TP_RSI", "30.0"))
EMA99_DOUBLE_TOUCH_TP_EMA_PCT = float(os.getenv("EMA99_DOUBLE_TOUCH_TP_EMA_PCT", "3.0"))
EMA99_DOUBLE_TOUCH_ENTRY_VALIDITY_MINUTES = int(os.getenv("EMA99_DOUBLE_TOUCH_ENTRY_VALIDITY_MINUTES", "5"))
EMA7_26_CROSS_HAMMER_STRATEGY_ID = "ema7-26-cross-hammer-shooting-star-1h-adx-v1"
EMA7_26_CROSS_FAST_EMA = int(os.getenv("EMA7_26_CROSS_FAST_EMA", "7"))
EMA7_26_CROSS_SLOW_EMA = int(os.getenv("EMA7_26_CROSS_SLOW_EMA", "26"))
EMA7_26_CROSS_SETUP_LOOKBACK = int(os.getenv("EMA7_26_CROSS_SETUP_LOOKBACK", "10"))
EMA7_26_CROSS_EMA_PROXIMITY_PCT = float(os.getenv("EMA7_26_CROSS_EMA_PROXIMITY_PCT", "0.25"))
EMA7_26_CROSS_MIN_BODY = float(os.getenv("EMA7_26_CROSS_MIN_BODY", "0.0"))
EMA7_26_CROSS_HAMMER_LOWER_WICK_RATIO = float(os.getenv("EMA7_26_CROSS_HAMMER_LOWER_WICK_RATIO", "2.0"))
EMA7_26_CROSS_HAMMER_UPPER_WICK_RATIO = float(os.getenv("EMA7_26_CROSS_HAMMER_UPPER_WICK_RATIO", "0.5"))
EMA7_26_CROSS_STAR_UPPER_WICK_RATIO = float(os.getenv("EMA7_26_CROSS_STAR_UPPER_WICK_RATIO", "2.0"))
EMA7_26_CROSS_STAR_LOWER_WICK_RATIO = float(os.getenv("EMA7_26_CROSS_STAR_LOWER_WICK_RATIO", "0.5"))
EMA7_26_CROSS_RSI_LENGTH = int(os.getenv("EMA7_26_CROSS_RSI_LENGTH", "14"))
EMA7_26_CROSS_ENTRY_RSI_MIN = float(os.getenv("EMA7_26_CROSS_ENTRY_RSI_MIN", "40.0"))
EMA7_26_CROSS_ENTRY_RSI_MAX = float(os.getenv("EMA7_26_CROSS_ENTRY_RSI_MAX", "60.0"))
EMA7_26_CROSS_ATR_LENGTH = int(os.getenv("EMA7_26_CROSS_ATR_LENGTH", "14"))
EMA7_26_CROSS_ATR_STOP_MULTIPLIER = float(os.getenv("EMA7_26_CROSS_ATR_STOP_MULTIPLIER", "1.0"))
EMA7_26_CROSS_ADX_LENGTH = int(os.getenv("EMA7_26_CROSS_ADX_LENGTH", "14"))
EMA7_26_CROSS_ADX_SMOOTHING = int(os.getenv("EMA7_26_CROSS_ADX_SMOOTHING", "14"))
EMA7_26_CROSS_ADX_MIN = float(os.getenv("EMA7_26_CROSS_ADX_MIN", "20.0"))
EMA7_26_CROSS_LONG_EXIT_RSI = float(os.getenv("EMA7_26_CROSS_LONG_EXIT_RSI", "28.0"))
EMA7_26_CROSS_SHORT_EXIT_RSI = float(os.getenv("EMA7_26_CROSS_SHORT_EXIT_RSI", "72.0"))
EMA7_26_CROSS_EXIT_SPREAD_BPS = float(os.getenv("EMA7_26_CROSS_EXIT_SPREAD_BPS", "50.0"))
EMA7_26_CROSS_ENTRY_VALIDITY_MINUTES = int(os.getenv("EMA7_26_CROSS_ENTRY_VALIDITY_MINUTES", "5"))
GOLD_FAST_EMA = int(os.getenv("GOLD_FAST_EMA", "50"))
GOLD_SLOW_EMA = int(os.getenv("GOLD_SLOW_EMA", "200"))
GOLD_BB_LENGTH = int(os.getenv("GOLD_BB_LENGTH", "20"))
GOLD_BB_STD = float(os.getenv("GOLD_BB_STD", "2.0"))
GOLD_RSI_LENGTH = int(os.getenv("GOLD_RSI_LENGTH", "14"))
GOLD_STOCH_LENGTH = int(os.getenv("GOLD_STOCH_LENGTH", "14"))
GOLD_K_SMOOTHING = int(os.getenv("GOLD_K_SMOOTHING", "3"))
GOLD_D_SMOOTHING = int(os.getenv("GOLD_D_SMOOTHING", "3"))
GOLD_ATR_LENGTH = int(os.getenv("GOLD_ATR_LENGTH", "14"))
GOLD_ATR_STOP_MULTIPLIER = float(os.getenv("GOLD_ATR_STOP_MULTIPLIER", "3.5"))
GOLD_TOUCH_TOLERANCE = float(os.getenv("GOLD_TOUCH_TOLERANCE", "0.0005"))
MTF_EXHAUSTION_RSI_LENGTH = int(os.getenv("MTF_EXHAUSTION_RSI_LENGTH", "14"))
MTF_EXHAUSTION_DIVERGENCE_LOOKBACK = int(os.getenv("MTF_EXHAUSTION_DIVERGENCE_LOOKBACK", "24"))
MTF_EXHAUSTION_ATR_LENGTH = int(os.getenv("MTF_EXHAUSTION_ATR_LENGTH", "16"))
MTF_EXHAUSTION_ATR_STOP_MULTIPLIER = float(os.getenv("MTF_EXHAUSTION_ATR_STOP_MULTIPLIER", "2.0"))
MTF_EXHAUSTION_MAX_ADX = float(os.getenv("MTF_EXHAUSTION_MAX_ADX", "25.0"))
TREND_WALL_EMA_LENGTH = int(os.getenv("TREND_WALL_EMA_LENGTH", "99"))
TREND_WALL_ADX_MIN = float(os.getenv("TREND_WALL_ADX_MIN", "20.0"))
TREND_WALL_WALL_PROXIMITY = float(os.getenv("TREND_WALL_WALL_PROXIMITY", "0.01"))
TREND_WALL_ATR_LENGTH = int(os.getenv("TREND_WALL_ATR_LENGTH", "16"))
TREND_WALL_ATR_STOP_MULTIPLIER = float(os.getenv("TREND_WALL_ATR_STOP_MULTIPLIER", "2.0"))
WS_STREAM_TIMEFRAMES = [
    value.strip().lower()
    for value in os.getenv("WS_STREAM_TIMEFRAMES", "5m").split(",")
    if value.strip()
]
WS_STREAM_TIMEFRAMES = [value for value in WS_STREAM_TIMEFRAMES if value != "1m"]
if not WS_STREAM_TIMEFRAMES or any(value != "5m" for value in WS_STREAM_TIMEFRAMES):
    raise ValueError("WS_STREAM_TIMEFRAMES must contain only 5m")
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
    return [b if str(b).upper().endswith("USDT") else f"{b}USDT" for b in bases]


STRATEGY_ENABLED_IDS = tuple(
    s.strip() for s in os.getenv(
        "STRATEGY_ENABLED_IDS",
        "failed-break-v3,bb-rsi-meanrev-v1,williams-fractal-scalp-v1,"
        "ema9-adx-stochrsi-state-v1,"
        "ema20-pullback-h4-trend-v1,gold-trend-ema-bb-stoch-v1,"
        "mtf-exhaustion-reversal-v1,ema99-double-touch-stochrsi-state-v1,"
        "ema7-26-cross-hammer-shooting-star-1h-adx-v1"
    ).split(",") if s.strip()
)

# Evaluation intervals the active strategy plugins run on. 5m is streamed
# directly by ws_gateway; 15m is a derived extension point. HTF (1h/4h) is
# loaded from the regime-owned direct history during each evaluation.
EVAL_INTERVALS = [
    s.strip() for s in os.getenv("EVAL_INTERVALS", "5m").split(",")
    if s.strip() and s.strip() != "1m"
]

# Runtime active/inactive toggle (phase 6). Empty => all enabled strategies are
# active. Set to an explicit allowlist to override (e.g. "accumulation-base-v2,
# impulse-ignition-v2"). The `plugin_states` table can also override per-strategy
# at runtime without a restart.
STRATEGY_ACTIVE_IDS = tuple(
    s.strip() for s in os.getenv("STRATEGY_ACTIVE_IDS", "").split(",") if s.strip()
)

# Tiered prune retention, days per interval. <=0 disables that tier.
PRUNE_5M_DAYS = int(os.getenv("PRUNE_5M_DAYS", "30"))
PRUNE_15M_DAYS = int(os.getenv("PRUNE_15M_DAYS", "90"))
PRUNE_1H_DAYS = int(os.getenv("PRUNE_1H_DAYS", "365"))
PRUNE_4H_DAYS = int(os.getenv("PRUNE_4H_DAYS", "365"))
PRUNE_INTERVAL_DAYS = {
    "5m": PRUNE_5M_DAYS, "15m": PRUNE_15M_DAYS,
    "1h": PRUNE_1H_DAYS, "4h": PRUNE_4H_DAYS,
}

# Database maintenance is run by the owner of each SQLite file.  Zone and
# feature snapshots are recomputable caches; durable candidate/event ledgers
# receive longer retention below.
DB_MAINTENANCE_ENABLED = os.getenv("DB_MAINTENANCE_ENABLED", "true").lower() in (
    "1", "true", "yes", "on"
)
DB_MAINTENANCE_INTERVAL_SECONDS = int(os.getenv("DB_MAINTENANCE_INTERVAL_SECONDS", "21600"))
DB_MAINTENANCE_BATCH_SIZE = min(5000, max(100, int(os.getenv("DB_MAINTENANCE_BATCH_SIZE", "5000"))))
DB_MAINTENANCE_YIELD_SECONDS = float(os.getenv("DB_MAINTENANCE_YIELD_SECONDS", "0.01"))
MARKET_OPTION_RETENTION_DAYS = int(os.getenv("MARKET_OPTION_RETENTION_DAYS", "3"))
MARKET_AUXILIARY_RETENTION_DAYS = int(os.getenv("MARKET_AUXILIARY_RETENTION_DAYS", "30"))
MARKET_DAILY_SUMMARY_RETENTION_DAYS = int(os.getenv("MARKET_DAILY_SUMMARY_RETENTION_DAYS", "365"))
MARKET_DISCOVERY_RETENTION_DAYS = int(os.getenv("MARKET_DISCOVERY_RETENTION_DAYS", "90"))
MARKET_WATCHLIST_RETENTION_DAYS = int(os.getenv("MARKET_WATCHLIST_RETENTION_DAYS", "365"))
MARKET_REGIME_RETENTION_DAYS = int(os.getenv("MARKET_REGIME_RETENTION_DAYS", "365"))
ANALYST_SNAPSHOT_RETENTION_DAYS = int(os.getenv("ANALYST_SNAPSHOT_RETENTION_DAYS", "2"))
ANALYST_CUTOFF_RETENTION_DAYS = int(os.getenv("ANALYST_CUTOFF_RETENTION_DAYS", "30"))
ANALYST_PIPELINE_RETENTION_DAYS = int(os.getenv("ANALYST_PIPELINE_RETENTION_DAYS", "30"))
ANALYST_RAW_SIGNAL_RETENTION_DAYS = int(os.getenv("ANALYST_RAW_SIGNAL_RETENTION_DAYS", "90"))
ANALYST_CANDIDATE_RETENTION_DAYS = int(os.getenv("ANALYST_CANDIDATE_RETENTION_DAYS", "90"))
ANALYST_EVENT_RETENTION_DAYS = int(os.getenv("ANALYST_EVENT_RETENTION_DAYS", "365"))
ANALYST_DELIVERY_RETENTION_DAYS = int(os.getenv("ANALYST_DELIVERY_RETENTION_DAYS", "365"))
ANALYST_METRICS_RETENTION_DAYS = int(os.getenv("ANALYST_METRICS_RETENTION_DAYS", "30"))
ANALYST_RESEARCH_RETENTION_DAYS = int(os.getenv("ANALYST_RESEARCH_RETENTION_DAYS", "30"))

# Trade-intent delivery (see bybit-executor/AGENTS.md "Trade Intent Contract",
# schema_version 1). The internal alpha event is the advisory record (Discord);
# this envelope is published to the shared SQLite bus for executor handoff.
# Deliberately OFF by default (INTENT_DELIVERY_ENABLED).
BYBIT_EXECUTOR_DIR = os.getenv("BYBIT_EXECUTOR_DIR", "")
INTENT_DELIVERY_ENABLED = os.getenv("INTENT_DELIVERY_ENABLED", "false").lower() in ("1", "true", "yes", "on")
INTENT_SOURCE = os.getenv("INTENT_SOURCE", "research-analyst")
INTENT_EXCHANGE_ID = os.getenv("INTENT_EXCHANGE_ID", "bybit")
INTENT_ACCOUNT_ID = os.getenv("INTENT_ACCOUNT_ID", "hyro")
INTENT_TAKE_PROFIT_MODE = os.getenv("INTENT_TAKE_PROFIT_MODE", "fixed_full_close")
INTENT_VALIDITY_MINUTES = int(os.getenv("INTENT_VALIDITY_MINUTES", "5"))
INTENT_MIN_RR = float(os.getenv("INTENT_MIN_RR", "2.0"))
INTENT_MIN_STOP_DISTANCE_PCT = float(os.getenv("INTENT_MIN_STOP_DISTANCE_PCT", "0.001"))
INTENT_MIN_STOP_ATR_MULTIPLIER = float(os.getenv("INTENT_MIN_STOP_ATR_MULTIPLIER", "0.25"))
DATA_FRESHNESS_MAX_SECONDS = float(os.getenv("DATA_FRESHNESS_MAX_SECONDS", "600"))
CLASH_MIN_SCORE_MARGIN = float(os.getenv("CLASH_MIN_SCORE_MARGIN", "2.0"))
STRATEGY_PRIORITY = {}
# Structural admission is mandatory for every candidate; it has no runtime bypass.
STRUCTURAL_STOP_ADMISSION_ENABLED = True
STRUCTURAL_STOP_MIN_ATR_MULTIPLE = float(os.getenv("STRUCTURAL_STOP_MIN_ATR_MULTIPLE", "0.5"))
STRUCTURAL_STOP_MAX_ATR_MULTIPLE = float(os.getenv("STRUCTURAL_STOP_MAX_ATR_MULTIPLE", "3.0"))
if STRUCTURAL_STOP_MIN_ATR_MULTIPLE < 0 or STRUCTURAL_STOP_MAX_ATR_MULTIPLE < STRUCTURAL_STOP_MIN_ATR_MULTIPLE:
    raise ValueError("STRUCTURAL_STOP ATR multiples must be non-negative and ordered")
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
for _fundamo_strategy in ("ema99-retest-adx-v1",
                           "ema20-pullback-h4-trend-v1", "ema-stack-15m-adx-stochrsi-5m-v1",
                           "gold-trend-ema-bb-stoch-v1", "mtf-exhaustion-reversal-v1",
                           "trend-wall-v1", "ema99-double-touch-stochrsi-state-v1",
                           "ema7-26-cross-hammer-shooting-star-1h-adx-v1"):
    INTENT_ROUTING.setdefault(_fundamo_strategy, {"exchange_id": "bybit", "account_id": "fundamo"})

# --- Shared SQLite Intent Bus (spec SHARED_SQLITE_INTENT_BUS_SPEC.md §14) ---
# Per spec the research-analyst publisher is gated by INTENT_BUS_DB (path) and
# INTENT_BUS_BYBIT_ENABLED. INTENT_DELIVERY_ENABLED (elsewhere) is the overall
# delivery gate. All default OFF; no implicit path.
INTENT_BUS_BYBIT_ENABLED = os.getenv("INTENT_BUS_BYBIT_ENABLED", "false").lower() in ("1", "true", "yes", "on")
INTENT_BUS_PROPR_ENABLED = os.getenv("INTENT_BUS_PROPR_ENABLED", "false").lower() in ("1", "true", "yes", "on")
_INTENT_BUS_DB_RAW = os.getenv("INTENT_BUS_DB") or ""
INTENT_BUS_DB = str(Path(_INTENT_BUS_DB_RAW).expanduser()) if _INTENT_BUS_DB_RAW and Path(_INTENT_BUS_DB_RAW).expanduser().is_absolute() else None
# Executor snapshot handoff used by ws_gateway to retain open-position symbols
# during universe rotation. Position management is owned by standalone-llm-pm.
if BYBIT_EXECUTOR_DIR:
    _default_exec_snapshots = Path(BYBIT_EXECUTOR_DIR) / "data" / "position-snapshots"
else:
    _default_exec_snapshots = ""
EXECUTOR_SNAPSHOT_DIR = os.getenv("EXECUTOR_SNAPSHOT_DIR", str(_default_exec_snapshots)) if _default_exec_snapshots else os.getenv("EXECUTOR_SNAPSHOT_DIR", "")

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

# Emit classification (normative, see spec)
PRICE_STRUCTURE_STRATEGY_IDS = {
    "accumulation-base-v2", "rsi-reclaim-v1",
    "liquidity-sweep-reversal-v1", "bb-rsi-meanrev-v1", "failed-break-v3",
    "williams-fractal-scalp-v1", "ema9-continuation-stochrsi-v1",
    "ema20-pullback-h4-trend-v1", "ema-stack-15m-adx-stochrsi-5m-v1",
    "gold-trend-ema-bb-stoch-v1", "mtf-exhaustion-reversal-v1", "trend-wall-v1",
    "ema9-adx-stochrsi-state-v1",
    "ema99-double-touch-stochrsi-state-v1",
    "ema7-26-cross-hammer-shooting-star-1h-adx-v1",
}
MIXED_STRATEGY_IDS = {
    "impulse-ignition-v2", "continuation-breakout-v2",
}

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


# API Base URLs
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
        conn.execute("""CREATE TABLE IF NOT EXISTS raw_signal_evaluation_coverage (
            strategy_id TEXT NOT NULL, asset TEXT NOT NULL, evaluated_at TEXT NOT NULL,
            emitted_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (strategy_id, asset, evaluated_at))""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_raw_signal_coverage_evaluated_at
            ON raw_signal_evaluation_coverage (evaluated_at, asset)""")
        policy_migration = "2026-09-04-entry-policy-observations"
        if conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (policy_migration,)).fetchone() is None:
            conn.execute("""CREATE TABLE IF NOT EXISTS entry_policy_observations (
                candidate_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL,
                policy_version TEXT NOT NULL, mode TEXT NOT NULL, decision TEXT NOT NULL,
                session_name TEXT NOT NULL, session_phase TEXT NOT NULL,
                session_elapsed_minutes INTEGER, market_family TEXT NOT NULL,
                environment_state TEXT NOT NULL, reasons_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entry_policy_observed_at ON entry_policy_observations (observed_at)")
            conn.execute("INSERT INTO schema_migrations VALUES (?, CURRENT_TIMESTAMP)", (policy_migration,))
        conn.execute("""CREATE TABLE IF NOT EXISTS discord_signal_batches (
            window_start TEXT PRIMARY KEY, window_end TEXT NOT NULL, status TEXT NOT NULL,
            candidate_count INTEGER NOT NULL, message_count INTEGER NOT NULL DEFAULT 0,
            claimed_at TEXT, claimed_by TEXT, message_text TEXT, message_hash TEXT,
            sent_at TEXT, response_body TEXT, error_message TEXT,
            attempts INTEGER NOT NULL DEFAULT 0)""")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(discord_signal_batches)").fetchall()}
        if "claimed_by" not in columns:
            conn.execute("ALTER TABLE discord_signal_batches ADD COLUMN claimed_by TEXT")
        if "message_text" not in columns:
            conn.execute("ALTER TABLE discord_signal_batches ADD COLUMN message_text TEXT")
        if "message_hash" not in columns:
            conn.execute("ALTER TABLE discord_signal_batches ADD COLUMN message_hash TEXT")
        conn.execute("""CREATE TABLE IF NOT EXISTS discord_signal_batch_members (
            window_start TEXT NOT NULL, raw_signal_id TEXT NOT NULL,
            PRIMARY KEY (window_start, raw_signal_id))""")
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

        # Create indexes for fast analysis.
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_source_observations_retention ON source_observations (interval, source_end);")

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
        # Keep the two service stores physically independent even though the
        # schema declarations above share this compact initialization routine.
        owned = ANALYST_SCHEMA_TABLES if is_alpha else MARKET_SCHEMA_TABLES
        for table in (ANALYST_SCHEMA_TABLES | MARKET_SCHEMA_TABLES) - owned:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        
        conn.commit()
    finally:
        conn.close()

if __name__ == "__main__":
    print(f"Initializing market database at {MARKET_DB_PATH}...")
    init_db()
    print("Database initialized successfully.")
