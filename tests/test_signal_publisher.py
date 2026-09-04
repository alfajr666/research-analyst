import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import sqlite3

import config
from alpha_outbox import dedupe_key
from discord_format import format_discord_signal
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
        "targets": [150.2, 151.0],
        "data_freshness_seconds": 1.0,
        "structural_context": {
            "asset": "SOL",
            "cutoff": observed_at.isoformat(),
            "zones": [{
                "zone_id": "zone-signal", "asset": "SOL", "type": "order_block", "timeframe": "4h",
                "direction": "bullish", "low": 144.0, "high": 144.2, "state": "active",
                "created_at": (observed_at - timedelta(hours=4)).isoformat(),
                "confirmed_at": (observed_at - timedelta(hours=4)).isoformat(),
                "coverage_status": "covered", "source_evidence_ids": ["bar-signal"],
            }],
            "atr_by_timeframe": {"4h": 1.0},
            "atr_source_bar_ids": {"4h": ["bar-signal"]},
        },
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
        self.previous_llm = config.LLM_RESEARCH_ENABLED
        self.previous_include = config.LLM_INCLUDE_IN_TELEGRAM
        self.previous_include_discord = config.LLM_INCLUDE_IN_DISCORD
        config.LLM_RESEARCH_ENABLED = False
        config.LLM_INCLUDE_IN_TELEGRAM = False
        config.LLM_INCLUDE_IN_DISCORD = False
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.outbox = self.root / "alpha_outbox"
        self.outbox.mkdir()
        self.db = self.root / "events.db"
        config.init_analyst_db(self.db)
        self.market_db = self.root / "market.sqlite3"
        config.init_market_db(self.market_db)
        self.current_time = datetime(2026, 8, 16, 10, 30, tzinfo=timezone.utc)

    def tearDown(self):
        config.LLM_RESEARCH_ENABLED = self.previous_llm
        config.LLM_INCLUDE_IN_TELEGRAM = self.previous_include
        config.LLM_INCLUDE_IN_DISCORD = self.previous_include_discord
        self.directory.cleanup()

    def write(self, payload):
        (self.outbox / f"{payload['dedupe_key']}.json").write_text(json.dumps(payload))

    def publisher(self, transport):
        return SignalPublisher(self.db, self.outbox, transport, now=lambda: self.current_time, market_db_path=self.market_db)

    def test_default_ledger_is_separate_from_market_data_database(self):
        self.assertNotEqual(Path(config.ANALYST_DB_PATH).resolve(), Path(config.MARKET_DB_PATH).resolve())
        self.assertEqual(SignalPublisher().db_path, config.ANALYST_DB_PATH)

    def test_publisher_uses_alpha_ledger_while_market_database_is_locked(self):
        market_db = self.root / "market.db"
        alpha_db = self.root / "alpha.db"
        payload = event(self.current_time - timedelta(minutes=15), self.current_time + timedelta(hours=1))
        self.write(payload)
        lock = subprocess.Popen(
            [sys.executable, "-c", (
                "import sqlite3, sys, time; "
                "connection = sqlite3.connect(sys.argv[1]); "
                "print('locked', flush=True); time.sleep(10)"
            ), str(market_db)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(lock.stdout.readline().strip(), "locked")
            with patch.object(config, "MARKET_DB_PATH", str(market_db)), patch.object(config, "ANALYST_DB_PATH", str(alpha_db)):
                self.assertEqual(
                    SignalPublisher(outbox_dir=self.outbox, transport=FakeTransport(), now=lambda: self.current_time).run_once(),
                    {"persisted": 1, "sent": 1, "failed": 0, "invalid": 0},
                )
        finally:
            lock.terminate()
            lock.wait()
            lock.stdout.close()

    def rows(self, query):
        connection = sqlite3.connect(str(self.db))
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
        self.assertEqual(self.rows("SELECT count(*) FROM research_requests"), [(0,)])
        self.assertEqual(self.rows("SELECT status, attempt_number FROM signal_deliveries"), [("sent", 1)])
        self.assertEqual(
            self.rows("SELECT confidence, observation_status, reason FROM alpha_confidence_observations"),
            [(0.67, "unavailable", "confidence_components_missing_or_invalid")],
        )

    def test_repairs_legacy_admission_only_target_before_delivery(self):
        payload = event(self.current_time - timedelta(minutes=15), self.current_time + timedelta(hours=1))
        payload.pop("targets")
        payload["_admission_result"] = {"selected_take_profit": 151.0}
        self.write(payload)

        result = self.publisher(FakeTransport()).run_once()

        self.assertEqual(result, {"persisted": 1, "sent": 1, "failed": 0, "invalid": 0})
        self.assertEqual(self.rows("SELECT targets FROM alpha_candidates"), [("[151.0]",)])

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

    def test_persisted_event_transitions_to_expired(self):
        payload = event(self.current_time - timedelta(minutes=15), self.current_time + timedelta(minutes=1))
        self.write(payload)
        publisher = self.publisher(FakeTransport())
        publisher.run_once()

        self.current_time += timedelta(minutes=2)
        publisher.run_once()

        self.assertEqual(self.rows("SELECT status FROM alpha_events"), [("expired",)])

    def test_format_contains_portable_signal_fields(self):
        payload = event(self.current_time, self.current_time + timedelta(hours=1))
        message = format_signal(payload)
        for value in ("Continuation", "SOL", "LONG", "Confirmed expansion", "breakout above", "142.7", "150.2", "151", "2026-08-16 11:30 UTC", "2026-08-16 10:30 UTC"):
            self.assertIn(value, message)
        self.assertNotIn("Confidence", message)

    def test_telegram_format_labels_dual_zone_correctly(self):
        payload = event(self.current_time, self.current_time + timedelta(hours=1))
        payload["setup_class"] = "dual_zone_follower"
        payload["strategy_id"] = "dual-zone-follower-v2"
        message = format_signal(payload)
        self.assertIn("Trend pullback", message)
        self.assertNotIn("Impulse ignition", message)

    def test_delivers_telegram_and_discord_independently(self):
        payload = event(self.current_time - timedelta(minutes=15), self.current_time + timedelta(hours=1))
        self.write(payload)
        telegram = FakeTransport()
        discord = FakeTransport()
        publisher = SignalPublisher(
            self.db,
            self.outbox,
            transports={"telegram": telegram, "discord": discord},
            now=lambda: self.current_time,
            market_db_path=self.market_db,
        )
        self.assertEqual(publisher.run_once(), {"persisted": 1, "sent": 2, "failed": 0, "invalid": 0})
        self.assertEqual(publisher.run_once(), {"persisted": 0, "sent": 0, "failed": 0, "invalid": 0})
        self.assertEqual(len(telegram.messages), 1)
        self.assertEqual(len(discord.messages), 1)
        self.assertIn("ALPHA SIGNAL", telegram.messages[0])
        self.assertIn("**ALPHA SIGNAL · LONG · SOL**", discord.messages[0])
        channels = sorted(row[0] for row in self.rows("SELECT channel FROM signal_deliveries"))
        self.assertEqual(channels, ["discord", "telegram"])

    def test_does_not_invoke_retired_filesystem_execution_adapter(self):
        payload = event(self.current_time - timedelta(minutes=15), self.current_time + timedelta(hours=1))
        self.write(payload)
        with patch("execution_adapter.ExecutionAdapter.deliver") as deliver:
            self.publisher(FakeTransport()).run_once()
        deliver.assert_not_called()

    def test_discord_failure_does_not_block_telegram(self):
        payload = event(self.current_time - timedelta(minutes=15), self.current_time + timedelta(hours=1))
        self.write(payload)
        telegram = FakeTransport()
        discord = FakeTransport(failures=1)
        publisher = SignalPublisher(
            self.db,
            self.outbox,
            transports={"telegram": telegram, "discord": discord},
            now=lambda: self.current_time,
            market_db_path=self.market_db,
        )
        result = publisher.run_once()
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(telegram.messages), 1)
        self.assertEqual(self.rows(
            "SELECT channel, status FROM signal_deliveries ORDER BY channel"
        ), [("discord", "failed"), ("telegram", "sent")])

    def test_discord_format_matches_style_a(self):
        payload = event(self.current_time, self.current_time + timedelta(hours=1))
        message = format_discord_signal(payload)
        self.assertIn("**ALPHA SIGNAL · LONG · SOL**", message)
        self.assertIn("Continuation", message)
        self.assertNotIn("Confidence", message)


if __name__ == "__main__":
    unittest.main()
