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


def _sticky_feed(now: datetime, *, expires_at: datetime):
    from symbol_rotation import build_feed

    return build_feed(
        [{
            "asset": "SOL", "as_of": now, "source": "test", "interval": "24h",
            "retrieved_at": now, "reference_price": 100.0, "current_price": 110.0,
        }],
        now,
        generated_at=now,
    ) | {
        "valid_from": (now - timedelta(hours=5)).isoformat().replace("+00:00", "Z"),
        "valid_until": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "watchlist_entries": [{
            "asset": "SOL", "permanent": False,
            "first_selected_at": now.isoformat().replace("+00:00", "Z"),
            "last_selected_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            "last_feed_id": "sticky-feed", "last_rank_side": "gainer", "last_rank": 1,
        }],
        "symbols": ["BTC", "ETH", "PAXG", "QQQUSDT", "SOL"],
        "symbol_count": 5,
        "effective_symbol_count": 5,
        "watchlist_entry_count": 1,
        "effective_universe_version": "universe-sticky",
        "freshness_state": "degraded",
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

    def test_unexpired_sticky_state_is_healthy_when_feed_is_stale(self, tmp_path):
        now = datetime(2026, 9, 7, 12, tzinfo=UTC)
        path = tmp_path / "feed.json"
        path.write_text(json.dumps(_sticky_feed(now, expires_at=now + timedelta(hours=1))))
        with patch.object(symbol_rotation_healthcheck, "FEED", path):
            assert symbol_rotation_healthcheck.feed_ready(now) is True
