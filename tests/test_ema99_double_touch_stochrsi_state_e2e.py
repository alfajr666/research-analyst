import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import polars as pl

from intent_outbox import build_executor_intent, validate_geometry
from strategies.v2.ema99_double_touch_stochrsi_state_v1 import evaluate_symbol
from trade_admission import admit


def _bars(count, minutes, close=100.0, end=None):
    end = end or datetime(2026, 8, 1, tzinfo=timezone.utc)
    start = end - timedelta(minutes=minutes * (count - 1))
    return pl.DataFrame([
        {"timestamp": start + timedelta(minutes=minutes * i), "open": close,
         "high": close + 0.5, "low": close - 0.5, "close": close,
         "volume": 1.0}
        for i in range(count)
    ])


class Ema99DoubleTouchE2ETests(unittest.TestCase):
    def test_candidate_admits_and_builds_external_2r_intent(self):
        cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
        bars1 = _bars(140, 1, end=cutoff).with_columns(
            pl.when(pl.arange(0, 140) == 139)
            .then(pl.lit(101.0))
            .otherwise(pl.col("close"))
            .alias("close")
        )
        k = [50.0] * 140
        d = [50.0] * 140
        k[-2], d[-2], k[-1], d[-1] = 10.0, 20.0, 20.0, 10.0
        with patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1._stoch_values",
            return_value=([50.0] * 140, k, d),
        ), patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1._rsi_series",
            side_effect=[[50.0] * 140, [50.0] * 40],
        ), patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1._dmi_adx",
            return_value=(21.0, 30.0, 10.0),
        ), patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1._adx_series",
            return_value=[21.0] * 140,
        ), patch(
            "strategies.v2.ema99_double_touch_stochrsi_state_v1._recent_cross",
            return_value=True,
        ):
            event = evaluate_symbol(
                bars1, _bars(40, 5, end=cutoff), _bars(60, 60, end=cutoff),
                asset="BTC", symbol="BTCUSDT", cutoff=cutoff,
            )
        self.assertIsNotNone(event)
        event["candidate_id"] = "ema99-candidate-1"
        event["atr14_4h"] = 1.0
        event["data_freshness_seconds"] = 1.0
        observed = datetime.fromisoformat(event["observed_at"])
        boundary = event["entry_price"] - 1.0
        event["structural_context"] = {
            "cutoff": observed,
            "zones": [{
                "zone_id": "zone-ema99", "type": "order_block", "timeframe": "4h",
                "direction": "bullish", "low": boundary, "high": boundary,
                "state": "active", "created_at": observed - timedelta(hours=4),
                "coverage_status": "covered", "source_evidence_ids": ["bar-1"],
            }],
            "atr_by_timeframe": {"4h": 1.0},
        }
        self.assertEqual(admit(event, now=observed + timedelta(minutes=1))["hard_gate"], "pass")
        intent = build_executor_intent(event)
        self.assertAlmostEqual(intent["take_profit"], event["entry_price"] + 2 * (event["entry_price"] - event["invalidation_price"]))
        self.assertNotIn("quantity", intent["metadata"])
        self.assertTrue(validate_geometry(intent)[0])


if __name__ == "__main__":
    unittest.main()
