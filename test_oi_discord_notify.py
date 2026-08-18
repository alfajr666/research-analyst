import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config
from oi_discord_notify import notify_oi_feed


class FakeTransport:
    def __init__(self, failures=0):
        self.failures = failures
        self.messages = []

    def send(self, text):
        self.messages.append(text)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("webhook down")
        return "ok"


class OiDiscordNotifyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = str(Path(self.directory.name) / "market.db")
        self.previous_db = config.DB_PATH
        config.DB_PATH = self.db
        config.init_db(self.db)
        self.interval = datetime(2026, 8, 18, 5, 0, tzinfo=timezone.utc)
        self.now = self.interval + timedelta(minutes=3)

    def tearDown(self):
        config.DB_PATH = self.previous_db
        self.directory.cleanup()

    def _seed_event(self, interval, asset="LAB", rank=1):
        conn = config.get_db_connection(db_path=self.db)
        try:
            metrics = {
                "asset": asset,
                "symbol": f"{asset}USDT",
                "rank": rank,
                "oi_change_1h_pct": 0.05,
                "oi_change_1h_usd": 1_000_000,
                "open_interest_usd": 10_000_000,
                "price_change_1h": -0.01,
                "volume_anomaly": 1.5,
            }
            conn.execute(
                "INSERT INTO binance_oi_rotation_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "binance_usdm",
                    asset,
                    interval,
                    config.BINANCE_OI_ROTATION_SCANNER_VERSION,
                    f"{asset}USDT",
                    rank,
                    json.dumps(metrics),
                    self.now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_posts_hour_and_multi_on_boundary_once(self):
        self._seed_event(self.interval)
        self._seed_event(self.interval - timedelta(hours=2))
        feed = {
            "completed_interval_at": self.interval.isoformat(),
            "expires_at": (self.interval + timedelta(hours=6)).isoformat(),
            "candidates": [
                {
                    "rank": 1,
                    "asset": "LAB",
                    "symbol": "LABUSDT",
                    "oi_change_1h_pct": 0.068,
                    "oi_change_1h_usd": 1_000_000,
                    "open_interest_usd": 16_000_000,
                    "price_change_1h": -0.02,
                    "volume_anomaly": 1.8,
                }
            ],
        }
        transport = FakeTransport()
        with patch.object(config, "BINANCE_OI_DISCORD_MULTI_HOUR_WINDOW", 6), \
             patch.object(config, "BINANCE_OI_DISCORD_TOP_N", 5), \
             patch.object(config, "BINANCE_OI_DISCORD_SKIP_EMPTY", True):
            first = notify_oi_feed(feed, transport=transport, db_path=self.db, now=lambda: self.now)
            second = notify_oi_feed(feed, transport=transport, db_path=self.db, now=lambda: self.now)
        self.assertEqual(first, {"hour": "sent", "multi": "sent"})
        self.assertEqual(second, {"hour": "already_sent", "multi": "already_sent"})
        self.assertEqual(len(transport.messages), 2)
        self.assertIn("1h", transport.messages[0])
        self.assertIn("multi-hour", transport.messages[1])

    def test_skips_empty_hour_and_non_boundary_multi(self):
        hour = datetime(2026, 8, 18, 1, 0, tzinfo=timezone.utc)
        feed = {
            "completed_interval_at": hour.isoformat(),
            "expires_at": (hour + timedelta(hours=6)).isoformat(),
            "candidates": [],
        }
        transport = FakeTransport()
        with patch.object(config, "BINANCE_OI_DISCORD_SKIP_EMPTY", True), \
             patch.object(config, "BINANCE_OI_DISCORD_MULTI_HOUR_WINDOW", 6):
            result = notify_oi_feed(feed, transport=transport, db_path=self.db, now=lambda: hour)
        self.assertEqual(result["hour"], "skipped_empty")
        self.assertEqual(result["multi"], "skipped")
        self.assertEqual(transport.messages, [])


if __name__ == "__main__":
    unittest.main()
