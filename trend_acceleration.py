"""Rank perpetuals by trend re-acceleration quality using completed 15-minute bars."""

import argparse
from datetime import timedelta

import numpy as np
import pandas as pd

import alpha_research
import config


BARS_PER_DAY = 96
TREND_WINDOW = BARS_PER_DAY * 4
BASE_WINDOW = BARS_PER_DAY * 3
HISTORY_REQUIRED = TREND_WINDOW + BASE_WINDOW * 2
MAX_BAR_GAP = pd.Timedelta(minutes=20)

PRESETS = {
    "early": {
        "trend": 25.0,
        "base": 25.0,
        "acceptance": 12.0,
        "participation": 18.0,
        "relative_strength": 20.0,
    },
    "balanced": {
        "trend": 20.0,
        "base": 20.0,
        "acceptance": 25.0,
        "participation": 20.0,
        "relative_strength": 15.0,
    },
    "confirmed": {
        "trend": 15.0,
        "base": 15.0,
        "acceptance": 30.0,
        "participation": 25.0,
        "relative_strength": 15.0,
    },
}


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


def _safe_return(current: float, previous: float) -> float:
    return current / previous - 1.0 if previous and previous > 0 else 0.0


def _range_fraction(frame: pd.DataFrame) -> float:
    low = frame["low"].min()
    high = frame["high"].max()
    return (high - low) / low if low and low > 0 else 0.0


def score_trend_acceleration(frame: pd.DataFrame, btc_frame: pd.DataFrame, preset: str = "balanced") -> dict | None:
    """Score one completed-bar series against a BTC benchmark without future data."""
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset: {preset}")
    if len(frame) < HISTORY_REQUIRED or len(btc_frame) < TREND_WINDOW + 1:
        return None

    frame = frame.sort_values("timestamp").reset_index(drop=True)
    btc_frame = btc_frame.sort_values("timestamp").reset_index(drop=True)
    if frame["timestamp"].iloc[-HISTORY_REQUIRED:].diff().dropna().gt(MAX_BAR_GAP).any():
        return None
    if btc_frame["timestamp"].iloc[-TREND_WINDOW:].diff().dropna().gt(MAX_BAR_GAP).any():
        return None
    weights = PRESETS[preset]
    close = float(frame.iloc[-1]["close"])

    trend_return = _safe_return(close, float(frame.iloc[-TREND_WINDOW - 1]["close"]))
    trend_score = weights["trend"] * _clamp(trend_return / 0.20)

    base = frame.iloc[-BASE_WINDOW:]
    prior_base = frame.iloc[-BASE_WINDOW * 2:-BASE_WINDOW]
    base_range = _range_fraction(base)
    prior_range = _range_fraction(prior_base)
    compression = 1.0 - base_range / prior_range if prior_range > 0 else 0.0
    base_low = float(base["low"].min())
    base_high = float(base["high"].max())
    close_position = _clamp((close - base_low) / (base_high - base_low)) if base_high > base_low else 0.0
    base_quality = 0.7 * _clamp(compression) + 0.3 * close_position
    base_score = weights["base"] * base_quality

    breakout_level = float(base.iloc[:-1]["high"].max())
    breakout_distance = _safe_return(close, breakout_level)
    recent_closes = base.iloc[-4:]["close"]
    acceptance = sum(recent_closes > breakout_level) / len(recent_closes)
    breakout_quality = 0.7 * _clamp((breakout_distance + 0.01) / 0.04) + 0.3 * acceptance
    acceptance_score = weights["acceptance"] * breakout_quality

    volume_median = float(frame.iloc[-BARS_PER_DAY - 1:-1]["volume"].median())
    volume_ratio = float(frame.iloc[-1]["volume"]) / volume_median if volume_median > 0 else 0.0
    oi_now = float(frame.iloc[-1]["open_interest"])
    oi_1h_ago = float(frame.iloc[-5]["open_interest"])
    oi_change_1h = _safe_return(oi_now, oi_1h_ago)
    participation_quality = 0.7 * _clamp((volume_ratio - 1.0) / 2.0) + 0.3 * _clamp(oi_change_1h / 0.05)
    participation_score = weights["participation"] * participation_quality

    btc_return = _safe_return(float(btc_frame.iloc[-1]["close"]), float(btc_frame.iloc[-TREND_WINDOW - 1]["close"]))
    relative_return = trend_return - btc_return
    relative_score = weights["relative_strength"] * _clamp((relative_return + 0.02) / 0.15)

    one_day_return = _safe_return(close, float(frame.iloc[-BARS_PER_DAY - 1]["close"]))
    funding = float(frame.iloc[-1]["funding_rate"])
    funding_history = frame.iloc[-BARS_PER_DAY * 14:-1]["funding_rate"].abs()
    funding_p90 = float(funding_history.quantile(0.90)) if not funding_history.empty else 0.0
    current_range = (float(frame.iloc[-1]["high"]) - float(frame.iloc[-1]["low"])) / close
    trailing_ranges = (
        (frame.iloc[-BARS_PER_DAY * 14:-1]["high"] - frame.iloc[-BARS_PER_DAY * 14:-1]["low"])
        / frame.iloc[-BARS_PER_DAY * 14:-1]["low"]
    )
    range_p90 = float(trailing_ranges.quantile(0.90))
    extension_penalty = 10.0 * _clamp((one_day_return - 0.15) / 0.20)
    funding_penalty = 5.0 if funding_p90 > 0 and funding > funding_p90 and funding > 0 else 0.0
    range_penalty = 5.0 if range_p90 > 0 and current_range > range_p90 * 1.25 else 0.0
    risk_penalty = extension_penalty + funding_penalty + range_penalty

    raw_score = trend_score + base_score + acceptance_score + participation_score + relative_score
    score = max(0.0, min(100.0, raw_score - risk_penalty))
    return {
        "score": round(score, 2),
        "risk_penalty": round(risk_penalty, 2),
        "trend": round(trend_score, 2),
        "base": round(base_score, 2),
        "acceptance": round(acceptance_score, 2),
        "participation": round(participation_score, 2),
        "relative_strength": round(relative_score, 2),
        "close": close,
        "breakout_level": breakout_level,
        "trend_return_4d": trend_return,
        "relative_return_4d": relative_return,
        "base_range": base_range,
        "compression": compression,
        "breakout_distance": breakout_distance,
        "volume_ratio": volume_ratio,
        "oi_change_1h": oi_change_1h,
        "one_day_return": one_day_return,
        "funding_rate": funding,
        "observed_at": frame.iloc[-1]["timestamp"],
    }


