import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import alpha_research
import config


class AlphaResearchPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_market_path = config.MARKET_DB_PATH
        self.old_analyst_path = config.ANALYST_DB_PATH
        config.MARKET_DB_PATH = os.path.join(self.temp_dir.name, "market.db")
        config.ANALYST_DB_PATH = os.path.join(self.temp_dir.name, "analyst.db")
        config.init_market_db()
        config.init_analyst_db()
        self.market_conn = config.get_db_connection(db_path=config.MARKET_DB_PATH)
        self.conn = config.get_db_connection(db_path=config.ANALYST_DB_PATH)

    def tearDown(self):
        self.conn.close()
        self.market_conn.close()
        config.MARKET_DB_PATH = self.old_market_path
        config.ANALYST_DB_PATH = self.old_analyst_path
        self.temp_dir.cleanup()

    def test_records_tiered_snapshot_and_candidate(self):
        observed_at = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)
        alpha_research.record_universe_snapshot(self.market_conn, observed_at, [
            {"binance_symbol": "BTCUSDT", "vol_24h_usd": 200_000_000, "last_price": 100_000},
            {"binance_symbol": "SMALLUSDT", "vol_24h_usd": 6_000_000, "last_price": 1.5},
        ], {"BTCUSDT"})

        tiers = self.market_conn.execute("""
            SELECT binance_symbol, liquidity_tier, selected_for_scan
            FROM universe_snapshots ORDER BY binance_symbol
        """).fetchall()
        self.assertEqual(tiers, [("BTCUSDT", "core", True), ("SMALLUSDT", "emerging", False)])

        candidate_id = alpha_research.record_candidate(self.conn, {
            "candidate_id": "candidate:SMALL:2026-08-16T10:00:00Z",
            "observed_at": observed_at,
            "asset": "SMALL",
            "source_symbol": "SMALLUSDT_PERP.A",
            "direction": "long",
            "setup_class": "impulse_ignition",
            "phase": "armed",
            "strategy_id": "impulse-ignition-v1",
            "liquidity_tier": "emerging",
            "status": "armed",
            "valid_until": observed_at + timedelta(minutes=90),
            "feature_snapshot": {"volume_zscore": 2.1},
        })
        self.conn.commit()

        record = self.conn.execute("""
            SELECT asset, liquidity_tier, status
            FROM alpha_candidates WHERE candidate_id = ?
        """, (candidate_id,)).fetchone()
        self.assertEqual(record, ("SMALL", "emerging", "armed"))

    def test_rejects_non_emitted_candidate_without_stable_identity(self):
        with self.assertRaisesRegex(ValueError, "explicit stable candidate_id"):
            alpha_research.record_candidate(self.conn, {
                "observed_at": datetime(2026, 8, 16, 10, tzinfo=timezone.utc),
                "asset": "SMALL",
                "setup_class": "impulse_ignition",
                "phase": "armed",
                "strategy_id": "impulse-ignition-v1",
                "liquidity_tier": "emerging",
                "status": "armed",
                "valid_until": datetime(2026, 8, 16, 11, tzinfo=timezone.utc),
            })


if __name__ == "__main__":
    unittest.main()
