import unittest
import tempfile
from pathlib import Path

import config
from openmarket_adapter import fetch_htf_profile, fetch_15m_flow


class OpenMarketAdapterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "m.db"
        config.init_market_db(self.db)
        self.prev_enabled = config.OPENMARKET_ENABLED
        self.prev_key = config.OPENMARKET_API_KEY
        config.OPENMARKET_ENABLED = False
        config.OPENMARKET_API_KEY = ""

    def tearDown(self):
        config.OPENMARKET_ENABLED = self.prev_enabled
        config.OPENMARKET_API_KEY = self.prev_key
        self.directory.cleanup()

    def test_disabled_returns_unavailable_without_blocking(self):
        res = fetch_htf_profile(["BTC", "ETH"], "cut-1", db_path=str(self.db))
        self.assertEqual(res["BTC"]["status"], "unavailable")
        self.assertEqual(res["ETH"]["status"], "unavailable")

    def test_request_log_records_skips(self):
        fetch_15m_flow(["SOL"], "cut-2", db_path=str(self.db))
        conn = config.get_db_connection(read_only=True, db_path=self.db)
        try:
            rows = conn.execute("SELECT status, request_type FROM source_request_log WHERE source='openmarket'").fetchall()
            self.assertTrue(any(r[0] == "skipped" for r in rows))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
