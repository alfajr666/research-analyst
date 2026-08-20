import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config
from binance_oi_rotation_scanner import (
    build_feed,
    build_observation,
    completed_bar,
    completed_hour,
    publish_feed_atomic,
    qualify_and_rank,
    run_scanner,
)
from binance_oi_rotation_worker import run_due_scan


INTERVAL = datetime(2026, 8, 16, 11, tzinfo=timezone.utc)


def _market(symbol="TESTUSDT"):
    return {
        "symbol": symbol,
        "baseAsset": symbol.removesuffix("USDT"),
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "contractType": "PERPETUAL",
        "status": "TRADING",
    }


def _history(interval=INTERVAL, final_oi=130_000_000, final_volume=10_000_000, base_volume=1_000_000):
    start = interval - timedelta(hours=8)
    oi, candles = [], []
    for index in range(9):
        timestamp = start + timedelta(hours=index)
        oi_value = 100_000_000 + index * 1_000_000
        if timestamp == interval:
            oi_value = final_oi
        oi.append({"timestamp": int(timestamp.timestamp() * 1000), "sumOpenInterestValue": str(oi_value)})
        quote_volume = base_volume + index * 10_000
        if timestamp == interval:
            quote_volume = final_volume
        close = 100 + index
        candles.append([int(timestamp.timestamp() * 1000), "0", "0", "0", str(close), "0", "0", str(quote_volume)])
    return oi, candles


class BinanceOIRotationDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "BINANCE_OI_ROTATION_MIN_24H_VOLUME_USD": config.BINANCE_OI_ROTATION_MIN_24H_VOLUME_USD,
            "BINANCE_OI_ROTATION_MIN_OI_DELTA_USD": config.BINANCE_OI_ROTATION_MIN_OI_DELTA_USD,
            "BINANCE_OI_ROTATION_MIN_OI_PERCENTILE": config.BINANCE_OI_ROTATION_MIN_OI_PERCENTILE,
            "BINANCE_OI_ROTATION_MIN_VOLUME_ANOMALY": config.BINANCE_OI_ROTATION_MIN_VOLUME_ANOMALY,
            "BINANCE_OI_ROTATION_HISTORY_HOURS": config.BINANCE_OI_ROTATION_HISTORY_HOURS,
        }
        config.BINANCE_OI_ROTATION_MIN_24H_VOLUME_USD = 5_000_000
        config.BINANCE_OI_ROTATION_MIN_OI_DELTA_USD = 5_000_000
        config.BINANCE_OI_ROTATION_MIN_OI_PERCENTILE = 0.9
        config.BINANCE_OI_ROTATION_MIN_VOLUME_ANOMALY = 1.0
        config.BINANCE_OI_ROTATION_HISTORY_HOURS = 8

    def tearDown(self):
        for name, value in self.settings.items():
            setattr(config, name, value)

    def test_completed_hour_never_uses_the_in_progress_bar(self):
        self.assertEqual(completed_hour(datetime(2026, 8, 16, 12, 59, tzinfo=timezone.utc)), INTERVAL)

    def test_disabled_rotation_scanner_does_not_open_the_database(self):
        with patch.object(config, "BINANCE_OI_ROTATION_ENABLED", False), \
             patch.object(config, "BINANCE_OI_10M_ENABLED", False), \
             patch("binance_oi_rotation_worker.config.get_db_connection") as get_connection:
            self.assertFalse(run_due_scan(INTERVAL + timedelta(hours=1)))
        get_connection.assert_not_called()

    def test_liquidity_data_quality_and_feature_gates(self):
        oi, candles = _history()
        qualified = build_observation(_market(), {"quoteVolume": "10000000"}, oi, candles, INTERVAL)
        thin_oi, thin_candles = _history(final_volume=1, base_volume=1)
        thin = build_observation(_market("THINUSDT"), {"quoteVolume": "1"}, thin_oi, thin_candles, INTERVAL)
        incomplete = build_observation(_market("MISSUSDT"), {"quoteVolume": "10000000"}, oi[:-1], candles, INTERVAL)

        self.assertTrue(qualified["is_eligible"])
        self.assertGreater(qualified["oi_change_1h_usd"], 5_000_000)
        self.assertGreaterEqual(qualified["oi_spike_percentile"], 0.9)
        self.assertGreater(qualified["volume_anomaly"], 1)
        self.assertEqual(thin["rejection_reason"], "below_liquidity_floor")
        self.assertEqual(incomplete["rejection_reason"], "incomplete_completed_bar")

    def test_non_boundary_oi_observations_align_to_the_completed_hour(self):
        oi, candles = _history()
        for item in oi:
            item["timestamp"] += 15 * 60 * 1000
        record = build_observation(_market(), {"quoteVolume": "10000000"}, oi, candles, INTERVAL)
        self.assertTrue(record["is_eligible"])

    def test_ranking_is_deterministic_and_feed_explicitly_expires(self):
        oi, candles = _history()
        alpha = build_observation(_market("ALPHAUSDT"), {"quoteVolume": "10000000"}, oi, candles, INTERVAL)
        beta = {**alpha, "symbol": "BETAUSDT", "asset": "BETA", "oi_change_1h_usd": alpha["oi_change_1h_usd"] - 1}
        ranked = qualify_and_rank([beta, alpha])
        feed = build_feed(INTERVAL, ranked, generated_at=INTERVAL + timedelta(hours=1))

        self.assertEqual([item["symbol"] for item in ranked], ["ALPHAUSDT", "BETAUSDT"])
        self.assertEqual(feed["schema_version"], 1)
        self.assertEqual(feed["expires_at"], "2026-08-16T17:00:00+00:00")
        self.assertNotIn("direction", feed["candidates"][0])


class BinanceOIRotationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        self.old_oi_db_path = config.BINANCE_OI_DB_PATH
        self.old_feed_path = config.BINANCE_OI_ROTATION_FEED_PATH
        self.old_max_contracts = config.BINANCE_OI_ROTATION_MAX_CONTRACTS
        self.old_history_hours = config.BINANCE_OI_ROTATION_HISTORY_HOURS
        config.DB_PATH = os.path.join(self.temp_dir.name, "research.db")
        config.BINANCE_OI_DB_PATH = os.path.join(self.temp_dir.name, "binance_oi.db")
        config.BINANCE_OI_ROTATION_FEED_PATH = os.path.join(self.temp_dir.name, "rotation-feed.json")
        config.BINANCE_OI_ROTATION_MAX_CONTRACTS = 5
        config.BINANCE_OI_ROTATION_HISTORY_HOURS = 8
        config.init_db()
        config.init_binance_oi_db()

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        config.BINANCE_OI_DB_PATH = self.old_oi_db_path
        config.BINANCE_OI_ROTATION_FEED_PATH = self.old_feed_path
        config.BINANCE_OI_ROTATION_MAX_CONTRACTS = self.old_max_contracts
        config.BINANCE_OI_ROTATION_HISTORY_HOURS = self.old_history_hours
        self.temp_dir.cleanup()

    def test_scan_persists_rejections_deduplicates_event_and_publishes_atomic_feed(self):
        oi, candles = _history()

        class Client:
            def eligible_markets(self):
                return [_market(), _market("THINUSDT")], {
                    "TESTUSDT": {"quoteVolume": "10000000"},
                    "THINUSDT": {"quoteVolume": "1"},
                }

            def history(self, symbol, interval, bar_minutes=60):
                if symbol == "THINUSDT":
                    return _history(final_volume=1, base_volume=1)
                return oi, candles

        first = run_scanner(now=INTERVAL + timedelta(hours=1, minutes=2), client=Client())
        second = run_scanner(now=INTERVAL + timedelta(hours=1, minutes=3), client=Client())
        with open(config.BINANCE_OI_ROTATION_FEED_PATH, encoding="utf-8") as handle:
            on_disk = json.load(handle)
        oi_conn = config.get_db_connection(read_only=True, db_path=config.BINANCE_OI_DB_PATH)
        observations = oi_conn.execute("SELECT COUNT(*) FROM binance_oi_rotation_observations").fetchone()[0]
        raw_oi = oi_conn.execute("SELECT COUNT(*) FROM binance_oi_rotation_raw_oi_history").fetchone()[0]
        events = oi_conn.execute("SELECT COUNT(*) FROM binance_oi_rotation_events").fetchone()[0]
        watchlist = oi_conn.execute("SELECT state, deep_backfill_required FROM binance_oi_rotation_watchlist_history").fetchall()
        oi_conn.close()
        main_conn = config.get_db_connection(read_only=True, db_path=config.DB_PATH)
        warmup_job = main_conn.execute("SELECT status FROM deep_backfill_jobs WHERE symbol = 'TESTUSDT_PERP.A'").fetchone()
        main_conn.close()

        self.assertEqual(observations, 2)
        self.assertEqual(raw_oi, len(oi) * 2)
        self.assertEqual(events, 1)
        self.assertEqual(watchlist, [("entered", True)])
        self.assertEqual(warmup_job, ("pending",))
        self.assertEqual(first["candidates"], second["candidates"])
        self.assertEqual(on_disk["completed_interval_at"], INTERVAL.isoformat())
        self.assertFalse(os.path.exists(config.BINANCE_OI_ROTATION_FEED_PATH + ".tmp"))

    def test_incomplete_scan_is_retried_until_marked_complete(self):
        oi, candles = _history()

        class FailingThenWorkingClient:
            def __init__(self):
                self.calls = 0

            def eligible_markets(self):
                return [_market()], {"TESTUSDT": {"quoteVolume": "10000000"}}

            def history(self, symbol, interval, bar_minutes=60):
                self.calls += 1
                if self.calls == 1:
                    raise __import__("httpx").HTTPError("temporary")
                return oi, candles

        client = FailingThenWorkingClient()
        run_scanner(now=INTERVAL + timedelta(hours=1, minutes=2), client=client)
        run_scanner(now=INTERVAL + timedelta(hours=1, minutes=3), client=client)
        conn = config.get_db_connection(read_only=True, db_path=config.BINANCE_OI_DB_PATH)
        status = conn.execute("SELECT status FROM binance_oi_rotation_scans").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM binance_oi_rotation_events").fetchone()[0]
        conn.close()
        self.assertEqual(status, "complete")
        self.assertEqual(events, 1)

    def test_atomic_writer_replaces_valid_document(self):
        path = config.BINANCE_OI_ROTATION_FEED_PATH
        publish_feed_atomic({"complete": True}, path)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"complete": True})

    def test_worker_runs_only_missing_completed_hour(self):
        from binance_oi_rotation_worker import run_due_scan

        now = INTERVAL + timedelta(hours=1, minutes=4)
        with patch.object(config, "BINANCE_OI_10M_ENABLED", False), \
             patch("binance_oi_rotation_worker.run_scanner", return_value={"candidates": []}) as scanner:
            self.assertTrue(run_due_scan(now))
            scanner.assert_called_once_with(now=now, bar_minutes=60)

            conn = config.get_db_connection(db_path=config.BINANCE_OI_DB_PATH)
            conn.execute(
                "INSERT INTO binance_oi_rotation_scans VALUES (?, ?, ?, ?, ?, ?)",
                ("binance_usdm", INTERVAL, config.BINANCE_OI_ROTATION_SCANNER_VERSION, "complete", now, 60),
            )
            conn.commit()
            conn.close()
            self.assertFalse(run_due_scan(now))


