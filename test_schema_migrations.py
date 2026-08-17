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


if __name__ == "__main__":
    unittest.main()
