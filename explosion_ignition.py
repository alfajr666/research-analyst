"""Rank pre-breakout bases, deliberately excluding established expansions and pullbacks."""

import argparse

import pandas as pd

import config
from trend_acceleration import BARS_PER_DAY, HISTORY_REQUIRED, MAX_BAR_GAP, _clamp, _safe_return


BASE_WINDOW = BARS_PER_DAY * 3
PRIOR_WINDOW = BARS_PER_DAY * 4


def score_ignition(frame: pd.DataFrame, btc: pd.DataFrame) -> dict | None:
    """Score latent upside pressure while price remains inside its completed base."""
    if len(frame) < HISTORY_REQUIRED or len(btc) < HISTORY_REQUIRED:
        return None
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    btc = btc.sort_values("timestamp").reset_index(drop=True)
    if frame["timestamp"].iloc[-HISTORY_REQUIRED:].diff().dropna().gt(MAX_BAR_GAP).any():
        return None

    close = float(frame.iloc[-1]["close"])
    base = frame.iloc[-BASE_WINDOW - 1:-1]
    prior = frame.iloc[-BASE_WINDOW - PRIOR_WINDOW - 1:-BASE_WINDOW - 1]
    base_high, base_low = float(base["high"].max()), float(base["low"].min())
    base_range = _safe_return(base_high, base_low)
    prior_range = _safe_return(float(prior["high"].max()), float(prior["low"].min()))
    compression_ratio = base_range / prior_range if prior_range > 0 else 99.0
    breakout_distance = _safe_return(close, base_high)
    one_day_return = _safe_return(close, float(frame.iloc[-BARS_PER_DAY - 1]["close"]))
    close_position = _clamp((close - base_low) / (base_high - base_low)) if base_high > base_low else 0.0

    # An armed candidate must still be inside its base, not a breakout retrace.
    inside_base = -0.03 <= breakout_distance <= 0.005
    compressed = compression_ratio <= 0.90
    not_chasing = abs(one_day_return) <= 0.08
    if not (inside_base and compressed and not_chasing):
        return None

    impulse_start = float(frame.iloc[-BASE_WINDOW - PRIOR_WINDOW - 1]["close"])
    impulse_end = float(frame.iloc[-BASE_WINDOW - 1]["close"])
    prior_impulse = _safe_return(impulse_end, impulse_start)
    # A modest prior impulse is constructive; an already parabolic impulse is not.
    impulse_score = 20.0 * _clamp((prior_impulse - 0.03) / 0.20)
    if prior_impulse > 0.50:
        impulse_score = 0.0

    compression_score = 25.0 * _clamp((0.90 - compression_ratio) / 0.55) * close_position
    base_return = _safe_return(close, float(base.iloc[0]["close"]))
    btc_base = btc[btc["timestamp"] <= frame.iloc[-1]["timestamp"]].iloc[-BASE_WINDOW:]
    btc_return = _safe_return(float(btc_base.iloc[-1]["close"]), float(btc_base.iloc[0]["close"]))
    relative_score = 15.0 * _clamp((base_return - btc_return + 0.02) / 0.10)

    base_volume = float(base["volume"].median())
    prior_volume = float(prior["volume"].median())
    volume_dryup = 1.0 - base_volume / prior_volume if prior_volume > 0 else 0.0
    volume_score = 10.0 * _clamp(volume_dryup / 0.50)

    oi_base_start = float(base.iloc[0]["open_interest"])
    oi_change = _safe_return(float(frame.iloc[-1]["open_interest"]), oi_base_start)
    price_change = _safe_return(close, float(base.iloc[0]["close"]))
    oi_pressure = oi_change - max(price_change, 0.0)
    oi_score = 20.0 * _clamp(oi_pressure / 0.12)

    funding = float(frame.iloc[-1]["funding_rate"])
    funding_history = frame.iloc[-BARS_PER_DAY * 14:-1]["funding_rate"].abs()
    funding_p90 = float(funding_history.quantile(0.90))
    funding_score = 10.0 if funding_p90 <= 0 or funding < funding_p90 else 0.0
    score = impulse_score + compression_score + relative_score + volume_score + oi_score + funding_score
    return {
        "score": round(score, 2), "asset": frame.iloc[-1]["underlying"],
        "observed_at": frame.iloc[-1]["timestamp"], "close": close,
        "prior_impulse": prior_impulse, "compression_ratio": compression_ratio,
        "breakout_distance": breakout_distance, "base_return": base_return,
        "oi_pressure": oi_pressure, "volume_dryup": volume_dryup,
        "impulse": round(impulse_score, 2), "compression": round(compression_score, 2),
        "relative_strength": round(relative_score, 2), "volume_dryup_score": round(volume_score, 2),
        "oi_pressure_score": round(oi_score, 2), "funding_neutral": round(funding_score, 2),
    }


def rank_ignition(conn, include_core: bool = False) -> list[dict]:
    data = conn.execute("""
        SELECT timestamp, underlying, symbol, open_interest, funding_rate, high, low, close, volume
        FROM futures_data WHERE close > 0 ORDER BY underlying, timestamp
    """).fetchdf()
    latest = data["timestamp"].max()
    btc = data[data["underlying"] == "BTC"]
    tiers = {
        row[0]: row[1]
        for row in conn.execute("""
            SELECT underlying, liquidity_tier
            FROM universe_snapshots
            QUALIFY ROW_NUMBER() OVER (PARTITION BY underlying ORDER BY observed_at DESC) = 1
        """).fetchall()
    }
    results = []
    for _, frame in data[data["underlying"] != "BTC"].groupby("underlying"):
        if latest - frame["timestamp"].max() > MAX_BAR_GAP:
            continue
        candidate = score_ignition(frame, btc)
        if candidate:
            candidate["liquidity_tier"] = tiers.get(candidate["asset"], "unknown")
            if candidate["liquidity_tier"] != "emerging" and not include_core:
                continue
            results.append(candidate)
    return sorted(results, key=lambda item: item["score"], reverse=True)


def main():
    parser = argparse.ArgumentParser(description="Rank pre-breakout explosion ignition candidates")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--include-core", action="store_true", help="Include core-liquidity benchmarks")
    args = parser.parse_args()
    conn = config.get_db_connection(read_only=True)
    try:
        candidates = rank_ignition(conn, include_core=args.include_core)
    finally:
        conn.close()
    print("asset       tier       score  impulse  compress  rel.str  vol.dry  oi.press  base/prev")
    print("-" * 79)
    for candidate in candidates[:args.top]:
        print(f"{candidate['asset']:<11} {candidate['liquidity_tier']:<10} {candidate['score']:>5.1f} {candidate['impulse']:>8.1f} "
              f"{candidate['compression']:>8.1f} {candidate['relative_strength']:>7.1f} "
              f"{candidate['volume_dryup_score']:>8.1f} {candidate['oi_pressure_score']:>8.1f} "
              f"{candidate['compression_ratio']:>9.2f}")


if __name__ == "__main__":
    main()
