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
   4. Transition logging: HIGH conviction transitions are recorded locally

Run standalone:
  python regime_signal.py               # run today for full universe
  python regime_signal.py --backtest 30 # simulate last 30 daily bars (BTC + ETH)
  python regime_signal.py --symbol BTC  # single symbol
"""

from __future__ import annotations

import argparse
import pickle
import sys
import os
import contextlib
import warnings
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import polars as pl
import numpy as np

warnings.filterwarnings("ignore", category=UserWarning)  # suppress hmmlearn convergence noise

import config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WEEKLY_WINDOW = 7      # rolling days for "weekly" VWAP
VWAP_4H_WINDOW = 18    # rolling 4-hour bars (18 × 4h = 72h = 3 days) for intraday VWAP
HMM_STATES     = 3     # trending / ranging / high_vol
HMM_TRAIN_BARS = 300   # minimum bars needed for HMM training
HMM_ITER       = 500   # max EM iterations (increased to reduce non-convergence noise)
HMM_TOL        = 1e-2  # looser tolerance — financial data rarely achieves tight convergence
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

def load_daily_bars(conn, symbol: str) -> Optional[pl.DataFrame]:
    """
    Loads 1d OHLCV from source_observations (post futures_data drop), aggregated from 15m candles.
    Returns a Polars DataFrame sorted ascending by date, or None if no data.
    """
    query = """
        SELECT
            DATE(source_end)                                        AS timestamp,
            FIRST(json_extract(payload_json, '$.open')::DOUBLE)     AS open,
            MAX(json_extract(payload_json, '$.high')::DOUBLE)       AS high,
            MIN(json_extract(payload_json, '$.low')::DOUBLE)        AS low,
            LAST(json_extract(payload_json, '$.close')::DOUBLE)     AS close,
            SUM(json_extract(payload_json, '$.volume')::DOUBLE)     AS volume
        FROM source_observations
        WHERE asset = ?
        GROUP BY DATE(source_end)
        ORDER BY timestamp
    """
    try:
        arrow = conn.execute(query, (symbol,)).fetch_arrow_table()
    except Exception as e:
        print(f"  [{symbol}] DB query failed: {e} — skipping.")
        return None

    if arrow.num_rows == 0:
        print(f"  [{symbol}] No daily data in DB — skipping.")
        return None

    df = pl.from_arrow(arrow)
    # ensure proper UTC timestamp type
    df = df.with_columns(pl.col("timestamp").cast(pl.Datetime("us")))
    return df


def load_xh_bars(conn, symbol: str, bar_hours: int, min_bars: int = 1) -> Optional[pl.DataFrame]:
    """
    Generic multi-hour OHLCV loader: aggregates 15m candles into N-hour bars.
    Returns a Polars DataFrame sorted ascending, or None if fewer than min_bars rows.

    Args:
        conn      : DuckDB connection
        symbol    : underlying symbol
        bar_hours : number of hours per bar (1, 4, etc.)
        min_bars  : minimum acceptable row count (default 1)
    """
    interval_secs = bar_hours * 3600
    query = f"""
        SELECT
            TO_TIMESTAMP(
                FLOOR(EPOCH(source_end) / {interval_secs}) * {interval_secs}
            )                                                       AS timestamp,
            FIRST(json_extract(payload_json, '$.open')::DOUBLE)     AS open,
            MAX(json_extract(payload_json, '$.high')::DOUBLE)       AS high,
            MIN(json_extract(payload_json, '$.low')::DOUBLE)        AS low,
            LAST(json_extract(payload_json, '$.close')::DOUBLE)     AS close,
            SUM(json_extract(payload_json, '$.volume')::DOUBLE)     AS volume
        FROM source_observations
        WHERE asset = ?
        GROUP BY FLOOR(EPOCH(source_end) / {interval_secs})
        ORDER BY timestamp
    """
    label = f"{bar_hours}h"
    try:
        arrow = conn.execute(query, (symbol,)).fetch_arrow_table()
    except Exception as e:
        print(f"  [{symbol}] {label} DB query failed: {e} — skipping.")
        return None

    if arrow.num_rows < min_bars:
        print(f"  [{symbol}] Only {arrow.num_rows} {label} bars — need {min_bars}. Skipping {label} load.")
        return None

    df = pl.from_arrow(arrow)
    df = df.with_columns(pl.col("timestamp").cast(pl.Datetime("us")))
    return df


def load_4h_bars(conn, symbol: str) -> Optional[pl.DataFrame]:
    """
    Loads 4-hour OHLCV with rolling 18-bar VWAP for dual-VWAP signal.
    """
    df = load_xh_bars(conn, symbol, bar_hours=4, min_bars=VWAP_4H_WINDOW)
    if df is None:
        return None
    vwap_col = _rolling_vwap(df, VWAP_4H_WINDOW, "vwap_4h_3d")
    df = df.with_columns(vwap_col)
    return df


def compute_vwap_4h(conn, symbol: str) -> Optional[float]:
    """Returns the latest 4h 3-day rolling VWAP value for a symbol."""
    df = load_4h_bars(conn, symbol)
    if df is None:
        return None
    return df.tail(1).to_dicts()[0].get("vwap_4h_3d")


def prepare_hmm_data(conn, symbol: str) -> Optional[pl.DataFrame]:
    """
    Loads 1-hour OHLCV bars and computes the 5 HMM feature columns.
    Returns a DataFrame with only HMM_FEATURES columns (nulls dropped),
    or None if insufficient data.
    """
    df = load_xh_bars(conn, symbol, bar_hours=1, min_bars=HMM_TRAIN_BARS)
    if df is None:
        return None

    # 1. Log return
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1)).log().alias("log_return")
    )

    # 2. Realized vol: 24-bar rolling std (≈ 1 day on 1h data)
    df = df.with_columns(
        pl.col("log_return").rolling_std(window_size=24, min_samples=12).alias("realized_vol")
    )

    # 3. VWAP deviation: % distance from a 24-bar rolling VWAP
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    tp_vol = tp * df["volume"]
    tp_vol_roll = tp_vol.rolling_sum(window_size=24, min_samples=12)
    vol_roll = df["volume"].rolling_sum(window_size=24, min_samples=12)
    vwap_24h = tp_vol_roll / vol_roll
    df = df.with_columns(
        ((pl.col("close") - vwap_24h) / vwap_24h).alias("vwap_dev")
    )

    # 4. Volume z-score: 24-bar rolling
    df = df.with_columns([
        pl.col("volume").rolling_mean(window_size=24, min_samples=12).alias("_vol_mean"),
        pl.col("volume").rolling_std(window_size=24, min_samples=12).alias("_vol_std"),
    ])
    df = df.with_columns(
        ((pl.col("volume") - pl.col("_vol_mean")) / (pl.col("_vol_std") + 1e-9)).alias("vol_zscore")
    ).drop(["_vol_mean", "_vol_std"])

    # 5. High-low range normalised by close
    df = df.with_columns(
        ((pl.col("high") - pl.col("low")) / pl.col("close")).alias("hl_range_norm")
    )

    hmm_df = df.select(HMM_FEATURES).drop_nulls()
    if len(hmm_df) < HMM_TRAIN_BARS:
        print(f"  [{symbol}] Only {len(hmm_df)} clean 1h feature rows — need {HMM_TRAIN_BARS}. Skipping HMM.")
        return None

    print(f"  [{symbol}] HMM data: {len(hmm_df)} clean 1h feature rows.")
    return hmm_df


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
    # --- Weekly rolling VWAP (7-day) ---
    df = df.with_columns(_rolling_vwap(df, WEEKLY_WINDOW, "weekly_vwap"))

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

    # 14-day Average True Range (ATR) for stop-loss and targets calculation
    prev_close = pl.col("close").shift(1)
    tr = pl.max_horizontal([
        pl.col("high") - pl.col("low"),
        (pl.col("high") - prev_close).abs(),
        (pl.col("low") - prev_close).abs()
    ])
    df = df.with_columns(
        tr.rolling_mean(window_size=14, min_samples=10).alias("atr14")
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
        tol=HMM_TOL,
        random_state=42,
    )
    try:
        with open(os.devnull, "w") as f, contextlib.redirect_stderr(f), contextlib.redirect_stdout(f):
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
    vwap_4h: Optional[float] = None,
) -> dict:
    """
    Full signal computation for the most recent bar in df.
    Uses weekly (7d) + 4h (3d) dual VWAP instead of weekly + monthly.
    Returns a signal dict ready for DB insertion and local transition reporting.
    """
    today = date.today()

    # Guard: need at least WEEKLY_WINDOW rows for VWAP to be meaningful
    if len(df) < WEEKLY_WINDOW:
        return _no_signal(symbol, today, "insufficient_data")

    latest = df.tail(1).to_dicts()[0]
    close       = latest.get("close")
    weekly_vwap = latest.get("weekly_vwap")
    ema12       = latest.get("ema12")
    ema25       = latest.get("ema25")

    if any(v is None for v in [close, weekly_vwap, ema12, ema25]):
        return _no_signal(symbol, today, "missing_indicator_data",
                          weekly_vwap=weekly_vwap, vwap_4h=vwap_4h,
                          ema12=ema12, ema25=ema25, close=close)

    # -----------------------------------------------------------------------
    # Step 1 — Dual VWAP bias (setup direction)
    # -----------------------------------------------------------------------
    above_weekly  = close > weekly_vwap
    above_4h      = close > vwap_4h if vwap_4h is not None else above_weekly

    if above_weekly == above_4h:
        bias = "long" if above_weekly else "short"
    else:
        return _no_signal(symbol, today, "vwap_split",
                          weekly_vwap=weekly_vwap, vwap_4h=vwap_4h,
                          ema12=ema12, ema25=ema25, close=close)

    # -----------------------------------------------------------------------
    # Step 2 — Acceptance filter (4-of-5 daily closes)
    # -----------------------------------------------------------------------
    accept_count, accept_side = _acceptance_count(df)
    expected_side = "above" if bias == "long" else "below"

    # Acceptance is now a BOOSTER/DAMPENER, not a hard gate.
    # Only the dual-VWAP *split* (above) is a hard no_signal.
    acceptance_ok = (accept_side == expected_side and accept_count >= ACCEPTANCE_MIN)
    if not acceptance_ok:
        # fall through to scoring with a dampened signal; don't early-return
        signal_override = "no_signal"
    else:
        signal_override = None

    # -----------------------------------------------------------------------
    # Step 3 — Conviction scoring
    # -----------------------------------------------------------------------
    score = 0

    # HMM regime alignment (booster/dampener)
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

    # Acceptance strength as booster (4/5 = +1, 5/5 = +2); weak acceptance dampens
    if accept_count == ACCEPTANCE_WINDOW:
        score += 2
    elif accept_count >= ACCEPTANCE_MIN:
        score += 1
    else:
        score -= 1   # <4/5 → dampen (acceptance_not_met)

    # Strong distance from 4h VWAP (>= 0.5% beyond)
    if vwap_4h is not None:
        vwap_4h_dist = abs(close - vwap_4h) / vwap_4h
        if vwap_4h_dist >= VWAP_DIST_STRONG:
            score += 1

    # Conviction level
    if score >= HIGH_SCORE:
        conviction = "HIGH"
    elif score >= MODERATE_SCORE:
        conviction = "MODERATE"
    else:
        conviction = "LOW"

    # -----------------------------------------------------------------------
    # Step 4 — Trade Levels (SL/TP) Calculation
    # -----------------------------------------------------------------------
    atr_val = latest.get("atr14")
    if atr_val is None or atr_val <= 0.0:
        atr_val = 0.02 * close  # fallback to 2% volatility

    # Stop Loss anchored at the further VWAP (invalidation point) or 1.5*ATR
    vwap_4h_pivot = vwap_4h if vwap_4h is not None else weekly_vwap
    sl_pivot = min(weekly_vwap, vwap_4h_pivot) if bias == "long" else max(weekly_vwap, vwap_4h_pivot)
    risk = max(abs(close - sl_pivot), 1.5 * atr_val)

    if bias == "long":
        sl = close - risk
        tp1 = close + 1.5 * risk
        tp2 = close + 3.0 * risk
    else:
        sl = close + risk
        tp1 = close - 1.5 * risk
        tp2 = close - 3.0 * risk

    vwap_4h_out = round(vwap_4h, 6) if vwap_4h is not None else None

    return {
        "date":             today,
        "underlying":       symbol,
        "signal":           signal_override if signal_override else bias,
        "no_signal_reason": ("acceptance_not_met" if signal_override else None),
        "conviction":       conviction,
        "conviction_score": score,
        "regime":           regime_label,
        "regime_conf":      round(regime_conf, 4),
        "weekly_vwap":      round(weekly_vwap, 6),
        "monthly_vwap":     vwap_4h_out,
        "ema12":            round(ema12, 6),
        "ema25":            round(ema25, 6),
        "ema_aligned":      ema_aligned,
        "acceptance":       accept_count,
        "close_price":      round(close, 6),
        "sl":               round(sl, 6),
        "tp1":              round(tp1, 6),
        "tp2":              round(tp2, 6),
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
        "monthly_vwap":     _round_safe(extras.get("vwap_4h")),
        "ema12":            _round_safe(extras.get("ema12")),
        "ema25":            _round_safe(extras.get("ema25")),
        "ema_aligned":      None,
        "acceptance":       extras.get("acceptance"),
        "close_price":      _round_safe(extras.get("close")),
        "sl":               None,
        "tp1":              None,
        "tp2":              None,
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

def _get_universe(conn) -> list[str]:
    """
    Returns all distinct underlyings available in source_observations table (post futures_data drop).
    """
    rows = conn.execute("SELECT DISTINCT asset FROM source_observations ORDER BY asset").fetchall()
    return [r[0] for r in rows if r[0]]


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
    vwap_4h_side = "above" if sig["signal"] == "long" else "below"
    weekly_side  = vwap_4h_side

    return (
        f"📡 *REGIME SIGNAL — {direction}*\n\n"
        f"• *Asset:* #{sig['underlying']}  |  *Conviction:* {icon} {sig['conviction']} (score: {sig['conviction_score']}/6)\n"
        f"• *Price:* {_fmt_price(sig['close_price'])}\n\n"
        f"🎯 *Levels:*\n"
        f"  ▫️ *Entry:* {_fmt_price(sig['close_price'])} (Market Entry)\n"
        f"  ▫️ *Stop Loss:* {_fmt_price(sig['sl'])}\n"
        f"  ▫️ *Target 1 (1.5R):* {_fmt_price(sig['tp1'])}\n"
        f"  ▫️ *Target 2 (3.0R):* {_fmt_price(sig['tp2'])}\n\n"
        f"*Setup:*\n"
        f"  ▫️ Weekly VWAP: {_fmt_price(sig['weekly_vwap'])}  — price {weekly_side} ✅\n"
        f"  ▫️ 4h (3d) VWAP: {_fmt_price(sig['monthly_vwap'])}  — price {vwap_4h_side} ✅\n"
        f"  ▫️ Acceptance:  {accept_str}\n\n"
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
             ema_aligned, acceptance, close_price, sl, tp1, tp2)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        sig["date"], sig["underlying"], sig["signal"], sig["no_signal_reason"],
        sig["conviction"], sig["conviction_score"],
        sig["regime"], sig["regime_conf"],
        sig["weekly_vwap"], sig["monthly_vwap"],
        sig["ema12"], sig["ema25"],
        sig["ema_aligned"], sig["acceptance"], sig["close_price"],
        sig["sl"], sig["tp1"], sig["tp2"],
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
    Writes results to DB and records HIGH conviction setup transitions locally.

    Args:
        conn     : DuckDB connection (writable)
        symbols  : list of symbols to process; if None, uses full universe
        dry_run  : if True, prints signal output but does not write DB
    """
    config.init_db()

    universe = symbols or _get_universe(conn)
    print(f"Running regime signals for {len(universe)} symbols...")

    results = []
    for symbol in universe:
        print(f"  Processing {symbol}...")

        # Load daily data from DB
        df = load_daily_bars(conn, symbol)
        if df is None:
            continue

        # Feature engineering
        df = compute_features(df)

        # HMM regime (trained on 1-hour bars for sufficient sample size)
        hmm_df = prepare_hmm_data(conn, symbol)
        if hmm_df is not None:
            regime_label, regime_conf = fit_hmm(hmm_df, symbol)
        else:
            regime_label, regime_conf = "unknown", 0.0

        # Compute 4h VWAP (3-day rolling)
        vwap_4h = compute_vwap_4h(conn, symbol)

        # Signal logic
        sig = compute_signal(symbol, df, regime_label, regime_conf, vwap_4h=vwap_4h)
        results.append(sig)

        if dry_run:
            _print_signal(sig)
            continue

        # Get previous signal for transition detection
        prev = _get_previous_signal(conn, symbol)

        # Write to DB
        _upsert_signal(conn, sig)
        conn.commit()

        # Preserve transition monitoring without legacy transport.
        _handle_alert_transition(sig, prev)

    return results