def rank_universe(conn, preset: str = "balanced") -> list[dict]:
    """Rank all sufficiently observed CoinAnalyze perps against BTC's matching history."""
    data = conn.execute("""
        SELECT timestamp, underlying, symbol, open_interest, funding_rate, open, high, low, close, volume
        FROM futures_data
        WHERE close > 0
        ORDER BY underlying, timestamp
    """).fetchdf()
    if data.empty:
        return []

    btc = data[data["underlying"] == "BTC"]
    if btc.empty:
        raise ValueError("BTC benchmark history is required for trend-acceleration ranking")

    latest_timestamp = data["timestamp"].max()

    tiers = {
        row[0]: row[1]
        for row in conn.execute("""
            SELECT underlying, liquidity_tier
            FROM universe_snapshots
            QUALIFY ROW_NUMBER() OVER (PARTITION BY underlying ORDER BY observed_at DESC) = 1
        """).fetchall()
    }
    results = []
    for underlying, frame in data.groupby("underlying"):
        if underlying == "BTC":
            continue
        if latest_timestamp - frame["timestamp"].max() > MAX_BAR_GAP:
            continue
        score = score_trend_acceleration(frame, btc, preset=preset)
        if score is None:
            continue
        score.update({
            "asset": underlying,
            "source_symbol": frame.iloc[-1]["symbol"],
            "liquidity_tier": tiers.get(underlying, "unknown"),
            "preset": preset,
        })
        results.append(score)
    return sorted(results, key=lambda item: item["score"], reverse=True)


def replay_scores(frame: pd.DataFrame, btc_frame: pd.DataFrame, preset: str, horizon_bars: int) -> list[dict]:
    """Re-score historical bars using only history available at each observation time."""
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    btc_frame = btc_frame.sort_values("timestamp").reset_index(drop=True)
    results = []
    for index in range(HISTORY_REQUIRED - 1, len(frame) - horizon_bars):
        history = frame.iloc[:index + 1]
        observed_at = history.iloc[-1]["timestamp"]
        btc_history = btc_frame[btc_frame["timestamp"] <= observed_at]
        score = score_trend_acceleration(history, btc_history, preset=preset)
        if score is None:
            continue
        entry = float(frame.iloc[index]["close"])
        future = frame.iloc[index + 1:index + horizon_bars + 1]
        future_return = _safe_return(float(future.iloc[-1]["close"]), entry)
        max_favorable = _safe_return(float(future["high"].max()), entry)
        max_adverse = _safe_return(float(future["low"].min()), entry)
        results.append({
            **score,
            "forward_return": future_return,
            "max_favorable_excursion": max_favorable,
            "max_adverse_excursion": max_adverse,
        })
    return results


