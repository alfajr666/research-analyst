import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import config
from symbol_rotation import (
    build_feed,
    fetch_bybit_ticker_snapshot,
    PERMANENT_SYMBOLS,
    read_feed,
    refresh_feed,
    select_symbols,
    validate_feed,
    write_feed,
)


class SymbolRotationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "market.db"
        config.init_market_db(self.db)
        self.previous = {
            name: getattr(config, name)
            for name in (
                "SYMBOL_ROTATION_ENABLED",
                "SYMBOL_ROTATION_LOOKBACK_HOURS",
                "SYMBOL_ROTATION_BAR_INTERVAL",
                "SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT",
            )
        }
        config.SYMBOL_ROTATION_ENABLED = True
        config.SYMBOL_ROTATION_LOOKBACK_HOURS = 24
        config.SYMBOL_ROTATION_BAR_INTERVAL = "5m"
        config.SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT = 30

    def tearDown(self):
        for name, value in self.previous.items():
            setattr(config, name, value)
        self.directory.cleanup()

    def test_selects_top_gainers_and_losers_at_four_hour_boundary(self):
        boundary = datetime(2026, 8, 18, tzinfo=timezone.utc)
        conn = config.get_db_connection(db_path=self.db)
        try:
            candidates = []
            for index in range(40):
                asset = f"COIN{index:02d}"
                candidates.append((f"{asset}USDT", asset))
                first = 100.0
                last = 100.0 + index if index < 20 else 100.0 - (index - 19)
                for suffix, timestamp, close in (
                    ("open", boundary.replace(day=17), first),
                    ("close", boundary, last),
                ):
                    conn.execute(
                        """INSERT INTO source_observations
                        (observation_id, source, venue, native_symbol, asset,
                         market_kind, interval, source_start, source_end,
                         retrieved_at, retrieval_kind, payload_json)
                        VALUES (?, 'test', 'test', ?, ?, 'perp', '5m', ?, ?, ?, 'test', ?)""",
                        (
                            f"{asset}-{suffix}", f"{asset}USDT", asset,
                            timestamp, timestamp, timestamp,
                            json.dumps({"close": close}),
                        ),
                    )
            conn.commit()
            selected = select_symbols(
                conn, candidates, datetime(2026, 8, 18, 1, 15, tzinfo=timezone.utc)
            )
        finally:
            conn.close()

        selected_assets = {asset for _, asset in selected}
        self.assertEqual(len(selected), 30)
        self.assertEqual(selected_assets, {
            *(f"COIN{index:02d}" for index in range(5, 20)),
            *(f"COIN{index:02d}" for index in range(25, 40)),
        })

    def test_missing_performance_data_keeps_candidates(self):
        candidates = [("BTCUSDT", "BTC"), ("ETHUSDT", "ETH")]
        conn = config.get_db_connection(db_path=self.db)
        try:
            selected = select_symbols(conn, candidates, datetime(2026, 8, 18, tzinfo=timezone.utc))
        finally:
            conn.close()
        self.assertEqual(selected, candidates)

    def test_future_rows_are_ignored_and_ws_wins_duplicate_timestamp(self):
        boundary = datetime(2026, 8, 18, tzinfo=timezone.utc)
        candidates = [("SOLUSDT", "SOL"), ("ADAUSDT", "ADA"), ("DOTUSDT", "DOT")]
        config.SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT = 2
        conn = config.get_db_connection(db_path=self.db)
        try:
            rows = [
                ("sol-start", "SOLUSDT", "SOL", boundary.replace(day=17), "test", 100.0),
                ("sol-start-ws", "SOLUSDT", "SOL", boundary.replace(day=17), "bybit_ws", 100.0),
                ("sol-end", "SOLUSDT", "SOL", boundary, "test", 50.0),
                ("sol-end-ws", "SOLUSDT", "SOL", boundary, "bybit_ws", 110.0),
                ("sol-future", "SOLUSDT", "SOL", boundary.replace(hour=1), "bybit_ws", 1000.0),
                ("ada-start", "ADAUSDT", "ADA", boundary.replace(day=17), "test", 100.0),
                ("ada-end", "ADAUSDT", "ADA", boundary, "test", 90.0),
                ("dot-start", "DOTUSDT", "DOT", boundary.replace(day=17), "test", 100.0),
                ("dot-end", "DOTUSDT", "DOT", boundary, "test", 100.0),
            ]
            for observation_id, symbol, asset, timestamp, source, close in rows:
                conn.execute(
                    """INSERT INTO source_observations
                    (observation_id, source, venue, native_symbol, asset,
                     market_kind, interval, source_start, source_end,
                     retrieved_at, retrieval_kind, payload_json)
                    VALUES (?, ?, 'test', ?, ?, 'perp', '5m', ?, ?, ?, 'test', ?)""",
                    (observation_id, source, symbol, asset, timestamp, timestamp,
                     timestamp, json.dumps({"close": close})),
                )
            conn.commit()
            selected = select_symbols(conn, candidates, boundary.replace(hour=1))
        finally:
            conn.close()
        self.assertEqual({asset for _, asset in selected}, {"SOL", "ADA"})

    def test_rotation_can_be_disabled(self):
        candidates = [(f"COIN{index}USDT", f"COIN{index}") for index in range(92)]
        config.SYMBOL_ROTATION_ENABLED = False
        conn = config.get_db_connection(db_path=self.db)
        try:
            selected = select_symbols(conn, candidates, datetime(2026, 8, 18, tzinfo=timezone.utc))
        finally:
            conn.close()
        self.assertEqual(selected, candidates)

    def test_feed_splits_rotating_slots_and_adds_permanent_symbols(self):
        boundary = datetime(2026, 8, 18, tzinfo=timezone.utc)
        assets = [f"COIN{index:02d}" for index in range(92)]
        records = [{"asset": asset, "as_of": boundary, "source": "test", "interval": "24h", "retrieved_at": boundary,
                    "reference_price": 100.0, "current_price": 100.0 + index}
                   for index, asset in enumerate(assets)]
        with patch.object(config, "load_static_symbols", return_value=assets):
            for total in (30, 40, 60):
                config.SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT = total
                feed = build_feed(records, boundary, generated_at=boundary)
                self.assertEqual(feed["symbol_count"], total + 4)
                self.assertEqual(feed["rotating_symbol_count"], total)
                self.assertEqual(len(feed["gainers"]), total // 2)
                self.assertEqual(len(feed["losers"]), total // 2)
                self.assertEqual(len(set(feed["symbols"])), total + 4)
                self.assertEqual(feed["permanent_symbols"], ["BTC", "ETH", "PAXG", "QQQUSDT"])
                self.assertEqual(feed["symbols"][:4], feed["permanent_symbols"])

            tied = [{"asset": asset, "as_of": boundary, "source": "test", "interval": "24h", "retrieved_at": boundary,
                     "reference_price": 100.0, "current_price": 110.0}
                    for asset in assets]
            config.SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT = 30
            feed = build_feed(tied, boundary, generated_at=boundary)
        self.assertEqual([item["asset"] for item in feed["gainers"]], assets[:15])
        self.assertEqual([item["asset"] for item in feed["losers"]], assets[:15])

    def test_invalid_snapshot_retains_then_falls_back_after_expiry(self):
        boundary = datetime(2026, 8, 18, tzinfo=timezone.utc)
        assets = [f"COIN{index:02d}" for index in range(92)]
        records = [{"asset": asset, "as_of": boundary, "source": "test", "interval": "24h", "retrieved_at": boundary,
                    "reference_price": 100.0, "current_price": 100.0 + index}
                   for index, asset in enumerate(assets)]
        with patch.object(config, "load_static_symbols", return_value=assets):
            previous = build_feed(records, boundary, generated_at=boundary)
            retained = build_feed([], boundary.replace(hour=2), previous_feed=previous)
            fallback = build_feed([], boundary.replace(hour=8), previous_feed=previous)
        self.assertEqual(retained["feed_id"], previous["feed_id"])
        self.assertEqual(fallback["status"], "fallback")
        self.assertEqual(fallback["symbol_count"], 4)
        self.assertTrue(validate_feed(fallback))

    def test_legacy_fallback_feed_is_migrated_to_permanent_symbols(self):
        boundary = datetime(2026, 8, 18, tzinfo=timezone.utc)
        path = Path(self.directory.name) / "feed.json"
        legacy = build_feed([], boundary, generated_at=boundary)
        legacy["symbols"] = [f"COIN{index:02d}" for index in range(92)] + list(PERMANENT_SYMBOLS)
        legacy["symbol_count"] = len(legacy["symbols"])
        write_feed(legacy, path)
        conn = config.get_db_connection(db_path=self.db)
        try:
            from symbol_rotation import refresh_feed
            current = refresh_feed(conn, boundary, path=path, now=boundary)
        finally:
            conn.close()
        self.assertEqual(current["symbols"], list(PERMANENT_SYMBOLS))

    def test_refreshes_rotation_at_each_four_hour_boundary(self):
        boundary = datetime(2026, 8, 18, tzinfo=timezone.utc)
        next_boundary = boundary.replace(hour=4)
        records = [
            {"asset": f"COIN{index:02d}", "as_of": boundary, "source": "bybit_ticker",
             "interval": "24h", "retrieved_at": boundary,
             "reference_price": 100.0, "current_price": 100.0 + index}
            for index in range(40)
        ]
        path = Path(self.directory.name) / "feed.json"
        initial = build_feed(records, boundary, generated_at=boundary)
        write_feed(initial, path)
        conn = config.get_db_connection(db_path=self.db)
        try:
            refreshed = refresh_feed(
                conn, next_boundary, records=records, now=next_boundary, path=path
            )
        finally:
            conn.close()
        self.assertNotEqual(refreshed["feed_id"], initial["feed_id"])
        self.assertEqual(refreshed["valid_from"], "2026-08-18T04:00:00Z")
        self.assertEqual(refreshed["valid_until"], "2026-08-18T08:00:00Z")

    def test_bootstraps_active_window_from_fresh_snapshot(self):
        boundary = datetime(2026, 8, 18, tzinfo=timezone.utc)
        observed_at = boundary.replace(hour=2)
        records = [
            {"asset": f"COIN{index:02d}", "as_of": observed_at, "source": "bybit_ticker",
             "interval": "24h", "retrieved_at": observed_at,
             "reference_price": 100.0, "current_price": 100.0 + index}
            for index in range(40)
        ]
        feed = build_feed(
            records, boundary, generated_at=observed_at, source_cutoff=observed_at
        )
        self.assertEqual(feed["status"], "ready")
        self.assertEqual(feed["valid_from"], "2026-08-18T00:00:00Z")
        self.assertEqual(feed["valid_until"], "2026-08-18T04:00:00Z")
        self.assertEqual(feed["source_as_of"], "2026-08-18T02:00:00+00:00")
        self.assertEqual(feed["symbol_count"], 34)

    def test_bybit_ticker_snapshot_uses_all_usdt_linear_tickers(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "time": 1787097600000,
                    "result": {"list": [
                        {"symbol": "SOLUSDT", "lastPrice": "110", "prevPrice24h": "100"},
                        {"symbol": "DOGEUSDC", "lastPrice": "1", "prevPrice24h": "1"},
                    ]},
                }

        class Client:
            def get(self, *args, **kwargs):
                return Response()

        records = fetch_bybit_ticker_snapshot(
            datetime(2026, 8, 18, tzinfo=timezone.utc), client=Client()
        )
        self.assertEqual([record["asset"] for record in records], ["SOL"])
        self.assertEqual(records[0]["source"], "bybit_ticker")
        self.assertLessEqual(records[0]["as_of"], records[0]["retrieved_at"])

    def test_future_and_stale_data_are_not_ranked_and_feed_is_atomic(self):
        boundary = datetime(2026, 8, 18, tzinfo=timezone.utc)
        assets = [f"COIN{index:02d}" for index in range(92)]
        with patch.object(config, "load_static_symbols", return_value=assets):
            invalid = build_feed([{
                "asset": assets[0], "as_of": boundary.replace(hour=1), "source": "test",
                "interval": "24h", "retrieved_at": boundary.replace(hour=1),
                "reference_price": 100.0, "current_price": 200.0,
            }], boundary, generated_at=boundary)
            self.assertEqual(invalid["status"], "fallback")
            path = Path(self.directory.name) / "feed.json"
            write_feed(invalid, path)
        self.assertEqual(read_feed(path, boundary)["feed_id"], invalid["feed_id"])


if __name__ == "__main__":
    unittest.main()
