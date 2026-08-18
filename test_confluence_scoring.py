import unittest

from confluence_scoring import (
    clamp01,
    confidence_from_confluence,
    proximity_score,
    weighted_confluence,
    zone_proximity_bin,
)


class ConfluenceScoringTests(unittest.TestCase):
    def test_zone_bins(self):
        self.assertEqual(zone_proximity_bin(0.1), "same_zone")
        self.assertEqual(zone_proximity_bin(0.5), "near_zone")
        self.assertEqual(zone_proximity_bin(1.0), "far")

    def test_weighted_score_and_uncalibrated_confidence(self):
        score, parts = weighted_confluence(
            {"a": 1.0, "b": 0.5, "contradiction_penalty": 0.0},
            {"a": 0.6, "b": 0.4, "contradiction_penalty": 0.2},
        )
        self.assertGreater(score, 0.5)
        self.assertIn("a", parts)
        conf, status = confidence_from_confluence(score)
        self.assertEqual(status, "uncalibrated")
        self.assertAlmostEqual(conf, clamp01(score), places=4)

    def test_proximity_far_is_zero(self):
        self.assertEqual(proximity_score(2.0), 0.0)


if __name__ == "__main__":
    unittest.main()
