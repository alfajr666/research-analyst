"""
regime_signal.py
================
Per-symbol daily signal generator combining:
  - TraderXO dual VWAP framework (weekly 7d + monthly 30d rolling)
  - HMM regime detection (GaussianHMM, k=3, trained on 300 daily bars)
  - EMA12/EMA25 confluence

Signal logic:
  1. Setup detection  : dual VWAP same side + 4-of-5 acceptance filter
  2. Conviction score : HMM regime + EMA alignment + acceptance strength + VWAP distance
  3. DB logging       : ALL signals (LOW / MODERATE / HIGH / no_signal)
  4. Telegram alert   : HIGH conviction only (+ close alert if prior was HIGH)

Run standalone:
  python regime_signal.py               # run today for full universe
  python regime_signal.py --backtest 30 # simulate last 30 daily bars (BTC + ETH)
  python regime_signal.py --symbol BTC  # single symbol
"""

from __future__ import annotations

import argparse
import pickle
import sys
import warnings
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import polars as pl
import numpy as np

warnings.filterwarnings("ignore", category=UserWarning)  # suppress hmmlearn convergence noise

import config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WEEKLY_WINDOW  = 7     # rolling days for "weekly" VWAP
MONTHLY_WINDOW = 30    # rolling days for "monthly" VWAP
HMM_STATES     = 3     # trending / ranging / high_vol
HMM_TRAIN_BARS = 300   # minimum bars needed for HMM training
HMM_ITER       = 200   # max EM iterations
ACCEPTANCE_WINDOW  = 5    # last N daily closes evaluated
ACCEPTANCE_MIN     = 4    # must have ≥ this many on correct side
REGIME_CONF_STRONG = 0.65  # threshold for +2 HMM score
REGIME_CONF_MILD   = 0.50  # threshold for +1 HMM score
VWAP_DIST_STRONG   = 0.005 # 0.5% beyond monthly VWAP → +1 score

# Conviction thresholds
HIGH_SCORE     = 4
MODERATE_SCORE = 2

# Feature column names fed into HMM
HMM_FEATURES = ["log_return", "realized_vol", "vwap_dev", "vol_zscore", "hl_range_norm"]


# ---------------------------------------------------------------------------
# Section 1 — Data Loader
# ---------------------------------------------------------------------------

def load_daily_bars(symbol: str) -> Optional[pl.DataFrame]:
    """
    Loads 1d futures OHLCV from the freqtrade feather file for a given symbol.
    Returns a Polars DataFrame sorted ascending by date, or None if not found / too short.
    """
    feather_path = Path(config.FREQTRADE_DATA_DIR) / f"{symbol}_USDT_USDT-1d-futures.feather"
    if not feather_path.exists():
        print(f"  [{symbol}] Feather file not found: {feather_path.name} — skipping.")
        return None

    try:
        import pandas as pd
        pdf = pd.read_feather(str(feather_path))
        df = pl.from_pandas(pdf)
    except Exception as e:
        print(f"  [{symbol}] Failed to read feather: {e} — skipping.")
        return None

    # Normalise column names (freqtrade uses 'date')
    if "date" in df.columns:
        df = df.rename({"date": "timestamp"})

    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if not required.issubset(set(df.columns)):
        print(f"  [{symbol}] Missing columns {required - set(df.columns)} — skipping.")
        return None

    df = df.sort("timestamp").select(["timestamp", "open", "high", "low", "close", "volume"])
    return df


# ---------------------------------------------------------------------------
# Section 2 — Feature Engineering
# ---------------------------------------------------------------------------

