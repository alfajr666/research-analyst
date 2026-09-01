import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config


class SymbolScopeE2ETests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "market.db"
        self.previous = {
            "STATIC_SYMBOLS_OVERRIDE": config.STATIC_SYMBOLS_OVERRIDE,
            "SYMBOL_ROTATION_ENABLED": config.SYMBOL_ROTATION_ENABLED,
            "SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT": config.SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT,
            "SYMBOL_ROTATION_FEED_PATH": config.SYMBOL_ROTATION_FEED_PATH,
        }
        self.assets = [f"COIN{index:02d}" for index in range(92)]
        config.STATIC_SYMBOLS_OVERRIDE = ",".join(self.assets)
        config.SYMBOL_ROTATION_ENABLED = True
        config.SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT = 30
        config.SYMBOL_ROTATION_FEED_PATH = self.db.with_name("rotation-feed.json")
        config.init_market_db(self.db)
        self._seed_prices()

    def tearDown(self):
        for name, value in self.previous.items():
            setattr(config, name, value)
        self.directory.cleanup()

    def _seed_prices(self):
        boundary = datetime(2026, 8, 18, tzinfo=timezone.utc)
        conn = config.get_db_connection(db_path=self.db)
        try:
            # This unrelated watchlist row must not replace the approved pool.
            conn.execute(
                """INSERT INTO discovery_watchlist_history
                (event_id, observed_at, pool, symbol, asset, state)
                VALUES ('junk', ?, 'test', 'JUNKUSDT', 'JUNK', 'active')""",
                (boundary,),
            )
            for index, asset in enumerate(self.assets):
                first = 100.0
                last = 100.0 + index if index < 46 else 100.0 - (index - 45)
                for suffix, timestamp, close in (
                    ("start", boundary.replace(day=17), first),
                    ("end", boundary, last),
                ):
                    conn.execute(
                        """INSERT INTO source_observations
                        (observation_id, source, venue, native_symbol, asset,
                         market_kind, interval, source_start, source_end,
                         retrieved_at, retrieval_kind, payload_json)
                        VALUES (?, 'bybit_ws', 'bybit', ?, ?, 'usdt_perp',
                                '5m', ?, ?, ?, 'test', ?)""",
                        (
                            f"{asset}-{suffix}", f"{asset}USDT", asset,
                            timestamp, timestamp, timestamp,
                            json.dumps({"close": close}),
                        ),
                    )
            conn.commit()
        finally:
            conn.close()

    def _publish_test_feed(self):
        from symbol_rotation import build_feed, write_feed

        boundary = datetime(2026, 8, 18, tzinfo=timezone.utc)
        records = [
            {"asset": asset, "as_of": boundary, "source": "test", "interval": "24h", "retrieved_at": boundary,
             "reference_price": 100.0,
             "current_price": 100.0 + index}
            for index, asset in enumerate(self.assets)
        ]
        write_feed(build_feed(records, boundary, generated_at=boundary))

    def test_rotating_plugin_runs_30_when_enabled_and_92_when_disabled(self):
        from strategies.v2 import dual_zone_follower_v2
        self._publish_test_feed()

        def event(*args, **kwargs):
            asset = kwargs["asset"]
            return {"asset": asset, "strategy_id": "dual-zone-follower-v2"}

        snapshot = {"market_db_path": str(self.db), "now": datetime(2026, 8, 18, 1, 15, tzinfo=timezone.utc)}
        with patch.object(dual_zone_follower_v2, "load_bars_for_interval", return_value=None), \
             patch.object(dual_zone_follower_v2, "_dmi_adx", return_value=(25.0, 30.0, 10.0)), \
             patch.object(dual_zone_follower_v2, "evaluate_symbol", side_effect=event) as evaluate:
            selected_events = dual_zone_follower_v2.run_plugin("cutoff-rotated", snapshot)
            config.SYMBOL_ROTATION_ENABLED = False
            all_events = dual_zone_follower_v2.run_plugin("cutoff-full", snapshot)

        self.assertEqual(len(selected_events), 34)
        self.assertEqual(len(all_events), 92)
        self.assertEqual(evaluate.call_count, 126)
        self.assertNotIn("JUNK", {event["asset"] for event in selected_events + all_events})

    def test_feed_supervisor_reconciles_and_falls_back_to_feed_symbols(self):
        from symbol_rotation import build_feed, write_feed
        from strategy_v2_context import list_candidate_symbols
        from ws_gateway import SubscriptionSupervisor, subscription_state

        boundary = datetime(2026, 8, 18, tzinfo=timezone.utc)
        records = [
            {"asset": asset, "as_of": boundary, "source": "test", "interval": "24h", "retrieved_at": boundary,
             "reference_price": 100.0,
             "current_price": 100.0 + index}
            for index, asset in enumerate(self.assets)
        ]
        first_feed = build_feed(records, boundary, generated_at=boundary)
        write_feed(first_feed)
        bases, metadata = subscription_state(boundary)
        self.assertEqual(len(bases), 34)
        self.assertEqual(metadata["feed_id"], first_feed["feed_id"])
        self.assertEqual(len(list_candidate_symbols(None, boundary)), 34)
        self.assertEqual({"BTC", "ETH", "PAXG", "QQQ"} & set(bases), {"BTC", "ETH", "PAXG", "QQQ"})
        self.assertIn("QQQUSDT", config.expand_perp_symbols(bases, "bybit"))

        config.SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT = 40
        next_boundary = boundary + timedelta(hours=4)
        second_feed = build_feed(records, next_boundary, generated_at=next_boundary)
        write_feed(second_feed)
        supervisor = SubscriptionSupervisor(bases, metadata)
        result = supervisor.reconcile(*subscription_state(next_boundary + timedelta(minutes=1)))
        self.assertTrue(result["changed"])
        self.assertEqual(len(supervisor.bases), 44)
        self.assertEqual(len(supervisor.reconcile(*subscription_state(next_boundary + timedelta(minutes=1)))["added"]), 0)

        config.SYMBOL_ROTATION_ENABLED = False
        full, disabled_metadata = subscription_state(next_boundary + timedelta(minutes=1))
        self.assertEqual(len(full), 92)
        self.assertEqual(disabled_metadata["status"], "disabled")

        config.SYMBOL_ROTATION_ENABLED = True
        expired, fallback = subscription_state(next_boundary + timedelta(hours=5))
        self.assertEqual(expired, ["BTC", "ETH", "PAXG", "QQQ"])
        self.assertEqual(fallback["status"], "fallback")

    def test_compact_plugin_receives_the_upstream_scope_in_both_modes(self):
        from strategies.compact import bb_rsi_meanrev_v1
        self._publish_test_feed()

        def event(*args, **kwargs):
            return {"asset": kwargs["asset"], "strategy_id": "bb-rsi-meanrev-v1"}

        snapshot = {"market_db_path": str(self.db), "now": datetime(2026, 8, 18, 1, 15, tzinfo=timezone.utc)}
        with patch.object(bb_rsi_meanrev_v1, "load_bars_for_interval", return_value=None), \
             patch.object(bb_rsi_meanrev_v1, "evaluate_symbol", side_effect=event) as evaluate:
            enabled_events = bb_rsi_meanrev_v1.run_plugin("compact-enabled", snapshot)
            config.SYMBOL_ROTATION_ENABLED = False
            disabled_events = bb_rsi_meanrev_v1.run_plugin("compact-disabled", snapshot)

        self.assertEqual(len(enabled_events), 34)
        self.assertEqual(len(disabled_events), 92)
        self.assertEqual(evaluate.call_count, 126)


if __name__ == "__main__":
    unittest.main()
