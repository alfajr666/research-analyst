import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import alpha_research
import config


class AlphaResearchPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = os.path.join(self.temp_dir.name, "research.db")
        config.init_db()
        self.conn = config.get_db_connection(read_only=False)

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_records_tiered_snapshot_candidate_and_outcome(self):
        observed_at = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)
        alpha_research.record_universe_snapshot(self.conn, observed_at, [
            {"binance_symbol": "BTCUSDT", "vol_24h_usd": 200_000_000, "last_price": 100_000},
            {"binance_symbol": "SMALLUSDT", "vol_24h_usd": 6_000_000, "last_price": 1.5},
        ], {"BTCUSDT"})

        tiers = self.conn.execute("""
            SELECT binance_symbol, liquidity_tier, selected_for_scan
            FROM universe_snapshots ORDER BY binance_symbol
        """).fetchall()
        self.assertEqual(tiers, [("BTCUSDT", "core", True), ("SMALLUSDT", "emerging", False)])

        candidate_id = alpha_research.record_candidate(self.conn, {
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
        alpha_research.record_outcome(self.conn, candidate_id, {
            "evaluated_at": observed_at + timedelta(hours=4),
            "outcome": "target",
            "return_4h": 0.04,
            "net_return": 0.035,
        })
        self.conn.commit()

        record = self.conn.execute("""
            SELECT c.asset, c.liquidity_tier, o.outcome, o.net_return
            FROM alpha_candidates c
            JOIN alpha_outcomes o USING (candidate_id)
        """).fetchone()
        self.assertEqual(record, ("SMALL", "emerging", "target", 0.035))


if __name__ == "__main__":
    unittest.main()
