import os
import tempfile
import unittest
from pathlib import Path

import duckdb

import config
from signal_publisher import SignalPublisher


class SchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "market_data.db"

    def tearDown(self):
        self.directory.cleanup()

    def test_new_database_records_event_ledger_migration(self):
        config.init_db(self.db_path)
        connection = duckdb.connect(str(self.db_path), read_only=True)
        try:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
                ).fetchall()
            }
            self.assertTrue({
                "schema_migrations", "alpha_events", "signal_deliveries",
                "alpha_event_status_history", "alpha_candidates", "alpha_outcomes",
                "execution_deliveries",
                "alpha_confidence_observations",
            }.issubset(tables))
            self.assertEqual(
                {row[0] for row in connection.execute("SELECT version FROM schema_migrations").fetchall()},
                {
                    "2026-08-16-phase0-event-ledger",
                    "2026-08-16-phase0-operational-metrics",
                    "2026-08-16-phase1-research-ledger",
                    "2026-08-16-phase3-research-metrics",
                    "2026-08-16-phase4-research-workflow",
                    "2026-08-16-phase3-research-metrics-v2",
                    "2026-08-17-research-execution-deliveries",
                    "2026-08-17-alpha-confidence-observations",
                },
            )
        finally:
            connection.close()

    def test_prior_event_database_upgrades_without_publisher_owned_ddl(self):
        connection = duckdb.connect(str(self.db_path))
        connection.execute("""
            CREATE TABLE alpha_events (
                dedupe_key VARCHAR PRIMARY KEY, alpha_id VARCHAR NOT NULL,
                strategy_id VARCHAR NOT NULL, asset VARCHAR NOT NULL,
                direction VARCHAR NOT NULL, setup_class VARCHAR NOT NULL,
                phase VARCHAR NOT NULL, status VARCHAR NOT NULL,
                observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
                valid_until TIMESTAMP WITH TIME ZONE NOT NULL,
                event_json VARCHAR NOT NULL, persisted_at TIMESTAMP WITH TIME ZONE NOT NULL
            )
        """)
        connection.close()

        config.init_db(self.db_path)
        publisher = SignalPublisher(self.db_path, Path(self.directory.name) / "outbox")
        connection = publisher._connect()
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM alpha_event_status_history").fetchone(),
                (0,),
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info('alpha_candidates')").fetchall()
            }
            self.assertIn("promoted_alpha_id", columns)
        finally:
            connection.close()

    def test_alpha_db_contains_no_market_tables(self):
        """Publisher DB must never receive orchestrator-owned market tables (single writer topology)."""
        alpha_path = Path(self.directory.name) / "alpha_events.db"
        # Use the alpha path explicitly; current monolithic init creates market tables (will fail until split)
        config.init_db(alpha_path)
        connection = duckdb.connect(str(alpha_path), read_only=True)
        try:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
                ).fetchall()
            }
            market_tables = {
                "broad_discovery_snapshots", "discovery_watchlist_history",
                "deep_backfill_jobs", "scanner_history", "universe_snapshots",
                "binance_oi_rotation_observations", "source_observations", "cutoff_runs"
            }
            self.assertFalse(market_tables & tables, f"market tables leaked into alpha DB: {market_tables & tables}")
            self.assertIn("alpha_events", tables)
        finally:
            connection.close()

    def test_market_db_contains_orchestrator_tables(self):
        market_path = Path(self.directory.name) / "market_data.db"
        config.init_db(market_path)
        connection = duckdb.connect(str(market_path), read_only=True)
        try:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
                ).fetchall()
            }
            self.assertIn("source_observations", tables)
            # futures_data dropped post-cutover; source_observations is the market source
        finally:
            connection.close()

    def test_legacy_futures_data_is_imported_as_append_only_source_observations(self):
        """One-shot migration of legacy futures_data produces immutable coinalyze observations."""
        market_path = Path(self.directory.name) / "market.db"
        config.init_db(market_path)
        conn = duckdb.connect(str(market_path))
        # create legacy table for this migration test only
        conn.execute("""
            CREATE TABLE IF NOT EXISTS futures_data (
                timestamp TIMESTAMP WITH TIME ZONE, underlying VARCHAR, symbol VARCHAR,
                open_interest DOUBLE, funding_rate DOUBLE, predicted_funding DOUBLE,
                liquidation_long DOUBLE, liquidation_short DOUBLE, long_short_ratio DOUBLE,
                open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE
            );
        """)
        # seed some legacy rows 
        for i in range(3):
            ts = f"2026-08-01 00:{15*i:02d}:00+00"
            conn.execute("""
                INSERT INTO futures_data (timestamp, underlying, symbol, open, high, low, close, volume)
                VALUES (?, 'BTC', 'BTCUSDT_PERP.A', 100,101,99,100.5, 123)
            """, (ts,))
        conn.commit()
        conn.close()

        # now trigger import
        from config import import_legacy_futures_as_source_observations
        import_legacy_futures_as_source_observations(market_path)

        conn = duckdb.connect(str(market_path), read_only=True)
        try:
            obs = conn.execute("""
                SELECT source, venue, interval, retrieval_kind, COUNT(*)
                FROM source_observations
                GROUP BY 1,2,3,4
            """).fetchall()
            self.assertTrue(any(o[0]=="coinalyze" and o[3]=="legacy_import" for o in obs))
            # post-drop, futures may be gone; migration verified via obs
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
