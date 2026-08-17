import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class RuntimeTopologyTests(unittest.TestCase):
    def test_pm2_starts_only_the_orchestrator_database_writer(self):
        ecosystem = (ROOT / "ecosystem.config.js").read_text(encoding="utf-8")
        self.assertIn('script: "orchestrator.py"', ecosystem)
        self.assertNotIn('script: "binance_oi_rotation_worker.py"', ecosystem)

    def test_orchestrator_deduplicates_scans_by_scanner_version(self):
        orchestrator = (ROOT / "orchestrator.py").read_text(encoding="utf-8")
        self.assertIn("scanner_version = ?", orchestrator)
        self.assertIn("config.BINANCE_OI_ROTATION_SCANNER_VERSION", orchestrator)


if __name__ == "__main__":
    unittest.main()
