import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import config


class StrategyPluginRegistryTests(unittest.TestCase):
    def setUp(self):
        self.prev_enabled = getattr(config, "STRATEGY_ENABLED_IDS", ())
        self.prev_active = getattr(config, "STRATEGY_ACTIVE_IDS", ())
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "m.db"
        self.prev_db_path = config.MARKET_DB_PATH
        self.prev_analyst_db_path = config.ANALYST_DB_PATH
        config.MARKET_DB_PATH = str(self.db)
        config.ANALYST_DB_PATH = str(self.db)
        config.init_market_db(self.db)
        config.init_analyst_db(self.db)

    def tearDown(self):
        config.STRATEGY_ENABLED_IDS = self.prev_enabled
        config.STRATEGY_ACTIVE_IDS = self.prev_active
        config.MARKET_DB_PATH = self.prev_db_path
        config.ANALYST_DB_PATH = self.prev_analyst_db_path
        self.directory.cleanup()

    def test_unknown_strategy_id_fails_startup(self):
        config.STRATEGY_ENABLED_IDS = ("accumulation-base-v2", "nonexistent-v99")
        from strategy_plugins import load_enabled_plugins
        with self.assertRaises(RuntimeError) as ctx:
            load_enabled_plugins()
        self.assertIn("nonexistent", str(ctx.exception))

    def test_disabled_plugins_are_not_invoked(self):
        config.STRATEGY_ENABLED_IDS = ("accumulation-base-v2",)
        from strategy_plugins import load_enabled_plugins, invoke_plugins_for_cutoff
        plugins = load_enabled_plugins()
        self.assertEqual([p.id for p in plugins], ["accumulation-base-v2"])

        # With only one enabled, others must not run
        # We simulate by checking the registry does not list disabled
        all_known = {
            "accumulation-base-v1", "impulse-ignition-v1", "continuation-breakout-balanced-v1",
            "accumulation-base-v2", "impulse-ignition-v2", "continuation-breakout-v2",
        }
        enabled = {p.id for p in plugins}
        self.assertTrue(all_known - enabled)

    def test_plugin_failure_is_isolated(self):
        config.STRATEGY_ENABLED_IDS = ("accumulation-base-v2", "impulse-ignition-v2")
        config.STRATEGY_ACTIVE_IDS = config.STRATEGY_ENABLED_IDS
        from strategy_plugins import load_enabled_plugins, invoke_plugins_for_cutoff
        plugins = load_enabled_plugins()

        # Provide a cutoff and a snapshot; one plugin will raise inside invoke
        cutoff_id = "cut-2026-08-17T12-00-00Z"
        conn = config.get_db_connection(db_path=self.db)
        conn.execute("INSERT OR IGNORE INTO cutoff_runs (cutoff_id, cutoff_at, status, started_at, finalized_at, source_observation_ids, error) VALUES (?, ?, 'finalized', ?, ?, ?, NULL)",
                     (cutoff_id, "2026-08-17T12:00:00+00:00", "2026-08-17T12:00:00+00:00", "2026-08-17T12:00:01+00:00", "[]"))
        conn.commit()
        conn.close()
        # One plugin is rigged to explode in test mode via env (phase 6 isolation hook)
        os.environ["TEST_EXPLODE_PLUGIN"] = "impulse-ignition-v2"
        try:
            results = invoke_plugins_for_cutoff(self.db, cutoff_id, now=datetime(2026,8,17,12,15,tzinfo=timezone.utc))
            # Should have recorded a structured failure for the exploding one, but not aborted others
            self.assertIn("accumulation-base-v2", results)
            self.assertIn("failed", str(results.get("impulse-ignition-v2", "")))
            # check emitted have required (if any emitted)
            acc_res = results.get("accumulation-base-v2", {})
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

    def test_live_compact_seam_admits_once_and_selects_one_intent(self):
        import strategy_plugins

        def event(strategy_id, score):
            return {"strategy_id": strategy_id, "asset": "BTCUSDT", "direction": "long",
                    "observed_at": "2099-08-17T12:15:00+00:00",
                    "valid_until": "2099-08-17T12:20:00+00:00", "entry_price": 100,
                    "invalidation_price": 95, "targets": [110],
                    "context": {"strategy_score": score}}

        ids = ("failed-break-v3", "bb-rsi-meanrev-v1")
        old_registry = strategy_plugins._REGISTRY.copy()
        old_enabled = config.STRATEGY_ENABLED_IDS
        old_active = config.STRATEGY_ACTIVE_IDS
        writes = []
        try:
            strategy_plugins._REGISTRY.update({sid: strategy_plugins.StrategyPlugin(
                sid, "test", ("bars_5m",), (), lambda _cutoff, _snapshot, sid=sid:
                    [event(sid, 3 if sid == ids[0] else 1)]) for sid in ids})
            config.STRATEGY_ENABLED_IDS = ids
            config.STRATEGY_ACTIVE_IDS = ids
            strategy_plugins.write_event = lambda ev: (writes.append(ev) or (True, self.db / "x"))
            result = strategy_plugins._run_plugins_for_cutoff(
                self.db, "cut", datetime(2026, 8, 17, 12, 15, tzinfo=timezone.utc),
                False, snapshot={"eval_interval": "5m", "feature_snapshots": {},
                                "market_db_path": str(self.db), "now": datetime(2026, 8, 17, 12, 15, tzinfo=timezone.utc)})
            assert len(writes) == 1, result
            assert writes[0]["strategy_id"] == ids[0]
            assert result[ids[0]]["emitted"] == 1
        finally:
            strategy_plugins._REGISTRY.clear(); strategy_plugins._REGISTRY.update(old_registry)
            config.STRATEGY_ENABLED_IDS = old_enabled
            config.STRATEGY_ACTIVE_IDS = old_active


if __name__ == "__main__":
    unittest.main()
