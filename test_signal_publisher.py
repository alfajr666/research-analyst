import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import duckdb

import config
from alpha_outbox import dedupe_key
from signal_publisher import RETRY_BASE_SECONDS, SignalPublisher, format_signal


def event(observed_at, valid_until):
    payload = {
        "schema_version": 1,
        "alpha_id": "a-1",
        "strategy_id": "continuation-breakout-v1",
        "asset": "SOL",
        "direction": "long",
        "setup_class": "continuation_breakout",
        "phase": "confirmed_expansion",
        "observed_at": observed_at.isoformat(),
        "valid_until": valid_until.isoformat(),
        "horizon_minutes": 240,
        "confidence": 0.67,
        "entry_condition": {"type": "breakout_above", "price": 145.2},
        "invalidation_price": 142.7,
        "targets": [148.1, 151.0],
        "feature_snapshot": {"regime": "trending_up"},
    }
    payload["dedupe_key"] = dedupe_key(payload)
    return payload


class FakeTransport:
    def __init__(self, failures=0):
        self.failures = failures
        self.messages = []

    def send(self, text):
        self.messages.append(text)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary Telegram failure")
        return '{"ok":true}'


class SignalPublisherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.outbox = self.root / "alpha_outbox"
        self.outbox.mkdir()
        self.db = self.root / "events.db"
        self.current_time = datetime(2026, 8, 16, 10, 30, tzinfo=timezone.utc)

    def tearDown(self):
        self.directory.cleanup()

    def write(self, payload):
        (self.outbox / f"{payload['dedupe_key']}.json").write_text(json.dumps(payload))

    def publisher(self, transport):
        return SignalPublisher(self.db, self.outbox, transport, now=lambda: self.current_time)

    def test_default_ledger_is_separate_from_market_data_database(self):
        self.assertNotEqual(Path(config.ALPHA_DB_PATH).resolve(), Path(config.DB_PATH).resolve())
        self.assertEqual(SignalPublisher().db_path, config.ALPHA_DB_PATH)

    def test_publisher_uses_alpha_ledger_while_market_database_is_locked(self):
        market_db = self.root / "market.db"
        alpha_db = self.root / "alpha.db"
        payload = event(self.current_time - timedelta(minutes=15), self.current_time + timedelta(hours=1))
        self.write(payload)
        lock = subprocess.Popen(
            [sys.executable, "-c", (
                "import duckdb, sys, time; "
                "connection = duckdb.connect(sys.argv[1]); "
                "print('locked', flush=True); time.sleep(10)"
            ), str(market_db)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(lock.stdout.readline().strip(), "locked")
            with patch.object(config, "DB_PATH", str(market_db)), patch.object(config, "ALPHA_DB_PATH", str(alpha_db)):
                self.assertEqual(
                    SignalPublisher(outbox_dir=self.outbox, transport=FakeTransport(), now=lambda: self.current_time).run_once(),
                    {"persisted": 1, "sent": 1, "failed": 0, "invalid": 0},
                )
        finally:
            lock.terminate()
            lock.wait()
            lock.stdout.close()

    def rows(self, query):
        connection = duckdb.connect(str(self.db), read_only=True)
        try:
            return connection.execute(query).fetchall()
        finally:
            connection.close()

    def test_persists_once_and_deduplicates_delivery(self):
        payload = event(self.current_time - timedelta(minutes=15), self.current_time + timedelta(hours=1))
        self.write(payload)
        transport = FakeTransport()
        publisher = self.publisher(transport)

        self.assertEqual(publisher.run_once(), {"persisted": 1, "sent": 1, "failed": 0, "invalid": 0})
        self.assertEqual(publisher.run_once(), {"persisted": 0, "sent": 0, "failed": 0, "invalid": 0})
        self.assertEqual(len(transport.messages), 1)
        self.assertEqual(self.rows("SELECT count(*) FROM alpha_events"), [(1,)])
        self.assertEqual(self.rows("SELECT status, error_code FROM research_requests"), [("skipped", "disabled")])
        self.assertEqual(self.rows("SELECT status, attempt_number FROM signal_deliveries"), [("sent", 1)])
        self.assertEqual(
            self.rows("SELECT confidence, observation_status, reason FROM alpha_confidence_observations"),
            [(0.67, "unavailable", "confidence_components_missing_or_invalid")],
        )

    def test_expired_event_is_persisted_but_not_sent(self):
        payload = event(self.current_time - timedelta(hours=2), self.current_time - timedelta(minutes=1))
        self.write(payload)
        transport = FakeTransport()

        self.publisher(transport).run_once()

        self.assertEqual(transport.messages, [])
        self.assertEqual(self.rows("SELECT status FROM alpha_events"), [("expired",)])
        self.assertEqual(self.rows("SELECT count(*) FROM signal_deliveries"), [(0,)])

    def test_persists_confidence_component_audit_when_emitter_supplies_it(self):
        payload = event(self.current_time - timedelta(minutes=15), self.current_time + timedelta(hours=1))
        payload["feature_snapshot"]["confidence_components"] = {"volume": 0.35, "ema_proximity": 0.25}
        self.write(payload)

        self.publisher(FakeTransport()).run_once()

        confidence, components, status, reason = self.rows(
            "SELECT confidence, components_json, observation_status, reason FROM alpha_confidence_observations"
        )[0]
        self.assertEqual(confidence, 0.67)
        self.assertEqual(json.loads(components), {"ema_proximity": 0.25, "volume": 0.35})
        self.assertEqual((status, reason), ("observed", None))

    def test_failure_retries_without_duplicate_event(self):
        payload = event(self.current_time - timedelta(minutes=15), self.current_time + timedelta(hours=1))
        self.write(payload)
        transport = FakeTransport(failures=1)
        publisher = self.publisher(transport)

        self.assertEqual(publisher.run_once()["failed"], 1)
        self.current_time += timedelta(seconds=RETRY_BASE_SECONDS - 1)
        self.assertEqual(publisher.run_once()["sent"], 0)
        self.current_time += timedelta(seconds=1)
        self.assertEqual(publisher.run_once()["sent"], 1)
        self.assertEqual(self.rows("SELECT count(*) FROM alpha_events"), [(1,)])
        self.assertEqual(self.rows("SELECT attempt_number, status FROM signal_deliveries ORDER BY attempt_number"), [(1, "failed"), (2, "sent")])

    def test_llm_mode_waits_for_review_then_appends_it_to_delivery(self):
        payload = event(self.current_time - timedelta(minutes=15), self.current_time + timedelta(hours=1))
        self.write(payload)
        transport = FakeTransport()
        report = {
            "verdict": "neutral", "thesis_summary": "Local evidence is mixed.",
            "limitations": ["No external sources are included."],
        }
        with patch.object(config, "LLM_RESEARCH_ENABLED", True), \
             patch.object(config, "LLM_INCLUDE_IN_TELEGRAM", True), \
             patch("research_repository.ResearchCoordinator.process", return_value={}), \
             patch("research_repository.latest_event_report", return_value=report):
            self.assertEqual(self.publisher(transport).run_once(), {"persisted": 1, "sent": 0, "failed": 0, "invalid": 0})
            self.assertEqual(self.rows("SELECT status FROM research_requests"), [("pending",)])
            connection = duckdb.connect(str(self.db))
            try:
                connection.execute("UPDATE research_requests SET status = 'completed'")
            finally:
                connection.close()
            self.assertEqual(self.publisher(transport).run_once(), {"persisted": 0, "sent": 1, "failed": 0, "invalid": 0})
        self.assertIn("Research note (advisory)", transport.messages[0])

    def test_llm_failure_falls_back_to_deterministic_delivery(self):
        payload = event(self.current_time - timedelta(minutes=15), self.current_time + timedelta(hours=1))
        self.write(payload)
        transport = FakeTransport()
        with patch.object(config, "LLM_RESEARCH_ENABLED", True), \
             patch("research_repository.ResearchCoordinator.process", return_value={}):
            self.publisher(transport).run_once()
            connection = duckdb.connect(str(self.db))
            try:
                connection.execute("UPDATE research_requests SET status = 'failed'")
            finally:
                connection.close()
            self.assertEqual(self.publisher(transport).run_once()["sent"], 1)
        self.assertNotIn("Research note", transport.messages[0])

    def test_persisted_event_transitions_to_expired(self):
        payload = event(self.current_time - timedelta(minutes=15), self.current_time + timedelta(minutes=1))
        self.write(payload)
        publisher = self.publisher(FakeTransport())
        publisher.run_once()

        self.current_time += timedelta(minutes=2)
        publisher.run_once()

        self.assertEqual(self.rows("SELECT status FROM alpha_events"), [("expired",)])

    def test_expired_outcome_records_target_after_a_triggered_bar(self):
        payload = event(self.current_time - timedelta(minutes=30), self.current_time - timedelta(minutes=15))
        payload["feature_snapshot"] = {"source_symbol": "SOLUSDT_PERP.A"}
        payload["entry_condition"]["price"] = 100
        payload["invalidation_price"] = 90
        payload["dedupe_key"] = dedupe_key(payload)
        self.write(payload)
        publisher = self.publisher(FakeTransport())
        publisher.run_once()
        connection = duckdb.connect(str(self.db))
        try:
            connection.executemany("""
                INSERT INTO futures_data (timestamp, underlying, symbol, open, high, low, close, volume)
                VALUES (?, 'SOL', 'SOLUSDT_PERP.A', 100, ?, ?, ?, 100)
            """, [
                (self.current_time - timedelta(minutes=30), 101, 99, 100),
                (self.current_time - timedelta(minutes=15), 149, 101, 148),
            ])
        finally:
            connection.close()
        publisher.run_once()
        self.assertEqual(self.rows("SELECT outcome FROM alpha_outcomes"), [("target",)])

    def test_same_bar_barrier_crossing_is_not_reported_as_trade_quality(self):
        payload = event(self.current_time - timedelta(minutes=30), self.current_time - timedelta(minutes=15))
        payload["feature_snapshot"] = {"source_symbol": "SOLUSDT_PERP.A"}
        self.write(payload)
        publisher = self.publisher(FakeTransport())
        publisher.run_once()
        connection = duckdb.connect(str(self.db))
        try:
            connection.execute("""
                INSERT INTO futures_data (timestamp, underlying, symbol, open, high, low, close, volume)
                VALUES (?, 'SOL', 'SOLUSDT_PERP.A', 100, 149, 99, 145, 100)
            """, (self.current_time - timedelta(minutes=30),))
        finally:
            connection.close()
        publisher.run_once()
        self.assertEqual(self.rows("SELECT outcome FROM alpha_outcomes"), [("ambiguous_same_bar",)])

    def test_format_contains_portable_signal_fields(self):
        payload = event(self.current_time, self.current_time + timedelta(hours=1))
        message = format_signal(payload)
        for value in ("Continuation", "SOL", "LONG", "confirmed_expansion", "67%", "breakout above", "142.7", "148.1, 151", "2026-08-16 11:30 UTC", "2026-08-16 10:30 UTC"):
            self.assertIn(value, message)


if __name__ == "__main__":
    unittest.main()