def replay_symbol(conn, symbol: str, preset: str, horizon_hours: int) -> list[dict]:
    """Replay one symbol against BTC to inspect a case study without look-ahead."""
    data = conn.execute("""
        SELECT timestamp, underlying, symbol, open_interest, funding_rate, open, high, low, close, volume
        FROM futures_data
        WHERE underlying IN (?, 'BTC') AND close > 0
        ORDER BY timestamp
    """, (symbol,)).fetchdf()
    frame = data[data["underlying"] == symbol]
    btc = data[data["underlying"] == "BTC"]
    if frame.empty or btc.empty:
        raise ValueError(f"Missing {symbol} or BTC history")
    return replay_scores(frame, btc, preset=preset, horizon_bars=horizon_hours * 4)


def record_ranked_candidates(conn, candidates: list[dict], minimum_score: float):
    """Persist selected ranking observations as research candidates, never execution events."""
    for candidate in candidates:
        if candidate["score"] < minimum_score:
            continue
        observed_at = candidate["observed_at"]
        alpha_research.record_candidate(conn, {
            "observed_at": observed_at,
            "asset": candidate["asset"],
            "source_symbol": candidate["source_symbol"],
            "direction": "long",
            "setup_class": "trend_reacceleration",
            "phase": "ranked",
            "strategy_id": f"trend-reacceleration-{candidate['preset']}-v1",
            "liquidity_tier": candidate["liquidity_tier"],
            "status": "research",
            "valid_until": observed_at + timedelta(hours=4),
            "entry_condition": {"type": "research_rank", "breakout_level": candidate["breakout_level"]},
            "feature_snapshot": candidate,
        })


def _print_rankings(candidates: list[dict], top: int):
    print("asset       tier       score  risk  trend  base  break  part  rel.str  vol/x  oi/1h")
    print("-" * 91)
    for candidate in candidates[:top]:
        print(
            f"{candidate['asset']:<11} {candidate['liquidity_tier']:<10} "
            f"{candidate['score']:>5.1f} {candidate['risk_penalty']:>5.1f} "
            f"{candidate['trend']:>5.1f} {candidate['base']:>5.1f} "
            f"{candidate['acceptance']:>5.1f} {candidate['participation']:>5.1f} "
            f"{candidate['relative_strength']:>7.1f} {candidate['volume_ratio']:>6.2f} "
            f"{candidate['oi_change_1h'] * 100:>+5.1f}%"
        )


def _print_replay(results: list[dict], top: int):
    print("observed_at                score  fwd/4h  mfe/4h  mae/4h  trend  base  break  part")
    print("-" * 91)
    for result in sorted(results, key=lambda item: item["score"], reverse=True)[:top]:
        print(
            f"{result['observed_at']:%Y-%m-%d %H:%M}  {result['score']:>5.1f} "
            f"{result['forward_return'] * 100:>+6.1f}% {result['max_favorable_excursion'] * 100:>+6.1f}% "
            f"{result['max_adverse_excursion'] * 100:>+6.1f}% {result['trend']:>5.1f} "
            f"{result['base']:>5.1f} {result['acceptance']:>5.1f} {result['participation']:>5.1f}"
        )


def main():
    parser = argparse.ArgumentParser(description="Rank CoinAnalyze perps by trend re-acceleration quality")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="balanced")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--record", action="store_true", help="Persist ranked observations as research candidates")
    parser.add_argument("--minimum-score", type=float, default=60.0)
    parser.add_argument("--replay-symbol", help="Replay one underlying with point-in-time scores")
    parser.add_argument("--horizon-hours", type=int, default=4)
    args = parser.parse_args()

    conn = config.get_db_connection(read_only=not args.record)
    try:
        if args.replay_symbol:
            results = replay_symbol(conn, args.replay_symbol, args.preset, args.horizon_hours)
            _print_replay(results, args.top)
            return
        candidates = rank_universe(conn, preset=args.preset)
        _print_rankings(candidates, args.top)
        if args.record:
            record_ranked_candidates(conn, candidates, args.minimum_score)
            conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
