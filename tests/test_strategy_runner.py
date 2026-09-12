import tempfile
import threading
import time
import unittest
import socket
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import config
import strategy_runner
import strategy_plugins


class StrategyRunnerContractTests(unittest.TestCase):
    def test_json_safe_normalizes_nested_timestamps(self):
        value = strategy_runner.json_safe({
            "observed_at": datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
            "nested": (Path("/tmp/example"),),
        })
        self.assertEqual(value["observed_at"], "2026-09-12T12:00:00+00:00")
        self.assertEqual(value["nested"], ["/tmp/example"])

    def test_json_safe_rejects_non_finite_numbers(self):
        with self.assertRaises(ValueError):
            strategy_runner.json_safe(float("nan"))

    def test_manifest_identity_and_cutoff_are_validated(self):
        manifest = {
            "protocol_version": strategy_runner.PROTOCOL_VERSION,
            "configuration_fingerprint": "fingerprint",
            "strategies": [],
            "manifest_id": "wrong",
        }
        with self.assertRaisesRegex(ValueError, "configuration fingerprint"):
            strategy_runner._plugins_for_manifest(manifest)


class StrategyRunnerTransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.directory.name) / "runner.sock"
        self.prev_db = config.ANALYST_DB_PATH
        self.prev_market = config.MARKET_DB_PATH
        self.prev_enabled = config.STRATEGY_ENABLED_IDS
        self.prev_active = config.STRATEGY_ACTIVE_IDS
        config.ANALYST_DB_PATH = str(Path(self.directory.name) / "analyst.sqlite3")
        config.MARKET_DB_PATH = str(Path(self.directory.name) / "market.sqlite3")
        config.STRATEGY_ENABLED_IDS = ("failed-break-v3",)
        config.STRATEGY_ACTIVE_IDS = ("failed-break-v3",)
        config.init_analyst_db(config.ANALYST_DB_PATH)

    def tearDown(self):
        config.ANALYST_DB_PATH = self.prev_db
        config.MARKET_DB_PATH = self.prev_market
        config.STRATEGY_ENABLED_IDS = self.prev_enabled
        config.STRATEGY_ACTIVE_IDS = self.prev_active
        self.directory.cleanup()

    def _request(self):
        return strategy_runner.make_request(
            "5m:2026-09-12T12:00:00Z",
            datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
            "5m",
            {"assets": ["BTC"], "metadata": {"feed_id": "feed-1"},
             "cutoff_at": datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)},
            {"mode": "shadow", "decisions": {}},
            db_path=config.ANALYST_DB_PATH,
            market_db_path=config.MARKET_DB_PATH,
            now=datetime(2026, 9, 12, 12, 1, tzinfo=timezone.utc),
        )

    def _start_server(self):
        server = strategy_runner.StrategyRunnerServer(self.socket_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while not self.socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.socket_path.exists())
        return server, thread

    def test_request_round_trip_and_restart(self):
        result = {
            "_attempted_symbols": 1,
            "_strategy_scopes": {},
            "failed-break-v3": {"emitted": 0, "events": []},
            "_candidates": [],
        }
        with patch.object(strategy_runner, "evaluate_strategy_plugins", return_value=result):
            server, thread = self._start_server()
            try:
                # The oxmgr health probe only opens and closes a connection.
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.connect(str(self.socket_path))
                first = strategy_runner.request_strategy_evaluation(
                    self._request(), socket_path=self.socket_path
                )
                self.assertEqual(first["_candidates"], [])
            finally:
                server.close()
                thread.join(timeout=2)

            server, thread = self._start_server()
            try:
                second = strategy_runner.request_strategy_evaluation(
                    self._request(), socket_path=self.socket_path
                )
                self.assertEqual(second["_candidates"], first["_candidates"])
                self.assertEqual(second["_runner_cutoff_id"], first["_runner_cutoff_id"])
                self.assertEqual(second["_strategy_manifest"], first["_strategy_manifest"])
            finally:
                server.close()
                thread.join(timeout=2)

    def test_runner_error_is_returned_without_falling_back_in_process(self):
        with patch.object(
            strategy_runner,
            "evaluate_strategy_plugins",
            side_effect=RuntimeError("runner fixture failure"),
        ):
            server, thread = self._start_server()
            try:
                with self.assertRaisesRegex(RuntimeError, "runner fixture failure"):
                    strategy_runner.request_strategy_evaluation(
                        self._request(), socket_path=self.socket_path
                    )
            finally:
                server.close()
                thread.join(timeout=2)

    def test_cutoff_id_mismatch_is_rejected(self):
        request = self._request()
        request["cutoff_at"] = "2026-09-12T12:05:00+00:00"
        with self.assertRaisesRegex(ValueError, "does not match"):
            strategy_runner.evaluate_request(request)

    def test_interval_invocation_routes_through_runner(self):
        old_enabled = config.STRATEGY_RUNNER_ENABLED
        config.STRATEGY_RUNNER_ENABLED = True
        cutoff = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        request = {"request_id": "request-1"}
        result = {"_candidates": [], "_strategy_scopes": {}}
        try:
            with patch.object(strategy_runner, "make_request", return_value=request) as make_request, \
                 patch.object(strategy_runner, "request_strategy_evaluation", return_value=result) as request_runner, \
                 patch.object(strategy_plugins, "process_strategy_result", return_value={"processed": True}) as process:
                output = strategy_plugins.invoke_plugins_for_intervals(
                    config.ANALYST_DB_PATH,
                    now=cutoff,
                    eval_intervals=["5m"],
                    cutoff_at=cutoff,
                    market_db_path=config.MARKET_DB_PATH,
                    regime_scope={"mode": "shadow"},
                    effective_universe={
                        "assets": ["BTC"],
                        "metadata": {"feed_id": "feed-1"},
                        "cutoff_at": cutoff,
                    },
                )
            make_request.assert_called_once()
            request_runner.assert_called_once_with(request)
            process.assert_called_once()
            self.assertEqual(output, {"5m": {"processed": True}})
        finally:
            config.STRATEGY_RUNNER_ENABLED = old_enabled

    def test_runner_candidates_cross_existing_downstream_seam(self):
        cutoff = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        candidate = {
            "candidate_id": "candidate-1",
            "strategy_id": "failed-break-v3",
            "plugin_version": "v3",
            "asset": "BTC",
            "direction": "long",
            "observed_at": cutoff.isoformat(),
            "valid_until": "2026-09-12T12:05:00+00:00",
            "entry_price": 100.0,
            "invalidation_price": 95.0,
            "targets": [110.0],
            "feature_snapshot": {},
            "data_freshness_seconds": 0.0,
            "eval_interval": "5m",
        }
        strategy_result = {
            "_strategy_scopes": {"failed-break-v3": {"allowed_assets": ["BTC"]}},
            "failed-break-v3": {"emitted": 1, "events": [candidate]},
            "_candidates": [candidate],
        }
        downstream = {"results": [], "selected_candidate_ids": []}
        with patch.object(strategy_plugins, "direct_htf_context_active", return_value=True), \
             patch.object(strategy_plugins, "direct_htf_context_evaluation_cutoff", return_value=cutoff), \
             patch.object(strategy_plugins, "shared_computation_context_active", return_value=True), \
             patch.object(strategy_plugins, "shared_computation_stats", return_value={}), \
             patch.object(strategy_plugins, "build_structural_contexts", return_value={}), \
             patch.object(strategy_plugins, "load_bars_for_interval", return_value=None), \
             patch.object(strategy_plugins, "resolve", return_value=downstream) as resolve, \
             patch.object(strategy_plugins, "write_event") as write_event:
            strategy_plugins.process_strategy_result(
                config.ANALYST_DB_PATH,
                "5m:2026-09-12T12:00:00Z",
                strategy_result,
                now=cutoff,
                require_finalized=False,
                snapshot={
                    "cutoff_at": cutoff,
                    "eval_interval": "5m",
                    "market_db_path": config.MARKET_DB_PATH,
                    "effective_universe": {
                        "assets": ["BTC"],
                        "metadata": {"feed_id": "feed-1"},
                    },
                    "regime_scope": {"mode": "shadow", "decisions": {}},
                    "feature_snapshots": {},
                },
                market_db_path=config.MARKET_DB_PATH,
            )
        resolve.assert_called_once()
        self.assertEqual(resolve.call_args.args[0], [candidate])
        write_event.assert_not_called()


if __name__ == "__main__":
    unittest.main()
