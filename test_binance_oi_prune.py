"""ADR-013 P0b: hard prune of aged binance_oi tables."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from binance_oi_prune import format_prune_log, prune_binance_oi_db


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


class BinanceOiPruneTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._td.name) / "binance_oi_test.db")
        self._prev = config.BINANCE_OI_DB_PATH
        config.BINANCE_OI_DB_PATH = self.db_path
        config.init_binance_oi_db(self.db_path)
        self.conn = config.get_db_connection(read_only=False, db_path=self.db_path)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        config.BINANCE_OI_DB_PATH = self._prev
        self._td.cleanup()

    def _seed(self) -> None:
        old = NOW - timedelta(days=45)
        fresh = NOW - timedelta(hours=2)
        src = "binance_usdm"
        # observations
        self.conn.execute(
            """INSERT INTO binance_oi_rotation_observations VALUES
            (?, ?, 'v1', 'OLDUSDT', 'OLD', 'USDT', 'PERPETUAL', true, NULL,
             1e7, 1e8, 0.1, 1e6, 0.01, 1e6, 1.0, 0.99, ?, 60)""",
            (src, old, old),
        )
        self.conn.execute(
            """INSERT INTO binance_oi_rotation_observations VALUES
            (?, ?, 'v1', 'NEWUSDT', 'NEW', 'USDT', 'PERPETUAL', true, NULL,
             1e7, 1e8, 0.1, 1e6, 0.01, 1e6, 1.0, 0.99, ?, 60)""",
            (src, fresh, fresh),
        )
        # raw oi
        self.conn.execute(
            """INSERT INTO binance_oi_rotation_raw_oi_history VALUES
            (?, 'OLDUSDT', ?, 1e8, ?, 60)""",
            (src, old, old),
        )
        self.conn.execute(
            """INSERT INTO binance_oi_rotation_raw_oi_history VALUES
            (?, 'NEWUSDT', ?, 1e8, ?, 60)""",
            (src, fresh, fresh),
        )
        # watchlist history — old expired + fresh active
        exp_fresh = NOW + timedelta(hours=24)
        self.conn.execute(
            """INSERT INTO binance_oi_rotation_watchlist_history VALUES
            (?, 'OLD', 'OLDUSDT', ?, 'expired', ?, false, false)""",
            (src, old, old),
        )
        self.conn.execute(
            """INSERT INTO binance_oi_rotation_watchlist_history VALUES
            (?, 'NEW', 'NEWUSDT', ?, 'active', ?, false, false)""",
            (src, fresh, exp_fresh),
        )
        # events
        self.conn.execute(
            """INSERT INTO binance_oi_rotation_events VALUES
            (?, 'OLD', ?, 'v1', 'OLDUSDT', 1, '{}', ?, 60)""",
            (src, old, old),
        )
        self.conn.execute(
            """INSERT INTO binance_oi_rotation_events VALUES
            (?, 'NEW', ?, 'v1', 'NEWUSDT', 1, '{}', ?, 60)""",
            (src, fresh, fresh),
        )
        # scans
        self.conn.execute(
            """INSERT INTO binance_oi_rotation_scans VALUES
            (?, ?, 'v1', 'complete', ?, 60)""",
            (src, old, old),
        )
        self.conn.execute(
            """INSERT INTO binance_oi_rotation_scans VALUES
            (?, ?, 'v1', 'complete', ?, 60)""",
            (src, fresh, fresh),
        )
        try:
            self.conn.commit()
        except Exception:
            pass

    def test_prune_deletes_old_keeps_recent_and_active_membership(self):
        self._seed()
        # Force short retention so 45d-old is deleted, 2h kept
        prev = {
            "BINANCE_OI_OBSERVATIONS_RETENTION_DAYS": config.BINANCE_OI_OBSERVATIONS_RETENTION_DAYS,
            "BINANCE_OI_RAW_OI_RETENTION_DAYS": config.BINANCE_OI_RAW_OI_RETENTION_DAYS,
            "BINANCE_OI_WATCHLIST_HISTORY_RETENTION_DAYS": config.BINANCE_OI_WATCHLIST_HISTORY_RETENTION_DAYS,
            "BINANCE_OI_EVENTS_RETENTION_DAYS": config.BINANCE_OI_EVENTS_RETENTION_DAYS,
            "BINANCE_OI_SCANS_RETENTION_DAYS": config.BINANCE_OI_SCANS_RETENTION_DAYS,
        }
        try:
            config.BINANCE_OI_OBSERVATIONS_RETENTION_DAYS = 30
            config.BINANCE_OI_RAW_OI_RETENTION_DAYS = 30
            config.BINANCE_OI_WATCHLIST_HISTORY_RETENTION_DAYS = 14
            config.BINANCE_OI_EVENTS_RETENTION_DAYS = 30
            config.BINANCE_OI_SCANS_RETENTION_DAYS = 30

            counts = prune_binance_oi_db(self.conn, now=NOW, db_path=self.db_path)
            self.assertGreaterEqual(counts["observations"], 1)
            self.assertGreaterEqual(counts["raw_oi"], 1)
            self.assertGreaterEqual(counts["watchlist_hist"], 1)

            obs = self.conn.execute(
                "SELECT asset FROM binance_oi_rotation_observations ORDER BY asset"
            ).fetchall()
            self.assertEqual([r[0] for r in obs], ["NEW"])

            active = self.conn.execute(
                """
                SELECT asset FROM binance_oi_rotation_watchlist_history
                WHERE state IN ('entered','active') AND expires_at > ?
                """,
                (NOW,),
            ).fetchall()
            self.assertEqual([r[0] for r in active], ["NEW"])

            log = format_prune_log(counts)
            self.assertIn("[oi-prune]", log)
            self.assertIn("observations=-", log)
        finally:
            for k, v in prev.items():
                setattr(config, k, v)


if __name__ == "__main__":
    unittest.main()
