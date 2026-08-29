import unittest
from unittest.mock import patch, MagicMock
import config
from api_clients.openmarket import OpenMarketClient


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
