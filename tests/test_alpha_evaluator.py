import unittest
from datetime import datetime, timezone

from alpha_evaluator import event_from_candidate


OBSERVED_AT = datetime(2026, 8, 17, tzinfo=timezone.utc)


class AlphaEvaluatorTests(unittest.TestCase):
    def test_ignition_confidence_tracks_candidate_score(self):
        candidate = {"asset": "SOL", "score": 61.25, "close": 100.0, "observed_at": OBSERVED_AT}
        self.assertEqual(event_from_candidate(candidate, "ignition")["confidence"], 0.6125)

    def test_continuation_confidence_tracks_candidate_score(self):
        candidate = {
            "asset": "SOL", "score": 83.75, "close": 100.0,
            "breakout_level": 102.0, "observed_at": OBSERVED_AT,
        }
        self.assertEqual(event_from_candidate(candidate, "acceleration")["confidence"], 0.8375)


if __name__ == "__main__":
    unittest.main()
