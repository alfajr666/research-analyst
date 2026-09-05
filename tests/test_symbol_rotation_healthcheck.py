import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts import symbol_rotation_healthcheck


UTC = timezone.utc


def _feed(now: datetime, *, status: str = "ready", valid_until: datetime | None = None):
    return {
        "schema_version": 1,
        "feed_id": "performance-test",
        "algorithm_version": "performance-24h-v1",
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "valid_from": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "valid_until": (valid_until or now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "permanent_symbols": ["BTC", "ETH", "PAXG", "QQQUSDT"],
        "rotating_symbol_count": 30,
        "symbol_count": 4,
        "gainers": [],
        "losers": [],
        "symbols": ["BTC", "ETH", "PAXG", "QQQUSDT"],
        "status": status,
    }


class TestSymbolRotationHealthcheck:
    def test_current_ready_feed_and_process_are_healthy(self, tmp_path):
        now = datetime.now(UTC)
        path = tmp_path / "feed.json"
        path.write_text(json.dumps(_feed(now)))
        with patch.object(symbol_rotation_healthcheck, "FEED", path), \
             patch.object(symbol_rotation_healthcheck, "process_running", return_value=True):
            assert symbol_rotation_healthcheck.feed_ready(now) is True
            assert symbol_rotation_healthcheck.main() == 0

    def test_current_fallback_feed_is_healthy(self, tmp_path):
        now = datetime(2026, 9, 4, 12, tzinfo=UTC)
        path = tmp_path / "feed.json"
        path.write_text(json.dumps(_feed(now, status="fallback")))
        with patch.object(symbol_rotation_healthcheck, "FEED", path):
            assert symbol_rotation_healthcheck.feed_ready(now) is True

    def test_expired_feed_is_unhealthy(self, tmp_path):
        now = datetime(2026, 9, 4, 12, tzinfo=UTC)
        path = tmp_path / "feed.json"
        path.write_text(json.dumps(_feed(now, valid_until=now - timedelta(seconds=1))))
        with patch.object(symbol_rotation_healthcheck, "FEED", path):
            assert symbol_rotation_healthcheck.feed_ready(now) is False

    def test_missing_process_is_unhealthy(self, tmp_path):
        now = datetime(2026, 9, 4, 12, tzinfo=UTC)
        path = tmp_path / "feed.json"
        path.write_text(json.dumps(_feed(now)))
        with patch.object(symbol_rotation_healthcheck, "FEED", path), \
             patch.object(symbol_rotation_healthcheck, "process_running", return_value=False):
            assert symbol_rotation_healthcheck.main() == 1
