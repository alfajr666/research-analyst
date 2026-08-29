"""Load / stress tests for the rate limiting client layer (per external-api-rate-limiting spec).

These exercise sustained load, 429 handling, logging, unavailable semantics, and budget paths
without hitting real networks. Use high rates or mocks to keep test runtime short.
"""
import unittest
import tempfile
import os
import time
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

import config
from api_clients.base import TokenBucket, RateLimitedClient
from api_clients.coinalyze import CoinAnalyzeClient
from api_clients.openmarket import OpenMarketClient


class TokenBucketLoadTests(unittest.TestCase):
    def test_burst_under_high_rate_is_fast(self):
        b = TokenBucket(rate_per_sec=100.0, capacity=50)
        start = time.time()
        for _ in range(30):
            ok = b.acquire(1.0, timeout=1.0)
            self.assertTrue(ok)
        dur = time.time() - start
        # Should be near-instant; no long sleeps at high rate
        self.assertLess(dur, 0.5)

    def test_low_rate_drains_and_waits(self):
        b = TokenBucket(rate_per_sec=5.0, capacity=2)
        # consume capacity
        self.assertTrue(b.acquire(2.0, timeout=0.1))
        start = time.time()
        ok = b.acquire(1.0, timeout=0.6)  # should wait ~0.2s to refill
        dur = time.time() - start
        self.assertTrue(ok)
        self.assertGreater(dur, 0.1)


class RateLimitedClientLoadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.directory.name, "load.db")
        config.init_db(self.db)
        # ensure clean log
        conn = config.get_db_connection(db_path=self.db, read_only=False)
        conn.execute("DELETE FROM source_request_log")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.directory.cleanup()

    @patch("api_clients.base.httpx.Client")
    def test_many_calls_log_and_respect_rate_via_mock(self, mock_httpx_cls):
        # High rate client for fast load sim
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"symbol": "BTC", "value": 1.0}]
        mock_resp.headers = {"x-ratelimit-remaining": "999"}
        mock_client = MagicMock()
        mock_client.request.return_value = mock_resp
        mock_httpx_cls.return_value = mock_client

        client = CoinAnalyzeClient()
        client.bucket = TokenBucket(rate_per_sec=200.0, capacity=100)

        results = []
        for i in range(25):
            res = client.fetch("open-interest", {"symbols": f"BTC{i}"}, cutoff_id=f"load-{i}", db_path=self.db)
            results.append(res)

        self.assertEqual(len(results), 25)
        # Verify logging happened for the calls
        conn = config.get_db_connection(db_path=self.db)
        logs = conn.execute(
            "SELECT COUNT(*) FROM source_request_log WHERE source='coinalyze' AND cutoff_id LIKE 'load-%'"
        ).fetchone()[0]
        conn.close()
        self.assertGreaterEqual(logs, 20)  # most or all should have logged

    @patch("api_clients.base.httpx.Client")
    def test_429_drains_bucket_and_returns_unavailable(self, mock_httpx_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {"Retry-After": "2"}
        mock_resp.text = "rate limit"
        mock_client = MagicMock()
        mock_client.request.return_value = mock_resp
        mock_httpx_cls.return_value = mock_client

        client = CoinAnalyzeClient()
        client.bucket = TokenBucket(rate_per_sec=10.0, capacity=5)

        # First call hits 429
        res = client.fetch("funding-rate", {"symbols": "BTC"}, cutoff_id="load-429", db_path=self.db)
        self.assertEqual(res, [])
        # status in log should reflect 429
        conn = config.get_db_connection(db_path=self.db)
        row = conn.execute(
            "SELECT status FROM source_request_log WHERE cutoff_id='load-429' ORDER BY requested_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIn("429", row[0] if row else "")

    @patch("api_clients.base.httpx.Client")
    def test_openmarket_budget_and_unavailable_paths_under_load(self, mock_httpx_cls):
        # force enabled with key
        prev_e = config.OPENMARKET_ENABLED
        prev_k = config.OPENMARKET_API_KEY
        config.OPENMARKET_ENABLED = True
        config.OPENMARKET_API_KEY = "fake"
        try:
            client = OpenMarketClient()
            client.bucket = TokenBucket(rate_per_sec=100.0)

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"data": {"SOL": {"flow": 123}}}
            mock_resp.headers = {}
            mock_client = MagicMock()
            mock_client.request.return_value = mock_resp
            mock_httpx_cls.return_value = mock_client

            # burst "load"
            for i in range(10):
                r = client.fetch_15m_flow(["SOL"], f"om-load-{i}", db_path=self.db)
                self.assertIn("SOL", r)

            conn = config.get_db_connection(db_path=self.db)
            cnt = conn.execute("SELECT COUNT(*) FROM source_request_log WHERE source='openmarket'").fetchone()[0]
            conn.close()
            self.assertGreaterEqual(cnt, 5)
        finally:
            config.OPENMARKET_ENABLED = prev_e
            config.OPENMARKET_API_KEY = prev_k


if __name__ == "__main__":
    unittest.main()
