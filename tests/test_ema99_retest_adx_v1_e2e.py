import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import polars as pl

import config
from intent_outbox import build_executor_intent, validate_geometry
from strategy_plugins import _REGISTRY
from trade_admission import admit
from strategies.v2.ema99_retest_adx_v1 import STRATEGY_ID, evaluate_symbol


def _bars(count, minutes, close=100.0, end=None):
    end = end or datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    start = end - timedelta(minutes=minutes * (count - 1))
    return pl.DataFrame([
        {"timestamp": start + timedelta(minutes=minutes * i), "open": close,
         "high": close + 0.1, "low": close - 0.1, "close": close,
         "volume": 1.0}
        for i in range(count)
    ])


def _features(bars, fast, slow, *, rsi=50.0, atr=1.5):
    return bars.with_columns(
        pl.Series(f"ema_{config.EMA99_RETEST_FAST_EMA_LENGTH}", fast),
        pl.Series(f"ema_{config.EMA99_RETEST_SLOW_EMA_LENGTH}", slow),
        pl.Series(f"rsi_{config.EMA99_RETEST_RSI_LENGTH}", [rsi] * bars.height),
        pl.Series(f"atr_{config.EMA99_RETEST_ATR_LENGTH}", [atr] * bars.height),
    )


class Ema99RetestE2ETests(unittest.TestCase):
    def test_candidate_admits_and_derives_executor_target_for_downstream_route(self):
        cutoff = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        bars5m = _bars(6, 5, close=100.2, end=cutoff).with_columns(
            pl.when(pl.arange(0, 6) == 5).then(pl.lit(100.05)).otherwise(pl.col("close")).alias("close"),
            pl.when(pl.arange(0, 6) == 5).then(pl.lit(99.0)).otherwise(pl.col("low")).alias("low"),
        )
        fast = [90.0, 95.0, 101.0, 102.0, 103.0, 104.0]
        slow = [100.0] * 6
        features = _features(bars5m, fast, slow)
        with patch(
            "strategies.v2.ema99_retest_adx_v1._expand_adx_to_5m",
            return_value=[30.0] * 6,
        ):
            event = evaluate_symbol(
                bars5m, _bars(40, 60, end=cutoff), asset="BTC",
                symbol="BTCUSDT", cutoff=cutoff,
                features5m=features,
            )
        event["candidate_id"] = "ema99-retest-candidate"
        event["atr14_4h"] = 10.0
        event["data_freshness_seconds"] = 1.0
        boundary = event["invalidation_price"] + 2.0
        event["structural_context"] = {
            "asset": "BTC",
            "cutoff": cutoff,
            "zones": [{
                "zone_id": "zone-retest", "asset": "BTC", "type": "order_block", "timeframe": "4h",
                "direction": "bullish", "low": boundary, "high": boundary,
                "state": "active", "created_at": cutoff - timedelta(hours=4),
                "confirmed_at": cutoff - timedelta(hours=4),
                "coverage_status": "covered", "source_evidence_ids": ["bar-1"],
            }],
            "atr_by_timeframe": {"4h": 1.0},
            "atr_source_bar_ids": {"4h": ["bar-1"]},
        }
        admission = admit(event, now=cutoff + timedelta(minutes=1), effective_universe=["BTC"])
        self.assertEqual(admission["hard_gate"], "pass", admission)
        self.assertNotIn("account_id", event)
        intent = build_executor_intent(event)
        self.assertEqual((intent["exchange_id"], intent["account_id"]), ("bybit", "fundamo"))
        self.assertEqual(intent["metadata"]["target_source"], "producer_derived_2r")
        self.assertTrue(validate_geometry(intent)[0])

    def test_one_registry_entry_is_enabled_and_active(self):
        self.assertIn(STRATEGY_ID, _REGISTRY)
        self.assertIn(STRATEGY_ID, config.STRATEGY_ENABLED_IDS)
        self.assertIn(STRATEGY_ID, config.STRATEGY_ACTIVE_IDS)


if __name__ == "__main__":
    unittest.main()
