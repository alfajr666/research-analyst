import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config
from alpha_outbox import dedupe_key
from intent_publisher import IntentPublisher


def event(observed_at, valid_until):
    payload = {
        "schema_version": 1,
        "alpha_id": "a-1",
        "strategy_id": "continuation-breakout-v1",
        "asset": "BTC",
        "direction": "long",
        "setup_class": "continuation_breakout",
        "phase": "confirmed_expansion",
        "observed_at": observed_at.isoformat(),
        "valid_until": valid_until.isoformat(),
        "horizon_minutes": 240,
        "confidence": 0.67,
        "entry_condition": {"type": "breakout_above", "price": 145.2},
        "invalidation_price": 142.7,
        "targets": [150.2, 151.0],
        "data_freshness_seconds": 1.0,
        "structural_context": {
            "asset": "BTC",
            "cutoff": observed_at.isoformat(),
            "zones": [{
                "zone_id": "zone-intent", "asset": "BTC", "type": "order_block",
                "timeframe": "4h", "direction": "bullish", "low": 144.0,
                "high": 144.2, "state": "active",
                "created_at": (observed_at - timedelta(hours=4)).isoformat(),
                "confirmed_at": (observed_at - timedelta(hours=4)).isoformat(),
                "coverage_status": "covered", "source_evidence_ids": ["bar-intent"],
            }],
            "atr_by_timeframe": {"4h": 1.0},
            "atr_source_bar_ids": {"4h": ["bar-intent"]},
        },
        "feature_snapshot": {"regime": "trending_up"},
    }
    payload["dedupe_key"] = dedupe_key(payload)
    return payload


class IntentPublisherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.outbox = self.root / "alpha_outbox"
        self.outbox.mkdir()
        self.db = self.root / "events.db"
        config.init_analyst_db(self.db)
        self.current_time = datetime(2026, 8, 16, 10, 30, tzinfo=timezone.utc)

    def tearDown(self):
        self.directory.cleanup()

    def write(self, payload):
        (self.outbox / f"{payload['dedupe_key']}.json").write_text(json.dumps(payload))

    def publisher(self):
        return IntentPublisher(self.db, self.outbox, now=lambda: self.current_time)

    def rows(self, query):
        connection = sqlite3.connect(str(self.db))
        try:
            return connection.execute(query).fetchall()
        finally:
            connection.close()

    def test_default_ledger_is_separate_from_market_database(self):
        self.assertNotEqual(Path(config.ANALYST_DB_PATH).resolve(), Path(config.MARKET_DB_PATH).resolve())
        self.assertEqual(IntentPublisher().db_path, config.ANALYST_DB_PATH)

    def test_fresh_schema_omits_retired_delivery_and_llm_tables(self):
        tables = {
            row[0] for row in self.rows(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue({"alpha_events", "alpha_event_status_history"} <= tables)
        self.assertTrue({
            "signal_deliveries", "execution_deliveries", "research_requests",
            "research_artifacts", "research_evidence", "research_run_metrics",
        }.isdisjoint(tables))

    def test_persists_once_and_retries_only_through_shared_bus_seam(self):
        payload = event(self.current_time - timedelta(minutes=15), self.current_time + timedelta(hours=1))
        self.write(payload)
        publisher = self.publisher()
        with patch("alpha_outbox._maybe_deliver_intent", return_value="published") as publish:
            first = publisher.run_once()
            second = publisher.run_once()

        self.assertEqual(first, {"persisted": 1, "published": 1, "failed": 0, "invalid": 0, "skipped": 0})
        self.assertEqual(second, {"persisted": 0, "published": 1, "failed": 0, "invalid": 0, "skipped": 0})
        self.assertEqual(publish.call_count, 2)
        self.assertEqual(self.rows("SELECT count(*) FROM alpha_events"), [(1,)])
        self.assertEqual(
            self.rows("SELECT confidence, observation_status, reason FROM alpha_confidence_observations"),
            [(0.67, "unavailable", "confidence_components_missing_or_invalid")],
        )

    def test_repairs_legacy_admission_only_target(self):
        payload = event(self.current_time - timedelta(minutes=15), self.current_time + timedelta(hours=1))
        payload.pop("targets")
        payload["_admission_result"] = {"selected_take_profit": 151.0}
        self.write(payload)

        with patch("alpha_outbox._maybe_deliver_intent", return_value="published"):
            result = self.publisher().run_once()

        self.assertEqual(result["persisted"], 1)
        self.assertEqual(self.rows("SELECT targets FROM alpha_candidates"), [("[151.0]",)])

    def test_expired_event_is_persisted_without_bus_publication(self):
        payload = event(self.current_time - timedelta(hours=2), self.current_time - timedelta(minutes=1))
        self.write(payload)

        with patch("alpha_outbox._maybe_deliver_intent") as publish:
            result = self.publisher().run_once()

        publish.assert_not_called()
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.rows("SELECT status FROM alpha_events"), [("expired",)])
        self.assertEqual(list(self.outbox.glob("*.json")), [])

    def test_persists_confidence_component_audit(self):
        payload = event(self.current_time - timedelta(minutes=15), self.current_time + timedelta(hours=1))
        payload["feature_snapshot"]["confidence_components"] = {
            "volume": 0.35, "ema_proximity": 0.25,
        }
        self.write(payload)

        with patch("alpha_outbox._maybe_deliver_intent", return_value="published"):
            self.publisher().run_once()

        confidence, components, status, reason = self.rows(
            "SELECT confidence, components_json, observation_status, reason FROM alpha_confidence_observations"
        )[0]
        self.assertEqual(confidence, 0.67)
        self.assertEqual(json.loads(components), {"ema_proximity": 0.25, "volume": 0.35})
        self.assertEqual((status, reason), ("observed", None))

    def test_bus_failure_is_recorded_without_venue_fallback(self):
        payload = event(self.current_time - timedelta(minutes=15), self.current_time + timedelta(hours=1))
        self.write(payload)

        with patch("alpha_outbox._maybe_deliver_intent", return_value="failed"):
            result = self.publisher().run_once()

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["published"], 0)


if __name__ == "__main__":
    unittest.main()
