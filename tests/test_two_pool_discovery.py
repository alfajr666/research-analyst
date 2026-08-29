import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import config
from two_pool_discovery import process_snapshot, rank_pools


def _record(symbol, **overrides):
    record = {
        "symbol": symbol,
        "asset": symbol.removesuffix("USDT"),
        "liquidity_tier": "emerging",
        "eligible": True,
        "data_fresh": True,
        "history_warmed": True,
        "volume_24h_usd": 10_000_000,
        "open_interest_usd": 5_000_000,
        "volume_zscore": 0.2,
        "oi_change_1h": 0.04,
        "price_change_1h": 0.003,
        "price_change_24h": 0.02,
        "price_range_percentile": 0.15,
        "funding_rate": 0.0001,
        "funding_zscore": 0.1,
        "long_short_ratio_change": 0.03,
    }
    record.update(overrides)
    return record


class TwoPoolDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = os.path.join(self.temp_dir.name, "research.db")
        config.init_db()
        self.conn = config.get_db_connection()

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_rankings_separate_quiet_ignition_from_active_continuation(self):
        ignition = _record("QUIETUSDT")
        continuation = _record("ACTIVEUSDT", volume_zscore=2.5, oi_change_1h=0.07, price_change_1h=0.025, price_range_percentile=0.8)
        thin = _record("THINUSDT", volume_24h_usd=100, open_interest_usd=10, volume_zscore=20, oi_change_1h=1, price_change_1h=0.5)
        rankings = rank_pools([ignition, continuation, thin])

        self.assertEqual(rankings["ignition"][0]["symbol"], "QUIETUSDT")
        self.assertEqual(rankings["continuation"][0]["symbol"], "ACTIVEUSDT")
        self.assertNotIn("THINUSDT", [item["symbol"] for item in rankings["continuation"]])
        self.assertNotIn("ACTIVEUSDT", [item["symbol"] for item in rankings["ignition"]])

    def test_continuation_pool_rejects_downward_movement_until_short_events_exist(self):
        falling = _record("FALLINGUSDT", volume_zscore=2.5, oi_change_1h=0.07, price_change_1h=-0.025)
        self.assertNotIn("FALLINGUSDT", [item["symbol"] for item in rank_pools([falling])["continuation"]])

    def test_watchlist_residency_and_backfill_handoff(self):
        start = datetime(2026, 8, 16, 10, tzinfo=timezone.utc)
        ranked = _record("QUIETUSDT")
        process_snapshot(self.conn, start, [ranked])
        process_snapshot(self.conn, start + timedelta(hours=1), [_record("QUIETUSDT", fresh_breakout=True)])
        process_snapshot(self.conn, start + timedelta(hours=25), [ranked])
        process_snapshot(self.conn, start + timedelta(hours=26), [_record("QUIETUSDT", fresh_breakout=True)])
        self.conn.commit()

        rows = self.conn.execute("""
            SELECT state, deep_backfill_required, expiry_reason
            FROM discovery_watchlist_history
            WHERE pool = 'ignition' AND symbol = 'QUIETUSDT'
            ORDER BY observed_at
        """).fetchall()
        self.assertEqual(rows, [
            ("entered", True, None),
            ("warming", False, None),
            ("active", False, None),
            ("expired", False, "no_longer_ranked"),
        ])
        self.assertEqual(
            self.conn.execute("SELECT status FROM deep_backfill_jobs WHERE symbol = 'QUIETUSDT'").fetchone(),
            ("pending",),
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM broad_discovery_snapshots").fetchone()[0], 4)


if __name__ == "__main__":
    unittest.main()
