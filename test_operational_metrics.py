import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

import config
import orchestrator


class OperationalMetricsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_db_path = config.DB_PATH
        config.DB_PATH = os.path.join(self.directory.name, "market_data.db")
        config.init_db()

    def tearDown(self):
        config.DB_PATH = self.previous_db_path
        self.directory.cleanup()

    def test_completed_run_persists_freshness_and_outbox_depth(self):
        run_id = "pipeline-run-1"
        orchestrator._start_pipeline_run(run_id, datetime.now(timezone.utc))
        connection = config.get_db_connection()
        try:
            ts = datetime.now(timezone.utc)
            payload = json.dumps({"close": 100})
            connection.execute(
                "INSERT OR IGNORE INTO source_observations (observation_id, source, venue, native_symbol, asset, market_kind, interval, source_start, source_end, retrieved_at, retrieval_kind, payload_json) VALUES (?, 'coinalyze', 'agg', 'SOLUSDT_PERP.A', 'SOL', 'perpetual', '15m', ?, ?, ?, 'live', ?)",
                (f"op-{ts.isoformat()}", ts, ts, ts, payload)
            )
        finally:
            connection.close()

        orchestrator._finish_pipeline_run(run_id, "completed")
        connection = config.get_db_connection(read_only=True)
        try:
            status, completed_at, freshness = connection.execute("""
                SELECT status, completed_at, data_freshness_seconds
                FROM pipeline_runs WHERE run_id = ?
            """, (run_id,)).fetchone()
        finally:
            connection.close()
        self.assertEqual(status, "completed")
        self.assertIsNotNone(completed_at)
        self.assertGreaterEqual(freshness, 0)


if __name__ == "__main__":
    unittest.main()