class BinanceOI10mFastPathTests(unittest.TestCase):
    """Spec-driven coverage for 15m (no native 10m) liquid fast path."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_oi = config.BINANCE_OI_DB_PATH
        self.old_feed = config.BINANCE_OI_ROTATION_FEED_PATH
        self.old_10m_enabled = getattr(config, "BINANCE_OI_10M_ENABLED", True)
        self.old_bar = getattr(config, "BINANCE_OI_10M_BAR_MINUTES", 15)
        self.old_min_delta = getattr(config, "BINANCE_OI_10M_MIN_OI_DELTA_USD", 250000)
        config.BINANCE_OI_DB_PATH = os.path.join(self.temp_dir.name, "oi.db")
        config.BINANCE_OI_ROTATION_FEED_PATH = os.path.join(self.temp_dir.name, "feed.json")
        config.BINANCE_OI_10M_ENABLED = True
        config.BINANCE_OI_10M_BAR_MINUTES = 15
        config.BINANCE_OI_10M_MIN_OI_DELTA_USD = 100_000
        config.BINANCE_OI_10M_MIN_OI_PERCENTILE = 0.8
        config.BINANCE_OI_10M_MIN_VOLUME_ANOMALY = 0.5
        config.BINANCE_OI_10M_MAX_CONTRACTS = 10
        config.init_binance_oi_db()

    def tearDown(self):
        config.BINANCE_OI_DB_PATH = self.old_oi
        config.BINANCE_OI_ROTATION_FEED_PATH = self.old_feed
        config.BINANCE_OI_10M_ENABLED = self.old_10m_enabled
        config.BINANCE_OI_10M_BAR_MINUTES = self.old_bar
        config.BINANCE_OI_10M_MIN_OI_DELTA_USD = self.old_min_delta
        self.temp_dir.cleanup()

    def test_completed_bar_15m(self):
        from binance_oi_rotation_scanner import completed_bar
        t = datetime(2026, 8, 18, 12, 7, tzinfo=timezone.utc)
        self.assertEqual(completed_bar(t, 15), datetime(2026, 8, 18, 11, 45, tzinfo=timezone.utc))

    def test_short_bar_publishes_with_bar_metadata_and_does_not_clobber_on_empty(self):
        # Seed a live feed via direct publish (simulates prior 1h or strong short)
        strong_feed = {
            "schema_version": 1,
            "source": "binance_usdm",
            "scanner_version": "v1",
            "generated_at": (INTERVAL + timedelta(minutes=5)).isoformat(),
            "completed_interval_at": INTERVAL.isoformat(),
            "expires_at": (INTERVAL + timedelta(hours=5)).isoformat(),
            "bar_minutes": 60,
            "discovery_cadence": "1h_full",
            "candidates": [{"asset": "SEED", "rank": 1, "oi_change_1h_usd": 900000}],
        }
        from binance_oi_rotation_scanner import publish_feed_atomic
        publish_feed_atomic(strong_feed)
        with open(config.BINANCE_OI_ROTATION_FEED_PATH) as f:
            before = json.load(f)
        gen_before = before["generated_at"]

        # quiet 15m run: should not publish (no clobber)
        class Quiet:
            def eligible_markets(self):
                return [_market("THINUSDT")], {"THINUSDT": {"quoteVolume": "100"}}

            def history(self, s, i, bar_minutes=15):
                return _history(final_oi=100_100_000, final_volume=10)

        run_scanner(now=INTERVAL + timedelta(minutes=17), client=Quiet(), bar_minutes=15)
        with open(config.BINANCE_OI_ROTATION_FEED_PATH) as f:
            after = json.load(f)
        self.assertEqual(after["generated_at"], gen_before, "empty short must not clobber unexpired feed")
        self.assertGreater(len(after.get("candidates", [])), 0)  # the seeded one remains

    def test_independent_scan_records_for_15m_and_60m_on_same_wall_time(self):
        oi, candles = _history()
        class C:
            def eligible_markets(self): return [_market()], {"TESTUSDT": {"quoteVolume": "6000000"}}
            def history(self, s, i, bar_minutes=60): return oi, candles

        # run 1h
        run_scanner(now=INTERVAL + timedelta(minutes=2), client=C(), bar_minutes=60)
        # run 15m for a bar whose ts may differ; use a non-hour boundary 15m ts
        short_i = completed_bar(INTERVAL + timedelta(minutes=12), 15)
        run_scanner(now=short_i + timedelta(minutes=1), client=C(), bar_minutes=15)

        conn = config.get_db_connection(read_only=True, db_path=config.BINANCE_OI_DB_PATH)
        rows = conn.execute("SELECT bar_minutes, status FROM binance_oi_rotation_scans ORDER BY bar_minutes").fetchall()
        conn.close()
        bars = {r[0] for r in rows}
        self.assertIn(60, bars)
        self.assertIn(15, bars)




class StaticMembershipSkipTests(unittest.TestCase):
    """ADR-013 P1: static seed skips entered/active membership; events still OK."""

    def setUp(self):
        import binance_oi_rotation_scanner as sc
        sc._STATIC_SEED_CACHE = None
        self._prev_skip = getattr(config, "BINANCE_OI_STATIC_MEMBERSHIP_SKIP", True)
        self._prev_path = getattr(config, "BINANCE_OI_STATIC_SEED_PATH", "")
        self.td = tempfile.TemporaryDirectory()
        seed = Path(self.td.name) / "static.json"
        seed.write_text(json.dumps(["BTC", "ETH"]), encoding="utf-8")
        config.BINANCE_OI_STATIC_MEMBERSHIP_SKIP = True
        config.BINANCE_OI_STATIC_SEED_PATH = str(seed)
        sc._STATIC_SEED_CACHE = None
        self.db = str(Path(self.td.name) / "oi.db")
        self._prev_db = config.BINANCE_OI_DB_PATH
        config.BINANCE_OI_DB_PATH = self.db
        config.init_binance_oi_db(self.db)

    def tearDown(self):
        import binance_oi_rotation_scanner as sc
        config.BINANCE_OI_STATIC_MEMBERSHIP_SKIP = self._prev_skip
        config.BINANCE_OI_STATIC_SEED_PATH = self._prev_path
        config.BINANCE_OI_DB_PATH = self._prev_db
        sc._STATIC_SEED_CACHE = None
        self.td.cleanup()

    def test_static_asset_no_active_membership(self):
        from binance_oi_rotation_scanner import _update_watchlist_oi_only, SOURCE
        conn = config.get_db_connection(read_only=False, db_path=self.db)
        interval = INTERVAL
        cands = [
            {"asset": "BTC", "symbol": "BTCUSDT"},
            {"asset": "ALPINE", "symbol": "ALPINEUSDT"},
        ]
        _update_watchlist_oi_only(conn, interval, cands)
        try:
            conn.commit()
        except Exception:
            pass
        rows = conn.execute(
            """
            SELECT asset, state FROM binance_oi_rotation_watchlist_history
            WHERE source = ? AND state IN ('entered','active')
            ORDER BY asset
            """,
            (SOURCE,),
        ).fetchall()
        conn.close()
        assets = [r[0] for r in rows]
        self.assertNotIn("BTC", assets)
        self.assertIn("ALPINE", assets)


if __name__ == "__main__":
    unittest.main()
