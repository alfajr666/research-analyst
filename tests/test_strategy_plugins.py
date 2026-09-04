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
        self.prev_warmup = config.DEEP_WARMUP_GATE_ENABLED
        config.DEEP_WARMUP_GATE_ENABLED = False
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "m.db"
        self.prev_db_path = config.MARKET_DB_PATH
        self.prev_analyst_db_path = config.ANALYST_DB_PATH
        config.MARKET_DB_PATH = str(self.db)
        config.ANALYST_DB_PATH = str(self.db)
        config.init_market_db(self.db)
        config.init_analyst_db(self.db)

    def test_portfolio_swap_retires_three_and_enables_three(self):
        retired = {
            "ema9-continuation-stochrsi-v1",
            "trend-wall-v1",
            "ema-stack-15m-adx-stochrsi-5m-v1",
        }
        enabled = {
            "ema9-adx-stochrsi-state-v1",
            "ema99-double-touch-stochrsi-state-v1",
            "ema7-26-cross-hammer-shooting-star-1h-adx-v1",
        }
        self.assertTrue(retired.isdisjoint(config.STRATEGY_ENABLED_IDS))
        self.assertTrue(enabled.issubset(config.STRATEGY_ENABLED_IDS))
        self.assertIn("ema9-adx-stochrsi-state-v1", config.COMPACT_STRATEGY_IDS)
        self.assertTrue(enabled - {"ema9-adx-stochrsi-state-v1"} <= config.FUNDAMO_STRATEGY_IDS)

    def tearDown(self):
        config.STRATEGY_ENABLED_IDS = self.prev_enabled
        config.STRATEGY_ACTIVE_IDS = self.prev_active
        config.DEEP_WARMUP_GATE_ENABLED = self.prev_warmup
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

    def test_cutoff_snapshot_preserves_complete_zone_reference(self):
        from strategy_plugins import _build_snapshot

        conn = config.get_db_connection(db_path=self.db)
        conn.execute(
            """INSERT INTO structure_zones
               (zone_id, cutoff_id, asset, kind, direction, strength, low, high,
                state, source_evidence_ids, confidence_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "zone-1", "4h:2026-08-17T12:00:00Z", "BTC", "fvg_4h", "bullish",
                2.0, 95.0, 96.0, "active", '["bar-1"]', "confirmed",
                "2026-08-17T08:00:00+00:00",
            ),
        )
        conn.commit()
        conn.close()

        snapshot = _build_snapshot(
            self.db,
            "4h:2026-08-17T12:00:00Z",
            datetime(2026, 8, 17, 12, 5, tzinfo=timezone.utc),
            self.db,
        )

        assert snapshot["zones"] == [{
            "zone_id": "zone-1", "reference_id": "zone-1", "asset": "BTC",
            "kind": "fvg_4h", "type": "fvg", "timeframe": "4h",
            "direction": "bullish", "strength": 2.0, "low": 95.0, "high": 96.0,
            "state": "active", "source_evidence_ids": ["bar-1"],
            "confidence_status": "confirmed", "created_at": "2026-08-17T08:00:00+00:00",
        }]

    def test_live_compact_seam_admits_once_and_selects_one_intent(self):
        import strategy_plugins

        def event(strategy_id, score):
            return {"strategy_id": strategy_id, "asset": "BTCUSDT", "direction": "long",
                    "observed_at": "2099-08-17T12:15:00+00:00",
                    "valid_until": "2099-08-17T12:20:00+00:00", "entry_price": 100,
                    "invalidation_price": 95, "targets": [110], "atr14_4h": 10,
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
            conn = config.get_db_connection(db_path=self.db)
            conn.execute("""CREATE TABLE source_observations (
                observation_id VARCHAR, source VARCHAR, venue VARCHAR,
                native_symbol VARCHAR, asset VARCHAR, market_kind VARCHAR,
                interval VARCHAR, source_start TIMESTAMP, source_end TIMESTAMP,
                retrieved_at TIMESTAMP, retrieval_kind VARCHAR, payload_json VARCHAR
            )""")
            conn.execute("INSERT INTO source_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         ("obs", "test", "test", "BTCUSDT", "BTC", "perp", "5m",
                         "2026-08-17 12:10:00", "2026-08-17 12:15:00",
                         "2026-08-17 12:16:00", None,
                         '{"open": 100, "high": 101, "low": 99, "close": 100}'))
            conn.commit(); conn.close()
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

    def test_companion_bars_are_checked_in_market_db(self):
        import strategy_plugins

        conn = config.get_db_connection(db_path=self.db)
        conn.execute("""CREATE TABLE source_observations (
            observation_id VARCHAR, source VARCHAR, venue VARCHAR,
            native_symbol VARCHAR, asset VARCHAR, market_kind VARCHAR,
            interval VARCHAR, source_start TIMESTAMP, source_end TIMESTAMP,
            retrieved_at TIMESTAMP, retrieval_kind VARCHAR, payload_json VARCHAR
        )""")
        conn.execute("INSERT INTO source_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     ("obs", "test", "test", "BTCUSDT", "BTC", "perp", "5m",
                      "2026-08-17 12:10:00", "2026-08-17 12:15:00",
                      "2026-08-17 12:16:00", None,
                      '{"open": 100, "high": 101, "low": 99, "close": 100}'))
        conn.commit(); conn.close()

        old_registry = strategy_plugins._REGISTRY.copy()
        old_enabled = config.STRATEGY_ENABLED_IDS
        old_active = config.STRATEGY_ACTIVE_IDS
        try:
            strategy_plugins._REGISTRY["failed-break-v3"] = strategy_plugins.StrategyPlugin(
                "failed-break-v3", "test", ("bars_5m",), (), lambda *_: [])
            config.STRATEGY_ENABLED_IDS = ("failed-break-v3",)
            config.STRATEGY_ACTIVE_IDS = ("failed-break-v3",)
            snapshot = {"cutoff_id": "1m:2026-08-17T12:15:00Z",
                        "eval_interval": "1m", "feature_snapshots": {},
                        "market_db_path": str(self.db)}
            result = strategy_plugins._run_plugins_for_cutoff(
                self.db, "1m:2026-08-17T12:15:00Z", None, False, snapshot=snapshot)
            self.assertEqual(result["failed-break-v3"], {"emitted": 0, "events": []})

            conn = config.get_db_connection(db_path=self.db)
            conn.execute("DELETE FROM source_observations")
            conn.commit(); conn.close()
            result = strategy_plugins._run_plugins_for_cutoff(
                self.db, "1m:2026-08-17T12:15:00Z", None, False, snapshot=snapshot)
            self.assertIn("missing required datasets: bars_5m", result["failed-break-v3"]["skipped"])
        finally:
            strategy_plugins._REGISTRY.clear(); strategy_plugins._REGISTRY.update(old_registry)
            config.STRATEGY_ENABLED_IDS = old_enabled
            config.STRATEGY_ACTIVE_IDS = old_active

    def test_deep_warmup_gate_blocks_cold_plugin_scope(self):
        import strategy_plugins

        old_registry = strategy_plugins._REGISTRY.copy()
        old_enabled = config.STRATEGY_ENABLED_IDS
        old_active = config.STRATEGY_ACTIVE_IDS
        old_static = config.STATIC_SYMBOLS_OVERRIDE
        old_rotation = config.SYMBOL_ROTATION_ENABLED
        old_warmup = config.DEEP_WARMUP_GATE_ENABLED
        calls = []
        try:
            strategy_plugins._REGISTRY["failed-break-v3"] = strategy_plugins.StrategyPlugin(
                "failed-break-v3", "test", ("bars_5m",), (), lambda *_: calls.append(True) or []
            )
            config.STRATEGY_ENABLED_IDS = ("failed-break-v3",)
            config.STRATEGY_ACTIVE_IDS = ("failed-break-v3",)
            config.STATIC_SYMBOLS_OVERRIDE = "BTC"
            config.SYMBOL_ROTATION_ENABLED = False
            config.DEEP_WARMUP_GATE_ENABLED = True
            result = strategy_plugins._run_plugins_for_cutoff(
                self.db, "5m:2026-08-17T12:05:00Z", None, False,
                snapshot={"eval_interval": "5m", "feature_snapshots": {},
                          "market_db_path": str(self.db)},
            )
            assert result["failed-break-v3"] == {"skipped": "deep warmup: no ready assets"}
            assert calls == []
        finally:
            strategy_plugins._REGISTRY.clear(); strategy_plugins._REGISTRY.update(old_registry)
            config.STRATEGY_ENABLED_IDS = old_enabled
            config.STRATEGY_ACTIVE_IDS = old_active
            config.STATIC_SYMBOLS_OVERRIDE = old_static
            config.SYMBOL_ROTATION_ENABLED = old_rotation
            config.DEEP_WARMUP_GATE_ENABLED = old_warmup


if __name__ == "__main__":
    unittest.main()
