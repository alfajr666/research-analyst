import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from strategy_v2_context import (
    load_bars_for_interval,
    shared_computation_context,
    strategy_market_connection,
)


class SharedComputationContextTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "market.sqlite3"
        self.previous_market_db = config.MARKET_DB_PATH
        config.MARKET_DB_PATH = str(self.db)
        config.init_market_db(self.db)
        self.cutoff = datetime(2026, 8, 17, 12, 15, tzinfo=timezone.utc)
        conn = config.get_db_connection(db_path=self.db)
        try:
            for index in range(4):
                end = self.cutoff - timedelta(minutes=5 * (3 - index))
                conn.execute(
                    """INSERT INTO source_observations
                       (observation_id, source, venue, native_symbol, asset, market_kind,
                        interval, source_start, source_end, retrieved_at, retrieval_kind,
                        payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f"obs-{index}", "bybit_ws", "bybit", "BTCUSDT", "BTC", "perp",
                        "5m", end - timedelta(minutes=5), end, end, "stream",
                        '{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}',
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        config.MARKET_DB_PATH = self.previous_market_db
        self.directory.cleanup()

    def test_frames_and_features_are_shared_within_cutoff(self):
        spec = {"ema": {"ema_2": 2}, "atr": {"atr_2": 2}}
        with shared_computation_context(self.db, self.cutoff) as context:
            first = load_bars_for_interval(None, "BTC", "5m", self.cutoff)
            second = load_bars_for_interval(None, "BTCUSDT", "5m", self.cutoff)
            feature_one = context.features("BTC", "5m", spec)
            feature_two = context.features("BTCUSDT", "5m", spec)

            self.assertIs(first, second)
            self.assertIs(feature_one, feature_two)
            self.assertEqual(context.stats["frame_misses"], 1)
            self.assertEqual(context.stats["frame_hits"], 2)
            self.assertEqual(context.stats["feature_misses"], 1)
            self.assertEqual(context.stats["feature_hits"], 1)

    def test_context_rejects_unrelated_cutoff(self):
        with shared_computation_context(self.db, self.cutoff):
            with self.assertRaises(ValueError):
                load_bars_for_interval(
                    None, "BTC", "5m", self.cutoff + timedelta(minutes=5)
                )

    def test_sequential_cutoffs_extend_the_cached_frame(self):
        later_cutoff = self.cutoff + timedelta(minutes=5)
        with shared_computation_context(self.db, self.cutoff) as first_context:
            first = load_bars_for_interval(None, "BTC", "5m", self.cutoff)
            self.assertEqual(first.height, 4)
            self.assertEqual(first_context.stats["sequential_misses"], 1)

        with shared_computation_context(self.db, later_cutoff) as second_context:
            second = load_bars_for_interval(None, "BTC", "5m", later_cutoff)
            self.assertEqual(second.height, 4)
            self.assertEqual(second_context.stats["sequential_hits"], 1)

    def test_sequential_cutoff_with_empty_repair_window_falls_back_to_reload(self):
        conn = config.get_db_connection(db_path=self.db)
        try:
            conn.execute(
                "DELETE FROM source_observations WHERE source_end >= ?",
                (self.cutoff - timedelta(minutes=10),),
            )
            conn.commit()
        finally:
            conn.close()

        later_cutoff = self.cutoff + timedelta(minutes=5)
        with shared_computation_context(self.db, self.cutoff):
            first = load_bars_for_interval(None, "BTC", "5m", self.cutoff)
            self.assertEqual(first.height, 1)

        with shared_computation_context(self.db, later_cutoff) as context:
            second = load_bars_for_interval(None, "BTC", "5m", later_cutoff)
            self.assertEqual(second.height, 1)
            self.assertEqual(context.stats["sequential_hits"], 0)
            self.assertEqual(context.stats["sequential_misses"], 1)

    def test_strategies_reuse_the_context_connection(self):
        with shared_computation_context(self.db, self.cutoff) as context:
            connection, owns_connection = strategy_market_connection(self.db)
            self.assertIs(connection, context.market_conn)
            self.assertFalse(owns_connection)

    def test_source_identity_change_invalidates_sequential_reuse(self):
        later_cutoff = self.cutoff + timedelta(minutes=5)
        with shared_computation_context(self.db, self.cutoff):
            load_bars_for_interval(None, "BTC", "5m", self.cutoff)

        conn = config.get_db_connection(db_path=self.db)
        try:
            conn.execute(
                "UPDATE source_observations SET payload_json = ? WHERE observation_id = ?",
                ('{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 99}', "obs-0"),
            )
            conn.commit()
        finally:
            conn.close()

        with shared_computation_context(self.db, later_cutoff) as context:
            load_bars_for_interval(None, "BTC", "5m", later_cutoff)
            self.assertEqual(context.stats["sequential_hits"], 0)
            self.assertEqual(context.stats["cache_invalidation_reasons"]["source_identity_changed"], 1)


if __name__ == "__main__":
    unittest.main()
