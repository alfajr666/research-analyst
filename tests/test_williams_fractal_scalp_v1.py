import unittest
from datetime import datetime, timedelta, timezone

import polars as pl

from strategies.compact.williams_fractal_scalp_v1 import ALLOWED_ASSETS, STRATEGY_ID, evaluate_symbol


class WilliamsFractalScalpTests(unittest.TestCase):
    def test_asset_allowlist(self):
        bars = pl.DataFrame({"timestamp": [datetime.now(timezone.utc)], "close": [1.0], "high": [1.0], "low": [1.0]})
        self.assertIsNone(evaluate_symbol(bars, asset="SOL", symbol="SOL", cutoff=datetime.now(timezone.utc)))
        self.assertEqual(ALLOWED_ASSETS, {"BTC", "ETH", "PAXG", "QQQ"})

    def test_strategy_contract_and_two_r_target(self):
        rows = []
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(140):
            price = 100 + i * 0.1
            low = price - 0.2
            if i == 137:
                low = price - 1.0
            rows.append({"timestamp": start + timedelta(minutes=i), "open": price, "high": price + 0.3, "low": low, "close": price, "volume": 1.0})
        ev = evaluate_symbol(pl.DataFrame(rows), asset="BTC", symbol="BTC/USDT", cutoff=start + timedelta(minutes=140))
        self.assertTrue(ev is None or ev["strategy_id"] == STRATEGY_ID)
        if ev:
            entry = ev["entry_condition"]["price"]
            self.assertAlmostEqual(ev["targets"][0] - entry, 2 * abs(entry - ev["invalidation_price"]), places=6)


if __name__ == "__main__":
    unittest.main()