def _rolling_vwap(df: pl.DataFrame, window: int, col_name: str) -> pl.Series:
    """
    Computes a rolling VWAP over `window` bars using typical price.
    Typical price = (high + low + close) / 3
    VWAP = sum(tp * volume, window) / sum(volume, window)
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    tp_vol = tp * df["volume"]

    tp_vol_roll = tp_vol.rolling_sum(window_size=window, min_samples=window)
    vol_roll    = df["volume"].rolling_sum(window_size=window, min_samples=window)

    vwap = tp_vol_roll / vol_roll
    return vwap.alias(col_name)


def compute_features(df: pl.DataFrame) -> pl.DataFrame:
    """
    Adds all derived feature columns to the daily OHLCV DataFrame.
    Returns a new DataFrame with added columns.
    """
    # --- Dual rolling VWAP ---
    weekly_vwap  = _rolling_vwap(df, WEEKLY_WINDOW,  "weekly_vwap")
    monthly_vwap = _rolling_vwap(df, MONTHLY_WINDOW, "monthly_vwap")
    df = df.with_columns([weekly_vwap, monthly_vwap])

    # --- EMA 12 and EMA 25 ---
    df = df.with_columns([
        pl.col("close").ewm_mean(span=12, adjust=False).alias("ema12"),
        pl.col("close").ewm_mean(span=25, adjust=False).alias("ema25"),
    ])

    # --- HMM input features ---
    # log return
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1)).log().alias("log_return")
    )
    # realized vol: rolling 20-bar std of log returns
    df = df.with_columns(
        pl.col("log_return").rolling_std(window_size=20, min_samples=10).alias("realized_vol")
    )
    # VWAP deviation: % distance from weekly VWAP
    df = df.with_columns(
        ((pl.col("close") - pl.col("weekly_vwap")) / pl.col("weekly_vwap")).alias("vwap_dev")
    )
    # volume z-score: rolling 20-bar
    df = df.with_columns([
        pl.col("volume").rolling_mean(window_size=20, min_samples=10).alias("_vol_mean"),
        pl.col("volume").rolling_std(window_size=20, min_samples=10).alias("_vol_std"),
    ])
    df = df.with_columns(
        ((pl.col("volume") - pl.col("_vol_mean")) / (pl.col("_vol_std") + 1e-9)).alias("vol_zscore")
    ).drop(["_vol_mean", "_vol_std"])

    # high-low range normalised by close
    df = df.with_columns(
        ((pl.col("high") - pl.col("low")) / pl.col("close")).alias("hl_range_norm")
    )

    return df


# ---------------------------------------------------------------------------
# Section 3 — HMM Regime Model
# ---------------------------------------------------------------------------

def _label_states(model, n_states: int) -> dict[int, str]:
    """
    Maps HMM state indices to human-readable labels using emission means.
    Heuristic:
      - Highest mean log_return  → trending_up
      - Lowest mean log_return   → trending_down
      - Highest realized_vol (remainder) → high_vol   (only if n_states >= 4)
      - Remaining               → ranging
    """
    means = model.means_  # shape (n_states, n_features)
    # feature order: log_return, realized_vol, vwap_dev, vol_zscore, hl_range_norm
    lr_idx  = 0
    vol_idx = 1

    state_lr  = {i: means[i, lr_idx]  for i in range(n_states)}
    state_vol = {i: means[i, vol_idx] for i in range(n_states)}

    sorted_by_lr = sorted(state_lr, key=state_lr.get, reverse=True)

    labels: dict[int, str] = {}
    labels[sorted_by_lr[0]]  = "trending_up"
    labels[sorted_by_lr[-1]] = "trending_down"

    remaining = [s for s in range(n_states) if s not in labels]
    if len(remaining) == 1:
        # k=3: one remaining — call it ranging unless vol is very high
        r = remaining[0]
        labels[r] = "high_vol" if state_vol[r] > state_vol[sorted_by_lr[0]] * 1.5 else "ranging"
    elif len(remaining) >= 2:
        # k=4+: highest vol remaining → high_vol, rest → ranging
        rem_by_vol = sorted(remaining, key=lambda s: state_vol[s], reverse=True)
        labels[rem_by_vol[0]] = "high_vol"
        for s in rem_by_vol[1:]:
            labels[s] = "ranging"

    return labels


def fit_hmm(df: pl.DataFrame, symbol: str) -> tuple[str, float]:
    """
    Trains (or loads cached) a GaussianHMM on the last HMM_TRAIN_BARS rows.
    Returns (regime_label, confidence) for the most recent bar.
    """
    from hmmlearn.hmm import GaussianHMM

    # Drop rows with any NaN in HMM features
    feat_df = df.select(HMM_FEATURES).drop_nulls()

    if len(feat_df) < HMM_TRAIN_BARS:
        print(f"  [{symbol}] Only {len(feat_df)} clean bars — need {HMM_TRAIN_BARS}. Skipping HMM.")
        return "unknown", 0.0

    # Use last HMM_TRAIN_BARS rows
    train_arr = feat_df.tail(HMM_TRAIN_BARS).to_numpy().astype(float)

    model_path = Path(config.HMM_MODELS_DIR) / f"{symbol}_hmm.pkl"

    # Always retrain (daily cadence)
    model = GaussianHMM(
        n_components=HMM_STATES,
        covariance_type="diag",
        n_iter=HMM_ITER,
        random_state=42,
        tol=1e-4,
    )
    try:
        model.fit(train_arr)
    except Exception as e:
        print(f"  [{symbol}] HMM fit failed: {e}")
        return "unknown", 0.0

    # Persist model
    try:
        with open(model_path, "wb") as f:
            pickle.dump(model, f)
    except Exception as e:
        print(f"  [{symbol}] Could not save HMM model: {e}")

    # Infer on the last bar
    last_obs = feat_df.tail(1).to_numpy().astype(float)
    try:
        posteriors = model.predict_proba(train_arr)  # shape (T, n_states)
        last_posteriors = posteriors[-1]              # probabilities for last bar
    except Exception as e:
        print(f"  [{symbol}] HMM inference failed: {e}")
        return "unknown", 0.0

    dominant_idx  = int(np.argmax(last_posteriors))
    confidence    = float(last_posteriors[dominant_idx])
    state_labels  = _label_states(model, HMM_STATES)
    regime_label  = state_labels.get(dominant_idx, "unknown")

    return regime_label, confidence


# ---------------------------------------------------------------------------
# Section 4 — Signal Logic
# ---------------------------------------------------------------------------

def _acceptance_count(df: pl.DataFrame) -> tuple[int, str]:
    """
    Returns (count, side) where count = number of last ACCEPTANCE_WINDOW closes
    that are on the same side of weekly_vwap as the most recent close.
    Side is 'above' or 'below'.
    """
    tail = df.tail(ACCEPTANCE_WINDOW)
    if len(tail) < ACCEPTANCE_WINDOW:
        return 0, "unknown"

    closes = tail["close"].to_list()
    vwaps  = tail["weekly_vwap"].to_list()

    # Determine side from the most recent bar
    last_close = closes[-1]
    last_vwap  = vwaps[-1]
    if last_vwap is None:
        return 0, "unknown"

    side = "above" if last_close >= last_vwap else "below"

    count = sum(
        1 for c, v in zip(closes, vwaps)
        if v is not None and (
            (side == "above" and c >= v) or
            (side == "below" and c <  v)
        )
    )
    return count, side


def compute_signal(
    symbol: str,
    df: pl.DataFrame,
    regime_label: str,
    regime_conf: float,
) -> dict:
    """
    Full signal computation for the most recent bar in df.
    Returns a signal dict ready for DB insertion and Telegram formatting.
    """
    today = date.today()

    # Guard: need at least MONTHLY_WINDOW rows for VWAP to be meaningful
    if len(df) < MONTHLY_WINDOW:
        return _no_signal(symbol, today, "insufficient_data")

    latest = df.tail(1).to_dicts()[0]
    close        = latest.get("close")
    weekly_vwap  = latest.get("weekly_vwap")
    monthly_vwap = latest.get("monthly_vwap")
    ema12        = latest.get("ema12")
    ema25        = latest.get("ema25")

    if any(v is None for v in [close, weekly_vwap, monthly_vwap, ema12, ema25]):
        return _no_signal(symbol, today, "missing_indicator_data")

    # -----------------------------------------------------------------------
    # Step 1 — Dual VWAP bias (setup direction)
    # -----------------------------------------------------------------------
    above_weekly  = close > weekly_vwap
    above_monthly = close > monthly_vwap

    if above_weekly == above_monthly:
        bias = "long" if above_weekly else "short"
    else:
        return _no_signal(symbol, today, "vwap_split",
                          weekly_vwap=weekly_vwap, monthly_vwap=monthly_vwap,
                          ema12=ema12, ema25=ema25, close=close)

    # -----------------------------------------------------------------------
    # Step 2 — Acceptance filter (4-of-5 daily closes)
    # -----------------------------------------------------------------------
    accept_count, accept_side = _acceptance_count(df)
    expected_side = "above" if bias == "long" else "below"

    if accept_side != expected_side or accept_count < ACCEPTANCE_MIN:
        return _no_signal(symbol, today, "acceptance_not_met",
                          weekly_vwap=weekly_vwap, monthly_vwap=monthly_vwap,
                          ema12=ema12, ema25=ema25, close=close,
                          acceptance=accept_count)

    # -----------------------------------------------------------------------
    # Step 3 — Conviction scoring
    # -----------------------------------------------------------------------
    score = 0

    # HMM regime alignment
    trending_direction = "trending_up" if bias == "long" else "trending_down"
    if regime_label == trending_direction:
        if regime_conf >= REGIME_CONF_STRONG:
            score += 2
        elif regime_conf >= REGIME_CONF_MILD:
            score += 1
    elif regime_label in ("ranging", "high_vol"):
        score -= 1
    # else: opposite trending → no points (not a penalty since setup conditions are already met)

    # EMA alignment
    ema_aligned = (bias == "long" and ema12 > ema25) or (bias == "short" and ema12 < ema25)
    if ema_aligned:
        score += 1

    # Perfect acceptance (5/5)
    if accept_count == ACCEPTANCE_WINDOW:
        score += 1

    # Strong distance from monthly VWAP
    monthly_dist = abs(close - monthly_vwap) / monthly_vwap
    if monthly_dist >= VWAP_DIST_STRONG:
        score += 1

    # Conviction level
    if score >= HIGH_SCORE:
        conviction = "HIGH"
    elif score >= MODERATE_SCORE:
        conviction = "MODERATE"
    else:
        conviction = "LOW"

    return {
        "date":             today,
        "underlying":       symbol,
        "signal":           bias,          # "long" | "short"
        "no_signal_reason": None,
        "conviction":       conviction,
        "conviction_score": score,
        "regime":           regime_label,
        "regime_conf":      round(regime_conf, 4),
        "weekly_vwap":      round(weekly_vwap, 6),
        "monthly_vwap":     round(monthly_vwap, 6),
        "ema12":            round(ema12, 6),
        "ema25":            round(ema25, 6),
        "ema_aligned":      ema_aligned,
        "acceptance":       accept_count,
        "close_price":      round(close, 6),
    }


def _no_signal(symbol: str, day: date, reason: str, **extras) -> dict:
    base = {
        "date":             day,
        "underlying":       symbol,
        "signal":           "no_signal",
        "no_signal_reason": reason,
        "conviction":       None,
        "conviction_score": None,
        "regime":           extras.get("regime"),
        "regime_conf":      extras.get("regime_conf"),
        "weekly_vwap":      _round_safe(extras.get("weekly_vwap")),
        "monthly_vwap":     _round_safe(extras.get("monthly_vwap")),
        "ema12":            _round_safe(extras.get("ema12")),
        "ema25":            _round_safe(extras.get("ema25")),
        "ema_aligned":      None,
        "acceptance":       extras.get("acceptance"),
        "close_price":      _round_safe(extras.get("close")),
    }
    return base


def _round_safe(v, decimals=6):
    try:
        return round(float(v), decimals) if v is not None else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Section 5 — Orchestrator
# ---------------------------------------------------------------------------

def _get_universe() -> list[str]:
    """
    Returns all symbols with a matching 1d feather file in FREQTRADE_DATA_DIR.
    Intersection with the options-research-analyst active universe if DB is available.
    """
    data_dir = Path(config.FREQTRADE_DATA_DIR)
    feather_symbols = [
        p.name.replace("_USDT_USDT-1d-futures.feather", "")
        for p in data_dir.glob("*_USDT_USDT-1d-futures.feather")
    ]
    return sorted(feather_symbols)


def _send_telegram(msg: str):
    token   = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        print("  Telegram not configured — skipping alert.")
        return
    try:
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = httpx.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=10)
        if resp.status_code != 200:
            print(f"  Telegram send failed: {resp.text}")
    except Exception as e:
        print(f"  Telegram error: {e}")


def _fmt_price(v) -> str:
    if v is None:
        return "N/A"
    v = float(v)
    if v < 1.0:
        return f"${v:.6f}"
    if v < 10000.0:
        return f"${v:,.2f}"
    return f"${round(v):,.0f}"


def _build_alert_new(sig: dict) -> str:
    direction = "LONG 🟢" if sig["signal"] == "long" else "SHORT 🔴"
    icon = {"HIGH": "🔥", "MODERATE": "✅", "LOW": "⚠️"}.get(sig["conviction"], "")
    regime_str = f"{sig['regime']} ({sig['regime_conf']*100:.0f}% confidence)" if sig["regime"] else "unknown"
    ema_str = "✅" if sig["ema_aligned"] else "❌"
    accept_str = f"{sig['acceptance']}/{ACCEPTANCE_WINDOW} closes on correct side ✅"
    monthly_side = "above" if sig["signal"] == "long" else "below"
    weekly_side  = monthly_side

    return (
        f"📡 *REGIME SIGNAL — {direction}*\n\n"
        f"• *Asset:* #{sig['underlying']}  |  *Conviction:* {icon} {sig['conviction']} (score: {sig['conviction_score']}/6)\n"
        f"• *Price:* {_fmt_price(sig['close_price'])}\n\n"
        f"*Setup:*\n"
        f"  ▫️ Weekly VWAP:  {_fmt_price(sig['weekly_vwap'])}  — price {weekly_side} ✅\n"
        f"  ▫️ Monthly VWAP: {_fmt_price(sig['monthly_vwap'])}  — price {monthly_side} ✅\n"
        f"  ▫️ Acceptance:   {accept_str}\n\n"
        f"*Confluences:*\n"
        f"  ▫️ HMM Regime:   {regime_str}\n"
        f"  ▫️ EMA12/EMA25:  {_fmt_price(sig['ema12'])} / {_fmt_price(sig['ema25'])} {ema_str}\n\n"
        f"_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC_"
    )


def _build_alert_closed(symbol: str, reason: str) -> str:
    return (
        f"🚫 *REGIME SIGNAL CLOSED — #{symbol}*\n\n"
        f"Previously HIGH conviction setup has been invalidated.\n"
        f"Reason: `{reason}`\n\n"
        f"_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC_"
    )


def _upsert_signal(conn, sig: dict):
    conn.execute("""
        INSERT OR REPLACE INTO regime_signals
            (date, underlying, signal, no_signal_reason, conviction, conviction_score,
             regime, regime_conf, weekly_vwap, monthly_vwap, ema12, ema25,
             ema_aligned, acceptance, close_price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        sig["date"], sig["underlying"], sig["signal"], sig["no_signal_reason"],
        sig["conviction"], sig["conviction_score"],
        sig["regime"], sig["regime_conf"],
        sig["weekly_vwap"], sig["monthly_vwap"],
        sig["ema12"], sig["ema25"],
        sig["ema_aligned"], sig["acceptance"], sig["close_price"],
    ))


