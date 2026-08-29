import unittest
from datetime import datetime, timezone

from orchestrator import _parse_timestamp


class OrchestratorTimestampTests(unittest.TestCase):
    def test_parses_sqlite_timestamp_as_utc(self):
        self.assertEqual(
            _parse_timestamp("2026-08-29 12:34:56"),
            datetime(2026, 8, 29, 12, 34, 56, tzinfo=timezone.utc),
        )

    def test_normalizes_offset_timestamp(self):
        self.assertEqual(
            _parse_timestamp("2026-08-29T14:34:56+02:00").hour,
            12,
        )


if __name__ == "__main__":
    unittest.main()
