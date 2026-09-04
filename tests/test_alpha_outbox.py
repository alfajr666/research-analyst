import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from alpha_outbox import dedupe_key, write_event
from signal_publisher import validate_event


def _event():
    return {
        "schema_version": 1,
        "strategy_id": "impulse-ignition-v1",
        "asset": "SOL",
        "direction": "long",
        "observed_at": "2026-08-16T10:15:00+00:00",
    }


def _complete_event_without_targets():
    event = _event()
    event.update({
        "plugin_version": "v1",
        "setup_class": "impulse_ignition",
        "phase": "triggered",
        "observed_at": "2026-08-16T10:15:00+00:00",
        "valid_until": "2026-08-16T10:20:00+00:00",
        "horizon_minutes": 5,
        "confidence": 0.5,
        "confidence_status": "uncalibrated",
        "entry_condition": {"type": "market", "price": 100.0},
        "entry_price": 100.0,
        "invalidation_price": 99.0,
        "data_freshness_seconds": 1.0,
        "structural_context": {
            "asset": "SOL", "cutoff": "2026-08-16T10:15:00+00:00",
            "zones": [{
                "zone_id": "zone-alpha", "asset": "SOL", "type": "order_block", "timeframe": "4h",
                "direction": "bullish", "low": 99.5, "high": 99.75, "state": "active",
                "created_at": "2026-08-16T06:15:00+00:00", "coverage_status": "covered",
                "confirmed_at": "2026-08-16T06:15:00+00:00",
                "source_evidence_ids": ["bar-alpha"],
            }],
            "atr_by_timeframe": {"4h": 0.25},
            "atr_source_bar_ids": {"4h": ["bar-alpha"]},
        },
        "feature_snapshot": {},
    })
    return event


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

    def test_duplicate_write_retries_existing_intent_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = Path(directory)
            with patch("alpha_outbox._maybe_deliver_intent") as deliver:
                write_event(_event(), outbox)
                write_event(_event(), outbox)

            self.assertEqual(deliver.call_count, 2)

    def test_written_derived_target_satisfies_publisher_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = Path(directory)
            with patch("raw_signal_batch.capture", return_value=None), \
                 patch("raw_signal_batch.record_status"), \
                 patch("trade_admission.admit", return_value={
                     "hard_gate": "pass", "hard_gate_reasons": [],
                     "symbol_account_gate": "pass",
                 }):
                created, path = write_event(_complete_event_without_targets(), outbox)

            self.assertTrue(created)
            written = json.loads(path.read_text())
            validate_event(written)
            self.assertEqual(written["targets"], [102.0])


if __name__ == "__main__":
    unittest.main()
