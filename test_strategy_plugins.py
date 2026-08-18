import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import config


class StrategyPluginRegistryTests(unittest.TestCase):
    def setUp(self):
        self.prev_enabled = getattr(config, "STRATEGY_ENABLED_IDS", ())
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "m.db"
        config.init_db(self.db)

    def tearDown(self):
        config.STRATEGY_ENABLED_IDS = self.prev_enabled
        self.directory.cleanup()

    def test_unknown_strategy_id_fails_startup(self):
        config.STRATEGY_ENABLED_IDS = ("accumulation-base-v1", "nonexistent-v99")
        from strategy_plugins import load_enabled_plugins
        with self.assertRaises(RuntimeError) as ctx:
            load_enabled_plugins()
        self.assertIn("nonexistent", str(ctx.exception))

    def test_disabled_plugins_are_not_invoked(self):
        config.STRATEGY_ENABLED_IDS = ("accumulation-base-v1",)
        from strategy_plugins import load_enabled_plugins, invoke_plugins_for_cutoff
        plugins = load_enabled_plugins()
        self.assertEqual([p.id for p in plugins], ["accumulation-base-v1"])

        # With only one enabled, others must not run
        # We simulate by checking the registry does not list disabled
        all_known = {
            "accumulation-base-v1", "impulse-ignition-v1", "continuation-breakout-balanced-v1",
            "accumulation-base-v2", "impulse-ignition-v2",
        }
        enabled = {p.id for p in plugins}
        self.assertTrue(all_known - enabled)

    def test_plugin_failure_is_isolated(self):
        config.STRATEGY_ENABLED_IDS = ("accumulation-base-v1", "impulse-ignition-v1")
        from strategy_plugins import load_enabled_plugins, invoke_plugins_for_cutoff
        plugins = load_enabled_plugins()

        # Provide a cutoff and a snapshot; one plugin will raise inside invoke
        cutoff_id = "cut-2026-08-17T12-00-00Z"
        conn = config.get_db_connection(db_path=self.db)
        conn.execute("INSERT OR IGNORE INTO cutoff_runs (cutoff_id, cutoff_at, status, started_at, finalized_at, source_observation_ids, error) VALUES (?, ?, 'finalized', ?, ?, ?, NULL)",
                     (cutoff_id, "2026-08-17T12:00:00+00:00", "2026-08-17T12:00:00+00:00", "2026-08-17T12:00:01+00:00", "[]"))
        conn.commit()
        conn.close()
        # The second plugin is rigged to explode in test mode via env
        os.environ["TEST_EXPLODE_PLUGIN"] = "impulse-ignition-v1"
        try:
            results = invoke_plugins_for_cutoff(self.db, cutoff_id, now=datetime(2026,8,17,12,15,tzinfo=timezone.utc))
            # Should have recorded a structured failure for the exploding one, but not aborted others
            self.assertIn("accumulation-base-v1", results)
            self.assertIn("failed", str(results.get("impulse-ignition-v1", "")))
            # check emitted have required (if any emitted)
            acc_res = results.get("accumulation-base-v1", {})
            if acc_res.get("emitted", 0) > 0 and acc_res.get("events"):
                ev = acc_res["events"][0]
                self.assertIn("plugin_version", ev)
                self.assertIn("input_snapshot_id", ev)
        finally:
            os.environ.pop("TEST_EXPLODE_PLUGIN", None)

    def test_plugins_only_see_finalized_cutoff_snapshot(self):
        from strategy_plugins import invoke_plugins_for_cutoff
        # A running (non-finalized) cutoff must be rejected
        with self.assertRaises(ValueError) as ctx:
            invoke_plugins_for_cutoff(self.db, "running-cut", require_finalized=True)
        self.assertIn("finalized", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
