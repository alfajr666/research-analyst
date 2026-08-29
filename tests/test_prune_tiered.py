import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import orchestrator


def _insert_obs(conn, interval, age_days, asset="BTC"):
    end = datetime.now(timezone.utc) - timedelta(days=age_days)
    oid = f"o-{interval}-{age_days}-{asset}"
    conn.execute(
        """INSERT OR IGNORE INTO source_observations
           (observation_id, source, venue, native_symbol, asset, market_kind, interval,
            source_start, source_end, retrieved_at, retrieval_kind, payload_json)
           VALUES (?, 'bybit_ws', 'bybit', ?, ?, 'usdt_perp', ?, ?, ?, ?, 'backfill', '{}')""",
        (oid, f"{asset}USDT", asset, interval, end, end, end),
    )


class TieredPruneTests(unittest.TestCase):
    def setUp(self):
        self.prev = {k: getattr(config, k, None) for k in ("PRUNE_INTERVAL_DAYS",)}
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "m.db"
        config.init_db(self.db)
        conn = config.get_db_connection(read_only=False, db_path=self.db)
        try:
            # old vs recent for each tier
            _insert_obs(conn, "1m", 10)   # beyond 7d -> prune
            _insert_obs(conn, "1m", 1)    # keep
            _insert_obs(conn, "5m", 40)   # beyond 30d -> prune
            _insert_obs(conn, "5m", 5)    # keep
            _insert_obs(conn, "15m", 100) # beyond 90d -> prune
            _insert_obs(conn, "15m", 10)  # keep
            _insert_obs(conn, "1h", 400)  # beyond 365d -> prune
            _insert_obs(conn, "1h", 10)   # keep
            _insert_obs(conn, "1d", 100)  # uncovered -> fallback futures_retention_days=5 -> prune
            _insert_obs(conn, "1d", 1)    # keep (fallback)
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        for k, v in self.prev.items():
            setattr(config, k, v)
        self.directory.cleanup()

    def _count(self, interval):
        conn = config.get_db_connection(read_only=True, db_path=self.db)
        try:
            return conn.execute(
                "SELECT count(*) FROM source_observations WHERE interval = ?", (interval,)
            ).fetchone()[0]
        finally:
            conn.close()

    def test_tiered_prune_respects_per_interval_ttl(self):
        conn = config.get_db_connection(read_only=False, db_path=self.db)
        try:
            orchestrator.prune_db(conn, futures_retention_days=5)
        finally:
            conn.close()

        self.assertEqual(self._count("1m"), 1)   # recent kept
        self.assertEqual(self._count("5m"), 1)
        self.assertEqual(self._count("15m"), 1)
        self.assertEqual(self._count("1h"), 1)
        self.assertEqual(self._count("1d"), 1)   # fallback kept recent

    def test_disable_tier_via_zero(self):
        # Setting a tier to 0 keeps even old rows of that interval.
        config.PRUNE_INTERVAL_DAYS = dict(getattr(config, "PRUNE_INTERVAL_DAYS", {}))
        config.PRUNE_INTERVAL_DAYS["1m"] = 0
        conn = config.get_db_connection(read_only=False, db_path=self.db)
        try:
            orchestrator.prune_db(conn, futures_retention_days=5)
        finally:
            conn.close()
        self.assertEqual(self._count("1m"), 2)  # nothing pruned for 1m


if __name__ == "__main__":
    unittest.main()
