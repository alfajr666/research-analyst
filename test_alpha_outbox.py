import json
import tempfile
import unittest
from pathlib import Path

from alpha_outbox import dedupe_key, write_event


def _event():
    return {
        "schema_version": 1,
        "strategy_id": "impulse-ignition-v1",
        "asset": "SOL",
        "direction": "long",
        "observed_at": "2026-08-16T10:15:00+00:00",
    }


class AlphaOutboxTests(unittest.TestCase):
    def test_write_is_atomic_append_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = Path(directory)
            created, path = write_event(_event(), outbox)
            duplicate, duplicate_path = write_event(_event(), outbox)

            self.assertTrue(created)
            self.assertFalse(duplicate)
            self.assertEqual(path, duplicate_path)
            self.assertEqual(path.name, f"{dedupe_key(_event())}.json")
            self.assertEqual(list(outbox.glob(".alpha-*.tmp")), [])
            with path.open() as handle:
                written = json.load(handle)
            self.assertEqual(written["dedupe_key"], dedupe_key(_event()))
            self.assertIn("alpha_id", written)


if __name__ == "__main__":
    unittest.main()