def _get_previous_signal(conn, symbol: str) -> Optional[dict]:
    """Returns the most recent regime_signals row for the symbol (before today)."""
    today = date.today()
    row = conn.execute("""
        SELECT signal, conviction, no_signal_reason
        FROM regime_signals
        WHERE underlying = ? AND date < ?
        ORDER BY date DESC LIMIT 1
    """, (symbol, today)).fetchone()
    if row:
        return {"signal": row[0], "conviction": row[1], "no_signal_reason": row[2]}
    return None


def run_regime_signals(conn, symbols: Optional[list[str]] = None, dry_run: bool = False):
    """
    Main entry point: runs the full pipeline for all (or specified) symbols.
    Writes results to DB and sends Telegram alerts for HIGH conviction setups.

    Args:
        conn     : DuckDB connection (writable)
        symbols  : list of symbols to process; if None, uses full universe
        dry_run  : if True, prints signal output but does not write DB or send Telegram
    """
    config.init_db()

    universe = symbols or _get_universe()
    print(f"Running regime signals for {len(universe)} symbols...")

    results = []
    for symbol in universe:
        print(f"  Processing {symbol}...")

        # Load data
        df = load_daily_bars(symbol)
        if df is None:
            continue

        # Feature engineering
        df = compute_features(df)

        # HMM regime
        regime_label, regime_conf = fit_hmm(df, symbol)

        # Signal logic
        sig = compute_signal(symbol, df, regime_label, regime_conf)
        results.append(sig)

        if dry_run:
            _print_signal(sig)
            continue

        # Get previous signal for transition detection
        prev = _get_previous_signal(conn, symbol)

        # Write to DB
        _upsert_signal(conn, sig)
        conn.commit()

        # Telegram alert logic
        _handle_telegram_alert(sig, prev)

    return results


