import unittest

import numpy as np
import pandas as pd

from trend_acceleration import BASE_WINDOW, HISTORY_REQUIRED, replay_scores, score_trend_acceleration


def _market_frame(symbol: str, extended: bool = False) -> pd.DataFrame:
    bars = HISTORY_REQUIRED + 8
    timestamps = pd.date_range("2026-07-01", periods=bars, freq="15min", tz="UTC")
    close = np.linspace(100.0, 120.0, bars)
    close[-BASE_WINDOW:] = np.linspace(116.0, 117.0, BASE_WINDOW)
    close[-1] = 120.0 if extended else 118.0
    high = close * 1.003
    low = close * 0.997
    volume = np.full(bars, 100.0)
    volume[-1] = 300.0
    oi = np.linspace(1_000.0, 1_100.0, bars)
    oi[-1] = 1_180.0
    funding = np.full(bars, 0.0001)
    if extended:
        close[-BARS_PER_DAY - 1] = 90.0
        funding[-1] = 0.01
        high[-1] = close[-1] * 1.10
        low[-1] = close[-1] * 0.90
    return pd.DataFrame({
        "timestamp": timestamps,
        "symbol": symbol,
        "open_interest": oi,
        "funding_rate": funding,
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })


BARS_PER_DAY = 96


class TrendAccelerationScoreTests(unittest.TestCase):
    def test_breakout_with_participation_scores_as_constructive(self):
        asset = _market_frame("TESTUSDT_PERP.A")
        btc = _market_frame("BTCUSDT_PERP.A")
        btc["close"] = np.linspace(100.0, 105.0, len(btc))
        btc["high"] = btc["close"] * 1.003
        btc["low"] = btc["close"] * 0.997

        score = score_trend_acceleration(asset, btc)

        self.assertIsNotNone(score)
        self.assertGreater(score["score"], 50.0)
        self.assertGreater(score["acceptance"], 0.0)
        self.assertGreater(score["participation"], 0.0)

    def test_late_extension_and_crowding_reduce_score(self):
        btc = _market_frame("BTCUSDT_PERP.A")
        btc["close"] = np.linspace(100.0, 105.0, len(btc))
        btc["high"] = btc["close"] * 1.003
        btc["low"] = btc["close"] * 0.997

        constructive = score_trend_acceleration(_market_frame("TESTUSDT_PERP.A"), btc)
        extended = score_trend_acceleration(_market_frame("TESTUSDT_PERP.A", extended=True), btc)

        self.assertGreater(extended["risk_penalty"], constructive["risk_penalty"])
        self.assertLess(extended["score"], constructive["score"])

    def test_gapped_history_is_rejected(self):
        asset = _market_frame("TESTUSDT_PERP.A")
        btc = _market_frame("BTCUSDT_PERP.A")
        asset.loc[len(asset) - 2, "timestamp"] = asset.loc[len(asset) - 3, "timestamp"]

        self.assertIsNone(score_trend_acceleration(asset, btc))

    def test_replay_evaluates_only_bars_after_observation(self):
        asset = _market_frame("TESTUSDT_PERP.A")
        btc = _market_frame("BTCUSDT_PERP.A")

        results = replay_scores(asset, btc, preset="balanced", horizon_bars=4)

        self.assertTrue(results)
        self.assertLess(results[-1]["observed_at"], asset.iloc[-1]["timestamp"])
        self.assertIn("forward_return", results[-1])


if __name__ == "__main__":
    unittest.main()
