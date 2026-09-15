import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts import ws_healthcheck


UTC = timezone.utc


def _health(recorded_at: datetime, *, last_bar_at: datetime | None = None, status="healthy"):
    timestamp = recorded_at.isoformat().replace("+00:00", "Z")
    last_bar = (last_bar_at or recorded_at).isoformat().replace("+00:00", "Z")
    return {
        "service": "ws_gateway",
        "status": status,
        "ts": timestamp,
        "last_bar_at": last_bar,
        "active_connections": 7,
    }


def test_recent_healthy_feed_and_process_are_healthy(tmp_path):
    path = tmp_path / "ws_health.json"
    path.write_text(json.dumps(_health(datetime.now(UTC))))
    with patch.object(ws_healthcheck, "HEALTH", path), \
         patch.object(ws_healthcheck, "process_running", return_value=True):
        assert ws_healthcheck.main() == 0


def test_stale_feed_is_unhealthy(tmp_path):
    path = tmp_path / "ws_health.json"
    old = datetime.now(UTC) - timedelta(minutes=2)
    path.write_text(json.dumps(_health(old)))
    with patch.object(ws_healthcheck, "HEALTH", path), \
         patch.object(ws_healthcheck, "process_running", return_value=True):
        assert ws_healthcheck.main() == 1


def test_missing_connections_are_unhealthy(tmp_path):
    path = tmp_path / "ws_health.json"
    payload = _health(datetime.now(UTC))
    payload["active_connections"] = 0
    path.write_text(json.dumps(payload))
    with patch.object(ws_healthcheck, "HEALTH", path), \
         patch.object(ws_healthcheck, "process_running", return_value=True):
        assert ws_healthcheck.main() == 1


def test_missing_process_is_unhealthy(tmp_path):
    path = tmp_path / "ws_health.json"
    path.write_text(json.dumps(_health(datetime.now(UTC))))
    with patch.object(ws_healthcheck, "HEALTH", path), \
         patch.object(ws_healthcheck, "process_running", return_value=False):
        assert ws_healthcheck.main() == 1
