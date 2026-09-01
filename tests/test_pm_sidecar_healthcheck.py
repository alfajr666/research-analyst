import os
import unittest
from unittest.mock import patch

from scripts import pm_sidecar_healthcheck


class PMSidecarHealthcheckTests(unittest.TestCase):
    def test_log_window_covers_one_five_minute_cadence(self):
        with patch.dict(os.environ, {"PM_CADENCE_MINUTES": "5"}):
            self.assertEqual(pm_sidecar_healthcheck.max_log_age_seconds(), 420)

    def test_log_window_matches_sidecar_minimum_cadence(self):
        with patch.dict(os.environ, {"PM_CADENCE_MINUTES": "1"}):
            self.assertEqual(pm_sidecar_healthcheck.max_log_age_seconds(), 420)
