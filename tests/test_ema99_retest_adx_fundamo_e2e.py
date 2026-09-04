import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import polars as pl

import config
from intent_outbox import build_executor_intent, validate_geometry
from strategy_plugins import _REGISTRY
from trade_admission import admit
from strategies.v2.ema99_retest_adx_fundamo_v1 import STRATEGY_ID, evaluate_symbol


def _bars(count, minutes, close=100.0, end=None):
    end = end or datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    start = end - timedelta(minutes=minutes * (count - 1))
    return pl.DataFrame([
        {"timestamp": start + timedelta(minutes=minutes * i), "open": close,
         "high": close + 0.1, "low": close - 0.1, "close": close,
         "volume": 1.0}
        for i in range(count)
    ])


class Ema99RetestFundamoE2ETests(unittest.TestCase):
    def test_candidate_admits_and_derives_executor_target_for_fundamo(self):
        cutoff = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        bars5m = _bars(6, 5, close=100.2, end=cutoff).with_columns(
            pl.when(pl.arange(0, 6) == 5).then(pl.lit(100.05)).otherwise(pl.col("close")).alias("close"),
            pl.when(pl.arange(0, 6) == 5).then(pl.lit(99.0)).otherwise(pl.col("low")).alias("low"),
        )
        fast = [90.0, 95.0, 101.0, 102.0, 103.0, 104.0]
        slow = [100.0] * 6
        with patch(
            "strategies.v2.ema99_retest_adx_fundamo_v1.ema_series",
            side_effect=[fast, slow],
        ), patch(
            "strategies.v2.ema99_retest_adx_fundamo_v1._expand_adx_to_5m",
            return_value=[30.0] * 6,
        ), patch(
            "strategies.v2.ema99_retest_adx_fundamo_v1.wilder_atr",
            return_value=1.5,
        ):
            event = evaluate_symbol(
                bars5m, _bars(40, 60, end=cutoff), asset="BTC",
                symbol="BTCUSDT", cutoff=cutoff,
            )
        event["candidate_id"] = "ema99-retest-candidate"
        event["atr14_4h"] = 10.0
        event["data_freshness_seconds"] = 1.0
        admission = admit(event, now=cutoff + timedelta(minutes=1))
        self.assertEqual(admission["hard_gate"], "pass", admission)
        intent = build_executor_intent(event, account_id="hyro")
        self.assertEqual((intent["exchange_id"], intent["account_id"]), ("bybit", "fundamo"))
        self.assertEqual(intent["metadata"]["target_source"], "producer_derived_2r")
        self.assertTrue(validate_geometry(intent)[0])

    def test_one_registry_entry_is_not_enabled_or_active_by_default(self):
        self.assertIn(STRATEGY_ID, _REGISTRY)
        self.assertNotIn(STRATEGY_ID, config.STRATEGY_ENABLED_IDS)
        self.assertNotIn(STRATEGY_ID, config.STRATEGY_ACTIVE_IDS)


if __name__ == "__main__":
    unittest.main()