def _handle_telegram_alert(sig: dict, prev: Optional[dict]):
    """Send Telegram only on HIGH conviction transitions or HIGH setup closure."""
    symbol = sig["underlying"]
    is_high = sig["conviction"] == "HIGH" and sig["signal"] != "no_signal"
    was_high = prev and prev["conviction"] == "HIGH" and prev["signal"] != "no_signal"

    if is_high and not was_high:
        # New HIGH conviction setup
        print(f"  [{symbol}] 🔔 NEW HIGH conviction {sig['signal'].upper()} — sending alert.")
        _send_telegram(_build_alert_new(sig))

    elif is_high and was_high and prev["signal"] != sig["signal"]:
        # Direction flip at HIGH conviction
        print(f"  [{symbol}] 🔔 HIGH conviction direction flip — sending alert.")
        _send_telegram(_build_alert_new(sig))

    elif was_high and sig["signal"] == "no_signal":
        # HIGH setup just closed
        print(f"  [{symbol}] 🚫 HIGH setup closed — sending invalidation alert.")
        _send_telegram(_build_alert_closed(symbol, sig["no_signal_reason"] or "setup_closed"))

    else:
        level = sig["conviction"] or "no_signal"
        print(f"  [{symbol}] {sig['signal']} ({level}) — logged only.")