def _handle_alert_transition(sig: dict, prev: Optional[dict]):
    """Report HIGH conviction transitions without sending a Telegram message."""
    symbol = sig["underlying"]
    is_high = sig["conviction"] == "HIGH" and sig["signal"] != "no_signal"
    was_high = prev and prev["conviction"] == "HIGH" and prev["signal"] != "no_signal"

    if is_high and not was_high:
        # New HIGH conviction setup
        print(f"  [{symbol}] NEW HIGH conviction {sig['signal'].upper()} — recorded; Telegram delivery is disabled here.")

    elif is_high and was_high and prev["signal"] != sig["signal"]:
        # Direction flip at HIGH conviction
        print(f"  [{symbol}] HIGH conviction direction flip — recorded; Telegram delivery is disabled here.")

    elif was_high and sig["signal"] == "no_signal":
        # HIGH setup just closed
        print(f"  [{symbol}] 🚫 HIGH setup closed — logged only (invalidation alert disabled).")

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
                        help="Process but do not write DB")
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
    Uses DB for data but skips DB writes.
    """
    conn = config.get_db_connection(read_only=False)
    try:
        pilot = [symbol] if symbol else ["BTC", "ETH"]
        print(f"\n{'='*70}")
        print(f"BACKTEST MODE — last {n_days} bars — symbols: {pilot}")
        print(f"{'='*70}")

        for sym in pilot:
            df = load_daily_bars(conn, sym)
            if df is None:
                continue
            df = compute_features(df)

            # Pre-load 4h bars with rolling VWAP for lookups
            df_4h = load_4h_bars(conn, sym)
            vwap_4h_series = None
            vwap_4h_timestamps = None
            if df_4h is not None:
                vwap_4h_series = df_4h["vwap_4h_3d"].to_list()
                vwap_4h_timestamps = df_4h["timestamp"].to_list()

            # Pre-load 1h HMM training data
            hmm_df = prepare_hmm_data(conn, sym)

            print(f"\n--- {sym} ---")
            print(f"{'Date':<12} {'Signal':<10} {'Conv':<10} {'Score':>6}  {'Regime':<14} {'RegConf':>8}  {'Accept':>7}  {'EMA_ok':>7}  Reason")
            print("-" * 110)

            total_rows = len(df)
            start_idx  = max(WEEKLY_WINDOW, total_rows - n_days)

            for i in range(start_idx, total_rows):
                slice_df = df[:i + 1]
                slice_date = slice_df["timestamp"].tail(1)[0]

                # Find the 4h VWAP as of this date
                vwap_4h = None
                if vwap_4h_timestamps:
                    for ts, v in reversed(list(zip(vwap_4h_timestamps, vwap_4h_series))):
                        if ts <= slice_date and v is not None:
                            vwap_4h = v
                            break

                try:
                    if hmm_df is not None:
                        regime_label, regime_conf = fit_hmm(hmm_df, sym)
                    else:
                        regime_label, regime_conf = "unknown", 0.0
                    sig = compute_signal(sym, slice_df, regime_label, regime_conf, vwap_4h=vwap_4h)
                    sig["date"] = slice_date.date()
                    _print_signal(sig)
                except Exception as e:
                    print(f"    Error at row {i}: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
