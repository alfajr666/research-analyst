import unittest

import numpy as np
import pandas as pd

from freqtrade_history import BARS_PER_DAY, expansion_episodes


def _frame(rows: int, breakout_index: int | None = None) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=rows, freq="15min", tz="UTC")
    close = np.full(rows, 100.0)
    high = close * 1.002
    low = close * 0.998
    volume = np.full(rows, 100.0)
    if breakout_index is not None:
        high[breakout_index + 1:breakout_index + 17] = 114.0
    return pd.DataFrame({"date": dates, "open": close, "high": high, "low": low, "close": close, "volume": volume})


class FreqtradeExpansionEpisodeTests(unittest.TestCase):
    def test_discovers_one_episode_and_applies_cooldown(self):
        frame = _frame(BARS_PER_DAY * 12, breakout_index=BARS_PER_DAY * 10)

        episodes = expansion_episodes(frame, "TEST", _frame(BARS_PER_DAY * 12), minimum_move=0.12)

        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["asset"], "TEST")
        self.assertGreaterEqual(episodes[0]["forward_mfe"], 0.12)

    def test_ignores_subthreshold_move(self):
        frame = _frame(BARS_PER_DAY * 12, breakout_index=BARS_PER_DAY * 10)

        episodes = expansion_episodes(frame, "TEST", _frame(BARS_PER_DAY * 12), minimum_move=0.15)

        self.assertEqual(episodes, [])


if __name__ == "__main__":
    unittest.main()
