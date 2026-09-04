import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import polars as pl

import config
from intent_outbox import build_executor_intent, validate_geometry
from strategy_plugins import _REGISTRY
from strategies.v2.ema9_adx_stochrsi_state_v1 import STRATEGY_ID, evaluate_symbol
from trade_admission import admit


def _bars(count, minutes, close=100.0):
    end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    start = end - timedelta(minutes=minutes * (count - 1))
    return pl.DataFrame([
        {
            "timestamp": start + timedelta(minutes=minutes * i),
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1.0,
        }
        for i in range(count)
    ])


class Ema9AdxStochRsiStateE2ETests(unittest.TestCase):
    def test_selected_candidate_becomes_executor_2r_intent_without_sizing(self):
        bars1 = _bars(80, 1).with_columns(
            pl.when(pl.arange(0, 80) == 79)
            .then(pl.lit(101.0))
            .otherwise(pl.col("close"))
            .alias("close")
        )
        bars5 = _bars(40, 5).with_columns(
            pl.lit(100.5).alias("high"),
            pl.lit(99.5).alias("low"),
        )
        k = [50.0] * 80
        d = [50.0] * 80
        k[-2], d[-2], k[-1], d[-1] = 20.0, 30.0, 40.0, 30.0
        with patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._stoch_values",
            return_value=([50.0] * 80, k, d),
        ), patch(
            "strategies.v2.ema9_adx_stochrsi_state_v1._dmi_adx",
            return_value=(21.0, 30.0, 10.0),
        ):
            event = evaluate_symbol(
                bars1, bars5, _bars(60, 60), asset="BTC",
                symbol="BTCUSDT", cutoff=bars1["timestamp"][-1],
            )
        self.assertIsNotNone(event)
        event["candidate_id"] = "candidate-1"
        event["atr14_4h"] = 1.0
        event["data_freshness_seconds"] = 1.0
        observed = datetime.fromisoformat(event["observed_at"])
        boundary = event["entry_price"] - 1.0
        event["structural_context"] = {
            "asset": "BTC",
            "cutoff": observed,
            "zones": [{
                "zone_id": "zone-ema9", "asset": "BTC", "type": "order_block", "timeframe": "4h",
                "direction": "bullish", "low": boundary, "high": boundary,
                "state": "active", "created_at": observed - timedelta(hours=4),
                "confirmed_at": observed - timedelta(hours=4),
                "coverage_status": "covered", "source_evidence_ids": ["bar-1"],
            }],
            "atr_by_timeframe": {"4h": 1.0},
            "atr_source_bar_ids": {"4h": ["bar-1"]},
        }
        result = admit(event, now=observed + timedelta(minutes=1))
        self.assertEqual(result["hard_gate"], "pass")
        self.assertAlmostEqual(event["invalidation_price"], 97.5)

        intent = build_executor_intent(event)
        self.assertEqual(intent["take_profit"], event["entry_price"] + 2 * (event["entry_price"] - event["invalidation_price"]))
        self.assertNotIn("quantity", intent["metadata"])
        self.assertNotIn("risk_amount", intent["metadata"])
        self.assertTrue(validate_geometry(intent)[0])

    def test_registered_and_enabled_after_portfolio_swap(self):
        self.assertIn(STRATEGY_ID, _REGISTRY)
        self.assertIn(STRATEGY_ID, config.STRATEGY_ENABLED_IDS)
        self.assertEqual(_REGISTRY[STRATEGY_ID].cadence, "1m")


if __name__ == "__main__":
    unittest.main()
