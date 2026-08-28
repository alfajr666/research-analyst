import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import rotation_feed


class RotationFeedTests(unittest.TestCase):
    def setUp(self):
        self.prev_enabled = config.ROTATION_FEED_ENABLED
        self.prev_path = os.environ.get("BINANCE_OI_ROTATION_FEED_PATH")
        self.prev_db = os.environ.get("BINANCE_OI_DB_PATH")
        self.directory = tempfile.TemporaryDirectory()
        self.feed = Path(self.directory.name) / "feed.json"
        self.oi_db = Path(self.directory.name) / "oi.db"
        os.environ["BINANCE_OI_ROTATION_FEED_PATH"] = str(self.feed)
        os.environ["BINANCE_OI_DB_PATH"] = str(self.oi_db)
        # config caches env at import; patch the attributes directly.
        self.prev_cfg_feed = getattr(config, "BINANCE_OI_ROTATION_FEED_PATH", None)
        self.prev_cfg_db = getattr(config, "BINANCE_OI_DB_PATH", None)
        config.BINANCE_OI_ROTATION_FEED_PATH = str(self.feed)
        config.BINANCE_OI_DB_PATH = str(self.oi_db)

    def tearDown(self):
        config.ROTATION_FEED_ENABLED = self.prev_enabled
        config.BINANCE_OI_ROTATION_FEED_PATH = self.prev_cfg_feed
        config.BINANCE_OI_DB_PATH = self.prev_cfg_db
        if self.prev_path is None:
            os.environ.pop("BINANCE_OI_ROTATION_FEED_PATH", None)
        else:
            os.environ["BINANCE_OI_ROTATION_FEED_PATH"] = self.prev_path
        if self.prev_db is None:
            os.environ.pop("BINANCE_OI_DB_PATH", None)
        else:
            os.environ["BINANCE_OI_DB_PATH"] = self.prev_db
        self.directory.cleanup()

    def test_disabled_is_noop(self):
        config.ROTATION_FEED_ENABLED = False
        res = rotation_feed.refresh_rotation_feed()
        self.assertEqual(res["enabled"], False)
        self.assertFalse(self.feed.exists())

    def test_enabled_exports_active_watchlist(self):
        config.ROTATION_FEED_ENABLED = True
        config.init_binance_oi_db()
        conn = config.get_db_connection(read_only=False, db_path=config.BINANCE_OI_DB_PATH)
        try:
            now = datetime.now(timezone.utc)
            conn.execute(
                """INSERT INTO binance_oi_rotation_watchlist_history
                   (source, asset, symbol, observed_at, state, expires_at,
                    deep_backfill_required, overlap_annotated)
                   VALUES ('binance_oi', 'SOL', 'SOLUSDT', ?, 'active', ?, false, false)""",
                (now, now + timedelta(days=7)),
            )
            conn.commit()
        finally:
            conn.close()

        res = rotation_feed.refresh_rotation_feed()
        self.assertEqual(res["enabled"], True)
        self.assertEqual(res["written"], 1)
        self.assertTrue(self.feed.exists())
        data = json.loads(self.feed.read_text(encoding="utf-8"))
        self.assertIn("SOL", data["candidates"])


if __name__ == "__main__":
    unittest.main()
