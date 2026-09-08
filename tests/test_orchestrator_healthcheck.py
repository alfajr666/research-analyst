import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts import orchestrator_healthcheck


UTC = timezone.utc


def _health(recorded_at: datetime, *, evaluations: int = 528):
    timestamp = recorded_at.isoformat().replace("+00:00", "Z")
    return {
        "bot": "research-analyst",
        "cycleIntervalMs": 900000,
        "lastCycleAt": timestamp,
        "evalsLastCycle": evaluations,
        "dataFreshness": {"ageMin": 0.3, "barsLast5m": 48},
        "evaluation": {"strategy_evaluations": evaluations},
        "ts": timestamp,
    }


class TestOrchestratorHealthcheck:
    def test_recent_complete_cycle_and_process_are_healthy(self, tmp_path):
        path = tmp_path / "health.json"
        path.write_text(json.dumps(_health(datetime.now(UTC))))
        with patch.object(orchestrator_healthcheck, "HEALTH", path), \
             patch.object(orchestrator_healthcheck, "process_running", return_value=True):
            assert orchestrator_healthcheck.main() == 0

    def test_stale_cycle_is_unhealthy(self, tmp_path):
        path = tmp_path / "health.json"
        path.write_text(json.dumps(_health(datetime.now(UTC) - timedelta(days=1))))
        with patch.object(orchestrator_healthcheck, "HEALTH", path), \
             patch.object(orchestrator_healthcheck, "process_running", return_value=True), \
             patch.object(orchestrator_healthcheck, "active_pipeline_recent", return_value=False):
            assert orchestrator_healthcheck.main() == 1

    def test_recent_running_pipeline_keeps_stale_cycle_healthy(self, tmp_path):
        path = tmp_path / "health.json"
        path.write_text(json.dumps(_health(datetime.now(UTC) - timedelta(days=1))))
        with patch.object(orchestrator_healthcheck, "HEALTH", path), \
             patch.object(orchestrator_healthcheck, "process_running", return_value=True), \
             patch.object(orchestrator_healthcheck, "active_pipeline_recent", return_value=True):
            assert orchestrator_healthcheck.main() == 0

    def test_missing_process_is_unhealthy(self, tmp_path):
        path = tmp_path / "health.json"
        path.write_text(json.dumps(_health(datetime.now(UTC))))
        with patch.object(orchestrator_healthcheck, "HEALTH", path), \
             patch.object(orchestrator_healthcheck, "process_running", return_value=False):
            assert orchestrator_healthcheck.main() == 1

    def test_missing_or_invalid_cycle_fields_are_unhealthy(self, tmp_path):
        path = tmp_path / "health.json"
        payload = _health(datetime.now(UTC))
        del payload["evaluation"]
        path.write_text(json.dumps(payload))
        with patch.object(orchestrator_healthcheck, "HEALTH", path), \
             patch.object(orchestrator_healthcheck, "process_running", return_value=True):
            assert orchestrator_healthcheck.main() == 1

    def test_negative_evaluation_count_is_unhealthy(self, tmp_path):
        path = tmp_path / "health.json"
        path.write_text(json.dumps(_health(datetime.now(UTC), evaluations=-1)))
        with patch.object(orchestrator_healthcheck, "HEALTH", path), \
             patch.object(orchestrator_healthcheck, "process_running", return_value=True):
            assert orchestrator_healthcheck.main() == 1
