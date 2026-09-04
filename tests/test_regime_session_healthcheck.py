import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import regime_session_healthcheck


def _cycle():
    return {
        "assets": 34,
        "cutoff_at": "2026-09-04T12:55:00+00:00",
        "history_1h_ready": 34,
        "history_4h_ready": 34,
        "score_ready": 34,
        "gate_allow": 34,
        "gate_block": 0,
        "mode": "shadow",
    }


class RegimeSessionHealthcheckTests(unittest.TestCase):
    def test_recent_complete_cycle_is_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "regime.log"
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            path.write_text(f"{now}: {json.dumps(_cycle())}\n")
            with patch.object(regime_session_healthcheck, "LOG", path), \
                 patch.object(regime_session_healthcheck, "process_running", return_value=True):
                self.assertEqual(regime_session_healthcheck.main(), 0)

    def test_stale_cycle_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "regime.log"
            path.write_text("2020-01-01 00:00:00: " + json.dumps(_cycle()) + "\n")
            with patch.object(regime_session_healthcheck, "LOG", path), \
                 patch.object(regime_session_healthcheck, "process_running", return_value=True):
                self.assertEqual(regime_session_healthcheck.main(), 1)

    def test_missing_cycle_fields_are_unhealthy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "regime.log"
            cycle = _cycle()
            del cycle["history_1h_ready"]
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            path.write_text(f"{now}: {json.dumps(cycle)}\n")
            with patch.object(regime_session_healthcheck, "LOG", path), \
                 patch.object(regime_session_healthcheck, "process_running", return_value=True):
                self.assertEqual(regime_session_healthcheck.main(), 1)

    def test_split_json_cycle_is_reassembled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "regime.log"
            serialized = json.dumps(_cycle())
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            midpoint = len(serialized) // 2
            path.write_text(f"{now}: {serialized[:midpoint]}\n{serialized[midpoint:]}\n")
            with patch.object(regime_session_healthcheck, "LOG", path), \
                 patch.object(regime_session_healthcheck, "process_running", return_value=True):
                self.assertEqual(regime_session_healthcheck.main(), 0)

    def test_unprefixed_json_cycle_uses_cutoff_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "regime.log"
            path.write_text(json.dumps(_cycle()) + "\n")
            with patch.object(regime_session_healthcheck, "LOG", path), \
                 patch.object(regime_session_healthcheck, "process_running", return_value=True):
                self.assertEqual(regime_session_healthcheck.main(), 1)


if __name__ == "__main__":
    unittest.main()
