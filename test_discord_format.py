import unittest
from datetime import datetime, timedelta, timezone

from discord_format import (
    format_discord_signal,
    format_oi_hour_message,
    format_oi_multi_hour_message,
    format_pct,
    format_usd,
    multi_hour_boundary,
)


class DiscordFormatTests(unittest.TestCase):
    def test_format_usd_and_pct(self):
        self.assertEqual(format_usd(1_035_965.74), "$1.04M")
        self.assertEqual(format_pct(0.0678), "+6.8%")
        self.assertEqual(format_pct(-0.022), "-2.2%")

    def test_discord_alpha_markdown(self):
        event = {
            "asset": "ETH",
            "direction": "long",
            "setup_class": "accumulation_base",
            "strategy_id": "accumulation-base-v1",
            "phase": "confirmed_pullback",
            "confidence": 0.65,
            "entry_condition": {"type": "limit_at_ema_context", "price": 1890.23},
            "invalidation_price": 1853.61,
            "targets": [1945.16],
            "observed_at": "2026-08-17T02:15:00+00:00",
            "valid_until": "2026-08-17T06:15:00+00:00",
            "feature_snapshot": {"volume_spike_multiple": 8.74, "ema_distance_pct": 0.45},
        }
        message = format_discord_signal(event)
        self.assertIn("**ALPHA · LONG · ETH**", message)
        self.assertIn("Accumulation base", message)
        self.assertIn("**65%**", message)
        self.assertIn("1890.23", message)
        self.assertIn("vol spike 8.74×", message)

    def test_oi_hour_message_top_candidates(self):
        feed = {
            "completed_interval_at": "2026-08-18T01:00:00+00:00",
            "expires_at": "2026-08-18T07:00:00+00:00",
            "candidates": [
                {
                    "rank": 1,
                    "asset": "LAB",
                    "symbol": "LABUSDT",
                    "oi_change_1h_pct": 0.068,
                    "oi_change_1h_usd": 1_035_965,
                    "open_interest_usd": 16_311_394,
                    "price_change_1h": -0.022,
                    "volume_anomaly": 1.88,
                }
            ],
        }
        message = format_oi_hour_message(feed)
        self.assertIsNotNone(message)
        self.assertIn("**OI ROTATION**", message)
        self.assertIn("LAB", message)
        self.assertIn("+6.8%", message)
        self.assertIn("not an alpha entry signal", message)

    def test_oi_hour_empty_returns_none(self):
        self.assertIsNone(format_oi_hour_message({
            "completed_interval_at": "2026-08-18T01:00:00+00:00",
            "expires_at": "2026-08-18T07:00:00+00:00",
            "candidates": [],
        }))

    def test_multi_hour_digest_and_boundary(self):
        end = datetime(2026, 8, 18, 5, 0, tzinfo=timezone.utc)
        self.assertTrue(multi_hour_boundary(end, 6))
        self.assertFalse(multi_hour_boundary(end - timedelta(hours=1), 6))
        rows = [
            {
                "completed_interval_at": end - timedelta(hours=2),
                "asset": "LAB",
                "rank": 1,
                "oi_change_1h_pct": 0.03,
                "oi_change_1h_usd": 500_000,
                "open_interest_usd": 10_000_000,
                "price_change_1h": -0.01,
                "volume_anomaly": 1.2,
            },
            {
                "completed_interval_at": end,
                "asset": "LAB",
                "rank": 1,
                "oi_change_1h_pct": 0.068,
                "oi_change_1h_usd": 1_000_000,
                "open_interest_usd": 16_000_000,
                "price_change_1h": -0.022,
                "volume_anomaly": 1.88,
            },
            {
                "completed_interval_at": end,
                "asset": "XYZ",
                "rank": 2,
                "oi_change_1h_pct": 0.04,
                "oi_change_1h_usd": 800_000,
                "open_interest_usd": 20_000_000,
                "price_change_1h": 0.01,
                "volume_anomaly": 1.5,
            },
        ]
        message = format_oi_multi_hour_message(
            window_end=end,
            window_hours=6,
            hour_rows=rows,
            generated_at=end + timedelta(minutes=2),
        )
        self.assertIn("multi-hour", message)
        self.assertIn("Repeat hits", message)
        self.assertIn("LAB", message)
        self.assertIn("Latest hour", message)
        self.assertIn("By hour", message)


if __name__ == "__main__":
    unittest.main()
