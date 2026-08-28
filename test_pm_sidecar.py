import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import config
import pm_sidecar


class PMSidecarTests(unittest.TestCase):
    def setUp(self):
        self.prev_enabled = config.PM_SIDECAR_ENABLED
        self.prev_key = os.environ.get("LLM_API_KEY")
        config.PM_SIDECAR_ENABLED = False
        os.environ.pop("LLM_API_KEY", None)
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "m.db"
        config.init_db(self.db)
        conn = config.get_db_connection(read_only=False, db_path=self.db)
        try:
            conn.execute(
                """
                INSERT INTO positions_feed
                    (position_id, symbol, asset, side, entry, size, opened_at,
                     strategy_id, current_pnl, status, updated_at)
                VALUES ('P1', 'BTCUSDT', 'BTC', 'long', 60000.0, 1.0, ?,
                        'rsi-reclaim-v1', 0.02, 'open', ?)
                """,
                (datetime(2026, 1, 1, tzinfo=timezone.utc),
                 datetime(2026, 1, 1, tzinfo=timezone.utc)),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        config.PM_SIDECAR_ENABLED = self.prev_enabled
        if self.prev_key is None:
            os.environ.pop("LLM_API_KEY", None)
        else:
            os.environ["LLM_API_KEY"] = self.prev_key
        self.directory.cleanup()

    def _count_advice(self):
        conn = config.get_db_connection(read_only=True, db_path=self.db)
        try:
            return conn.execute("SELECT count(*) FROM pm_advice").fetchone()[0]
        finally:
            conn.close()

    def test_disabled_is_noop(self):
        res = pm_sidecar.run_once(self.db, now=datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc))
        self.assertEqual(res, {"enabled": False, "advices": 0})
        self.assertEqual(self._count_advice(), 0)

    def test_enabled_emits_hold_without_llm_and_dedupes(self):
        config.PM_SIDECAR_ENABLED = True
        now = datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc)
        res = pm_sidecar.run_once(self.db, now=now)
        self.assertTrue(res["enabled"])
        self.assertEqual(res["positions"], 1)
        self.assertEqual(res["advices"], 1)
        self.assertEqual(self._count_advice(), 1)

        # Same 5m cutoff -> second pass must NOT add a duplicate.
        res2 = pm_sidecar.run_once(self.db, now=datetime(2026, 1, 1, 12, 6, tzinfo=timezone.utc))
        self.assertEqual(res2["advices"], 0)
        self.assertEqual(self._count_advice(), 1)

        conn = config.get_db_connection(read_only=True, db_path=self.db)
        try:
            row = conn.execute(
                "SELECT action, reason FROM pm_advice WHERE position_id='P1'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "hold")
        self.assertIn("hold", row[1].lower())

    def test_call_pm_llm_returns_none_without_key(self):
        self.assertIsNone(pm_sidecar.call_pm_llm("ignored prompt"))


if __name__ == "__main__":
    unittest.main()
