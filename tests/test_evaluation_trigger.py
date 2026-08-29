import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from evaluation_trigger import claim, cutoff_key, pending, publish, recover_claimed, retry


class EvaluationTriggerTests(unittest.TestCase):
    def test_publish_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cutoff = datetime(2026, 8, 29, 14, 55, 42, tzinfo=timezone.utc)
            created, path = publish(cutoff, root)
            duplicate, duplicate_path = publish(cutoff, root)

            self.assertTrue(created)
            self.assertFalse(duplicate)
            self.assertEqual(path, duplicate_path)
            self.assertEqual(path.name, f"{cutoff_key(cutoff)}.json")
            self.assertEqual(json.loads(path.read_text())['interval'], '5m')
            self.assertEqual(pending(root), [path])
            self.assertEqual(list(root.glob('.trigger-*.tmp')), [])

            path.rename(path.with_suffix('.processed'))
            duplicate_processed, _ = publish(cutoff, root)
            self.assertFalse(duplicate_processed)

    def test_claim_recovery_and_bounded_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, path = publish(datetime.now(timezone.utc), root)
            claimed = claim(path)
            claimed.touch()
            self.assertEqual(recover_claimed(root), 0)
            claimed.unlink()
            publish(datetime.now(timezone.utc), root)
            path = pending(root)[0]
            claimed = claim(path)
            failed = retry(claimed, "boom", root)
            self.assertTrue(failed.name.endswith(".json"))
            self.assertEqual(json.loads(failed.read_text())["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