def _print_signal(sig: dict):
    """Pretty-print a signal dict (used in dry-run / backtest mode)."""
    s = sig["signal"]
    c = sig.get("conviction") or "-"
    score = sig.get("conviction_score")
    score_str = f" score={score}" if score is not None else ""
    reason = f" [{sig['no_signal_reason']}]" if sig.get("no_signal_reason") else ""
    print(
        f"    {sig['date']}  {sig['underlying']:<12}  {s:<10}  {c:<10}{score_str}"
        f"  regime={sig.get('regime','?')} ({(sig.get('regime_conf') or 0)*100:.0f}%)"
        f"  accept={sig.get('acceptance','?')}/{ACCEPTANCE_WINDOW}"
        f"  ema_ok={sig.get('ema_aligned','?')}{reason}"
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Regime Signal Generator")
    parser.add_argument("--symbol",   type=str, help="Single symbol to process (e.g. BTC)")
    parser.add_argument("--backtest", type=int, metavar="N",
                        help="Dry-run: show signals for last N daily bars for BTC+ETH (no DB write)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Process but do not write DB or send Telegram")
    args = parser.parse_args()

    if args.backtest:
        _run_backtest(args.backtest, symbol=args.symbol)
        return

    symbols = [args.symbol] if args.symbol else None
    conn = config.get_db_connection(read_only=False)
    try:
        run_regime_signals(conn, symbols=symbols, dry_run=args.dry_run)
    finally:
        conn.close()


def _run_backtest(n_days: int, symbol: Optional[str] = None):
    """
    Simulates the last n_days of daily signals without touching the DB.
    Useful for validating VWAP values, acceptance counts, and HMM labels.
    """
    pilot = [symbol] if symbol else ["BTC", "ETH"]
    print(f"\n{'='*70}")
    print(f"BACKTEST MODE — last {n_days} bars — symbols: {pilot}")
    print(f"{'='*70}")

    for sym in pilot:
        df = load_daily_bars(sym)
        if df is None:
            continue
        df = compute_features(df)

        print(f"\n--- {sym} ---")
        print(f"{'Date':<12} {'Signal':<10} {'Conv':<10} {'Score':>6}  {'Regime':<14} {'RegConf':>8}  {'Accept':>7}  {'EMA_ok':>7}  Reason")
        print("-" * 110)

        # For each of the last n_days, simulate signal as of that day
        total_rows = len(df)
        start_idx  = max(MONTHLY_WINDOW + HMM_TRAIN_BARS, total_rows - n_days)

        for i in range(start_idx, total_rows):
            slice_df = df[:i + 1]
            try:
                regime_label, regime_conf = fit_hmm(slice_df, sym)
                sig = compute_signal(sym, slice_df, regime_label, regime_conf)
                sig["date"] = slice_df["timestamp"].tail(1)[0].date()
                _print_signal(sig)
            except Exception as e:
                print(f"    Error at row {i}: {e}")


if __name__ == "__main__":
    main()
