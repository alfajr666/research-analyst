import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import polars as pl

from intent_outbox import build_executor_intent, validate_geometry
from strategies.v2.ema7_26_cross_hammer_shooting_star_v1 import evaluate_symbol
from trade_admission import admit


def _bars(count, minutes, close=100.0, end=None):
    end = end or datetime(2026, 8, 1, tzinfo=timezone.utc)
    start = end - timedelta(minutes=minutes * (count - 1))
    return pl.DataFrame([
        {"timestamp": start + timedelta(minutes=minutes * i), "open": close,
         "high": close + 0.5, "low": close - 0.5, "close": close, "volume": 1.0}
        for i in range(count)
    ])


class Ema7CrossHammerE2ETests(unittest.TestCase):
    def test_candidate_admits_and_executor_derives_external_2r(self):
        cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
        bars5 = _bars(40, 5, end=cutoff).with_columns(
            pl.when(pl.arange(0, 40) == 38).then(pl.lit(99.8)).otherwise(pl.col("open")).alias("open"),
            pl.when(pl.arange(0, 40) == 38).then(pl.lit(100.05)).otherwise(
                pl.when(pl.arange(0, 40) == 39).then(pl.lit(101.5)).otherwise(pl.col("high"))
            ).alias("high"),
            pl.when(pl.arange(0, 40) == 39).then(pl.lit(101.0)).otherwise(pl.col("close")).alias("close"),
            pl.when(pl.arange(0, 40) == 38).then(pl.lit(98.0)).otherwise(
                pl.when(pl.arange(0, 40) == 39).then(pl.lit(99.5)).otherwise(pl.col("low"))
            ).alias("low"),
        )
        with patch(
            "strategies.v2.ema7_26_cross_hammer_shooting_star_v1._dmi_adx",
            return_value=(20.0, 30.0, 10.0),
        ), patch(
            "strategies.v2.ema7_26_cross_hammer_shooting_star_v1._rsi_series",
            return_value=[50.0] * 40,
        ), patch(
            "strategies.v2.ema7_26_cross_hammer_shooting_star_v1.wilder_atr",
            return_value=1.0,
        ):
            event = evaluate_symbol(
                bars5, _bars(60, 60, end=cutoff),
                asset="BTC", symbol="BTCUSDT", cutoff=cutoff,
            )
        self.assertIsNotNone(event)
        event["candidate_id"] = "ema7-cross-candidate-1"
        event["atr14_4h"] = 1.0
        event["data_freshness_seconds"] = 1.0
        observed = datetime.fromisoformat(event["observed_at"])
        boundary = event["entry_price"] - 1.0
        event["structural_context"] = {
            "cutoff": observed,
            "zones": [{
                "zone_id": "zone-ema7", "type": "order_block", "timeframe": "4h",
                "direction": "bullish", "low": boundary, "high": boundary,
                "state": "active", "created_at": observed - timedelta(hours=4),
                "coverage_status": "covered", "source_evidence_ids": ["bar-1"],
            }],
            "atr_by_timeframe": {"4h": 1.0},
        }
        self.assertEqual(admit(event, now=observed + timedelta(minutes=1))["hard_gate"], "pass")
        intent = build_executor_intent(event)
        self.assertAlmostEqual(intent["take_profit"], 109.0)
        self.assertEqual(intent["stop_loss"], 97.0)
        self.assertTrue(validate_geometry(intent)[0])
        self.assertNotIn("quantity", intent["metadata"])


if __name__ == "__main__":
    unittest.main()
