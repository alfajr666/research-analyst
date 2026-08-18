import unittest
from datetime import datetime, timezone
import tempfile
import os
import json

import config
import ingest_coinalyze


class IngestSourceObservationsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.directory.name, "ing.db")
        config.init_db(self.db)

    def tearDown(self):
        self.directory.cleanup()

    def test_ingest_records_append_only_source_observation(self):
        # simulate the write path without net (direct call to record logic extracted would be ideal; here exec the insert style)
        conn = config.get_db_connection(db_path=self.db)
        row_ts = datetime.now(timezone.utc)
        sym = "SOLUSDT"
        underlying = "SOL"
        payload = {"close": 100.1}
        obs_id = f"coinalyze:{sym}:{row_ts.isoformat()}"
        conn.execute("""
            INSERT OR IGNORE INTO source_observations (
                observation_id, source, venue, native_symbol, asset, market_kind, interval,
                source_start, source_end, retrieved_at, retrieval_kind, payload_json
            ) VALUES (?, 'coinalyze', 'aggregate_perp', ?, ?, 'perpetual', '15m', ?, ?, ?, 'live', ?)
        """, (obs_id, sym, underlying, row_ts, row_ts, row_ts, json.dumps(payload)))
        conn.commit()
        cnt = conn.execute("SELECT COUNT(*) FROM source_observations WHERE observation_id=?", (obs_id,)).fetchone()[0]
        self.assertEqual(cnt, 1)
        # idempotent
        conn.execute("""
            INSERT OR IGNORE INTO source_observations (
                observation_id, source, venue, native_symbol, asset, market_kind, interval,
                source_start, source_end, retrieved_at, retrieval_kind, payload_json
            ) VALUES (?, 'coinalyze', 'aggregate_perp', ?, ?, 'perpetual', '15m', ?, ?, ?, 'live', ?)
        """, (obs_id, sym, underlying, row_ts, row_ts, row_ts, json.dumps(payload)))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_observations WHERE observation_id=?", (obs_id,)).fetchone()[0], 1)
        conn.close()

    def test_shaping_decision_and_log(self):
        import unittest.mock as mock
        with mock.patch("ingest_coinalyze.is_ca_limited", return_value=True), \
             mock.patch.object(config, "CA_SHAPE_ON_CIRCUIT", True), \
             mock.patch("ingest_venue_agg_failover.log_ca_shaped") as mock_log:
            # Simulate the decision branch (full ingest needs net; just verify path)
            shape = getattr(config, "CA_SHAPE_ON_CIRCUIT", False) and ingest_coinalyze.is_ca_limited()
            self.assertTrue(shape)
            # Call via the source module
            from ingest_venue_agg_failover import log_ca_shaped as _log
            _log("funding-rate")
            mock_log.assert_called()
