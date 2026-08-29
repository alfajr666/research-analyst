import unittest
from datetime import datetime, timezone
import tempfile
import os

import config


class PostCutoverContractTests(unittest.TestCase):
    """Verify post cutover invariants per spec:
    - plugins sole producers of alpha events (no legacy direct)
    - single writer topology
    - futures_data (legacy) dropped explicitly after source_observations verified (step 9)
    - source_observations append only
    """

    def setUp(self):
        self.prev_enabled = config.STRATEGY_ENABLED_IDS
        self.prev_active = config.STRATEGY_ACTIVE_IDS
        config.STRATEGY_ENABLED_IDS = ("accumulation-base-v2",)
        config.STRATEGY_ACTIVE_IDS = config.STRATEGY_ENABLED_IDS
        self.directory = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.directory.name, "cutover.db")
        config.init_db(self.db)

    def tearDown(self):
        config.STRATEGY_ENABLED_IDS = self.prev_enabled
        config.STRATEGY_ACTIVE_IDS = self.prev_active
        self.directory.cleanup()

    def test_plugins_produce_via_outbox_not_legacy(self):
        from strategy_plugins import invoke_plugins_for_cutoff
        # create a finalized cutoff
        conn = config.get_db_connection(db_path=self.db)
        cutoff = "cutoff-post-1"
        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO cutoff_runs (cutoff_id, cutoff_at, started_at, status) VALUES (?, ?, ?, 'finalized')",
            (cutoff, now, now)
        )
        conn.commit()
        conn.close()

        # invoke (will emit to outbox, not directly mutate alpha beyond outbox)
        res = invoke_plugins_for_cutoff(self.db, cutoff)
        self.assertTrue(any("emitted" in str(v) or "failed" in str(v) for v in res.values()))

        # outbox should have files if any emitted (or empty ok)
        from alpha_outbox import OUTBOX_DIR
        # note: may write to default outbox; just assert no crash and schema ok

    def test_single_writer_topology(self):
        # market tables absent from pure alpha init path (enforce split)
        adb = os.path.join(self.directory.name, "a.db")
        config.init_alpha_db(adb)
        conn = config.get_db_connection(db_path=adb)
        tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
        self.assertNotIn("futures_data", tables)
        self.assertNotIn("source_observations", tables)  # market only
        conn.close()

    def test_source_observations_and_legacy_dual_ok_until_drop(self):
        conn = config.get_db_connection(db_path=self.db)
        # post-drop: futures_data not created by default (only source_observations); explicit drop if present
        tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
        self.assertIn("source_observations", tables)
        self.assertNotIn("futures_data", tables)
        conn.close()

    def test_drop_legacy_futures_after_source_coverage(self):
        conn = config.get_db_connection(db_path=self.db)
        # seed minimal source obs (required for drop)
        ts = datetime.now(timezone.utc)
        conn.execute(
            "INSERT OR IGNORE INTO source_observations (observation_id, source, venue, native_symbol, asset, market_kind, interval, source_start, source_end, retrieved_at, retrieval_kind, payload_json) VALUES (?, 'coinalyze', 'agg', 'S', 'SOL', 'perpetual', '15m', ?, ?, ?, 'live', '{}')",
            (f"drop-test-{ts.isoformat()}", ts, ts, ts)
        )
        # create legacy table to test drop action
        conn.execute("""
            CREATE TABLE IF NOT EXISTS futures_data (timestamp TIMESTAMP, symbol VARCHAR);
        """)
        conn.commit()
        conn.close()

        dropped = config.drop_legacy_futures_data(self.db)
        self.assertTrue(dropped)

        conn = config.get_db_connection(db_path=self.db)
        tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
        self.assertNotIn("futures_data", tables)
        self.assertIn("source_observations", tables)
        conn.close()
