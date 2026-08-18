import unittest
from unittest.mock import patch, MagicMock
import config
from api_clients.coinalyze import CoinAnalyzeClient
from api_clients.openmarket import OpenMarketClient


class TestCoinAnalyzeClient(unittest.TestCase):
    def setUp(self):
        self.client = CoinAnalyzeClient()

    @patch("api_clients.base.RateLimitedClient.request")
    def test_fetch_returns_data_on_ok(self, mock_request):
        mock_request.return_value = {"status": "ok", "data": [{"symbol": "BTC", "value": 123}]}
        res = self.client.fetch("open-interest", {"symbols": "BTCUSDT_PERP.A"})
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["symbol"], "BTC")

    @patch("api_clients.base.RateLimitedClient.request")
    def test_fetch_returns_empty_on_rate_limit(self, mock_request):
        mock_request.return_value = {"status": "unavailable", "reason": "rate_limited"}
        res = self.client.fetch("funding-rate", {"symbols": "BTC"})
        self.assertEqual(res, [])

    @patch("api_clients.base.RateLimitedClient.request")
    def test_fetch_batched(self, mock_request):
        mock_request.return_value = {"status": "ok", "data": [{"symbol": "BTC"}]}
        res = self.client.fetch_batched("open-interest", ["BTC", "ETH"], batch_size=1)
        self.assertGreaterEqual(len(res), 1)


class TestOpenMarketClient(unittest.TestCase):
    def setUp(self):
        self.prev_enabled = config.OPENMARKET_ENABLED
        self.prev_key = config.OPENMARKET_API_KEY
        config.OPENMARKET_ENABLED = True
        config.OPENMARKET_API_KEY = "fake"

    def tearDown(self):
        config.OPENMARKET_ENABLED = self.prev_enabled
        config.OPENMARKET_API_KEY = self.prev_key

    @patch("api_clients.base.RateLimitedClient.request")
    @patch("api_clients.base.RateLimitedClient._log_request")
    def test_returns_unavailable_on_disabled(self, mock_log, mock_request):
        config.OPENMARKET_ENABLED = False
        client = OpenMarketClient()
        res = client.fetch_htf_profile(["BTC"], "cutoff-1")
        self.assertEqual(res["BTC"]["status"], "unavailable")
        mock_request.assert_not_called()

    @patch("api_clients.base.RateLimitedClient.request")
    def test_returns_unavailable_on_request_failure(self, mock_request):
        mock_request.return_value = {"status": "unavailable", "reason": "timeout"}
        client = OpenMarketClient()
        res = client.fetch_15m_flow(["ETH"], "cutoff-2")
        self.assertEqual(res["ETH"]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
