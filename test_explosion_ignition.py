import unittest

from test_trend_acceleration import _market_frame
from explosion_ignition import BASE_WINDOW, score_ignition


class ExplosionIgnitionTests(unittest.TestCase):
    def test_rejects_fresh_breakout(self):
        asset = _market_frame("TESTUSDT_PERP.A")
        asset["underlying"] = "TEST"
        btc = _market_frame("BTCUSDT_PERP.A")
        btc["underlying"] = "BTC"
        asset.loc[len(asset) - 1, "close"] = asset.iloc[-BASE_WINDOW - 1]["high"] * 1.10

        self.assertIsNone(score_ignition(asset, btc))

    def test_rejects_uncompressed_pullback(self):
        asset = _market_frame("TESTUSDT_PERP.A")
        asset["underlying"] = "TEST"
        btc = _market_frame("BTCUSDT_PERP.A")
        btc["underlying"] = "BTC"
        asset.loc[len(asset) - BASE_WINDOW - 1:len(asset) - 2, "high"] *= 1.25

        self.assertIsNone(score_ignition(asset, btc))


if __name__ == "__main__":
    unittest.main()
