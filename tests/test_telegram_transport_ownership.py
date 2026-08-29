import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LEGACY_AUTOMATION_MODULES = (
    "orchestrator.py",
    "regime_signal.py",
)


class TelegramTransportOwnershipTests(unittest.TestCase):
    def test_only_signal_publisher_contains_telegram_http_transport(self):
        for module in LEGACY_AUTOMATION_MODULES:
            source = (ROOT / "src" / "research_analyst" / module).read_text()
            self.assertNotIn("api.telegram.org", source, module)
            self.assertNotIn("sendMessage", source, module)
            self.assertNotIn("httpx.", source, module)

        self.assertIn("api.telegram.org", (ROOT / "src" / "research_analyst" / "signal_publisher.py").read_text())


if __name__ == "__main__":
    unittest.main()
