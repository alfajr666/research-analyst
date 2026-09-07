import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import polars as pl

import config
from strategies.v2 import dual_zone_follower_v3
from strategy_plugins import _REGISTRY
from trade_admission import resolved_account


def _bars(closes, start=datetime(2026, 8, 1, tzinfo=timezone.utc)):
    timestamps = [start + timedelta(minutes=5 * index) for index in range(len(closes))]
    return pl.DataFrame({
        "timestamp": timestamps,
        "open": closes,
        "high": [value * 1.001 for value in closes],
        "low": [value * 0.999 for value in closes],
        "close": closes,
        "volume": [1.0] * len(closes),
    })


class DualZoneFollowerV3Tests(unittest.TestCase):
    def test_registry_and_routing_use_current_v3_ids(self):
        self.assertIn("dual-zone-follower-v3", _REGISTRY)
        self.assertIn("dual-zone-short-follower-v3", _REGISTRY)
        self.assertNotIn("dual-zone-follower-v2", _REGISTRY)
        self.assertNotIn("dual-zone-short-follower-v2", _REGISTRY)
        self.assertEqual(resolved_account("dual-zone-follower-v3"), "fundamo")
        self.assertEqual(resolved_account("dual-zone-short-follower-v3"), "fundamo")

    def test_long_and_short_candidates_have_mirrored_current_contract(self):
        cutoff = datetime(2026, 8, 1, 18, 15, tzinfo=timezone.utc)
        long_bars = _bars([100.0 + index * 0.04 for index in range(220)])
        short_bars = _bars([100.0 - index * 0.04 for index in range(220)])

        long_event = dual_zone_follower_v3.evaluate_symbol(
            long_bars, asset="BTC", symbol="BTCUSDT", cutoff=cutoff, direction="long"
        )
        short_event = dual_zone_follower_v3.evaluate_symbol(
            short_bars, asset="BTC", symbol="BTCUSDT", cutoff=cutoff, direction="short"
        )

        self.assertEqual(long_event["strategy_id"], "dual-zone-follower-v3")
        self.assertEqual(short_event["strategy_id"], "dual-zone-short-follower-v3")
        self.assertEqual(long_event["plugin_version"], "v3")
        self.assertEqual(short_event["plugin_version"], "v3")
        self.assertEqual(long_event["phase"], "channel_a")
        self.assertEqual(short_event["phase"], "channel_a")
        self.assertLess(long_event["invalidation_price"], long_event["entry_price"])
        self.assertLess(long_event["entry_price"], long_event["targets"][0])
        self.assertLess(short_event["targets"][0], short_event["entry_price"])
        self.assertLess(short_event["entry_price"], short_event["invalidation_price"])

    def test_run_plugin_requests_direct_adx_frame_at_exact_cutoff(self):
        cutoff = datetime(2026, 8, 1, 18, 15, tzinfo=timezone.utc)
        bars = _bars([100.0 + index * 0.04 for index in range(220)])
        with tempfile.TemporaryDirectory() as directory:
            market_db = Path(directory) / "market.sqlite3"
            config.init_market_db(market_db)
            requested = []

            def load_bars(_conn, _symbol, interval, requested_cutoff):
                requested.append((interval, requested_cutoff))
                return bars

            with patch.object(dual_zone_follower_v3, "evaluation_symbols", return_value=[("BTCUSDT", "BTC")]), \
                 patch.object(dual_zone_follower_v3, "cached_feature_frame", return_value=None), \
                 patch.object(dual_zone_follower_v3, "load_bars_for_interval", side_effect=load_bars), \
                 patch.object(dual_zone_follower_v3, "dmi_adx_last", return_value=(25.0, 30.0, 10.0)), \
                 patch.object(dual_zone_follower_v3, "has_active_event", return_value=False):
                events = dual_zone_follower_v3.run_plugin(
                    "5m:2026-08-01T18:15:00Z",
                    {"market_db_path": str(market_db), "cutoff_at": cutoff},
                )

        self.assertEqual(len(events), 1)
        self.assertEqual(requested, [("5m", cutoff), ("1h", cutoff)])
        self.assertEqual(events[0]["feature_snapshot"]["cutoff"], cutoff.isoformat())


if __name__ == "__main__":
    unittest.main()
