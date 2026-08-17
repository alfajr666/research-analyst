import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from execution_adapter import ExecutionAdapter


def event(direction="long"):
    entry, stop, target = (100, 95, 110) if direction == "long" else (100, 105, 90)
    return {
        "schema_version": 1,
        "alpha_id": f"alpha-{direction}",
        "strategy_id": "accumulation-base-v1",
        "asset": "SOL",
        "direction": direction,
        "setup_class": "accumulation_base",
        "phase": "confirmed_pullback",
        "status": "active",
        "observed_at": "2026-08-17T06:00:00Z",
        "valid_until": "2026-08-17T10:00:00Z",
        "confidence": 0.8,
        "entry_condition": {"type": "limit_at_ema_context", "price": entry},
        "invalidation_price": stop,
        "targets": [target],
    }


class ExecutionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "events.db"
        config.init_db(self.db_path)
        self.connection = config.get_db_connection(db_path=self.db_path)
        self.now = datetime(2026, 8, 17, 8, tzinfo=timezone.utc)
        self.outbox = Path(self.directory.name) / "execution_outbox"
        self.targets = {"bybit-test": {"enabled": True, "asset_allowlist": frozenset({"SOL"})}}

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def persist(self, payload):
        self.connection.execute("""
            INSERT INTO alpha_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            payload["alpha_id"], payload["alpha_id"], payload["strategy_id"], payload["asset"],
            payload["direction"], payload["setup_class"], payload["phase"], payload["status"],
            payload["observed_at"], payload["valid_until"], json.dumps(payload), self.now,
        ))

    def adapter(self):
        return ExecutionAdapter(self.outbox, self.targets, now=lambda: self.now)

    def test_valid_long_and_short_write_deterministic_inbox_items_once(self):
        self.persist(event("long"))
        self.persist(event("short"))

        self.assertEqual(self.adapter().deliver(self.connection), {"written": 2, "acknowledged": 0, "skipped": 0, "failed": 0})
        self.assertEqual(self.adapter().deliver(self.connection), {"written": 0, "acknowledged": 0, "skipped": 0, "failed": 0})
        payload = json.loads((self.outbox / "bybit-test" / "alpha-long.json").read_text())
        self.assertEqual(payload["delivery_id"], "alpha-long:bybit-test")
        self.assertEqual(payload["symbol"], "SOL/USDT:USDT")
        self.assertEqual(payload["direction"], "LONG")
        self.assertEqual(payload["strategy_id"], "RS-accumulation-base-v1")
        self.assertEqual(payload["take_profit_mode"], "fixed_full_close")
        self.assertEqual(list(self.outbox.rglob(".execution-*.tmp")), [])

    def test_invalid_events_record_distinct_terminal_skip_reasons(self):
        invalid_geometry = event()
        invalid_geometry["alpha_id"] = "bad-geometry"
        invalid_geometry["targets"] = [90]
        expired = event()
        expired["alpha_id"] = "expired"
        expired["valid_until"] = (self.now - timedelta(seconds=1)).isoformat()
        multi_target = event()
        multi_target["alpha_id"] = "multi-target"
        multi_target["targets"] = [110, 120]
        inactive = event()
        inactive["alpha_id"] = "inactive"
        inactive["status"] = "invalidated"
        unsupported_entry = event()
        unsupported_entry["alpha_id"] = "unsupported-entry"
        unsupported_entry["entry_condition"]["type"] = "breakout_above"
        for payload in (invalid_geometry, expired, multi_target, inactive, unsupported_entry):
            self.persist(payload)

        self.assertEqual(self.adapter().deliver(self.connection), {"written": 0, "acknowledged": 0, "skipped": 5, "failed": 0})
        self.assertEqual(self.connection.execute("SELECT alpha_id, reason FROM execution_deliveries ORDER BY alpha_id").fetchall(), [
            ("bad-geometry", "invalid_directional_geometry"),
            ("expired", "expired"),
            ("inactive", "inactive"),
            ("multi-target", "multi_target"),
            ("unsupported-entry", "unsupported_entry_condition"),
        ])

    def test_static_allowlist_rejection_is_terminal(self):
        payload = event()
        payload["asset"] = "DOGE"
        self.persist(payload)

        self.assertEqual(self.adapter().deliver(self.connection), {"written": 0, "acknowledged": 0, "skipped": 1, "failed": 0})
        self.assertEqual(self.connection.execute("SELECT status, reason FROM execution_deliveries").fetchone(), ("skipped", "unsupported_symbol"))

    def test_receipt_acknowledges_written_delivery(self):
        self.persist(event())
        self.adapter().deliver(self.connection)
        receipt_directory = self.outbox / "bybit-test" / "receipts"
        receipt_directory.mkdir()
        (receipt_directory / "alpha-long.json").write_text(json.dumps({
            "target": "bybit-test", "delivery_id": "alpha-long:bybit-test", "alpha_id": "alpha-long",
            "status": "accepted_pending_fill", "bot_trade_id": "trade-1", "bot_order_id": "order-1",
        }))

        self.assertEqual(self.adapter().deliver(self.connection)["acknowledged"], 1)
        self.assertEqual(self.connection.execute("SELECT status, bot_trade_id, bot_order_id FROM execution_deliveries").fetchone(), ("acknowledged", "trade-1", "order-1"))

    def test_terminal_failed_receipt_is_not_rewritten(self):
        self.persist(event())
        self.adapter().deliver(self.connection)
        receipt_directory = self.outbox / "bybit-test" / "receipts"
        receipt_directory.mkdir()
        (receipt_directory / "alpha-long.json").write_text(json.dumps({
            "target": "bybit-test", "delivery_id": "alpha-long:bybit-test", "alpha_id": "alpha-long",
            "status": "failed_order_submission",
        }))

        self.adapter().deliver(self.connection)
        self.assertEqual(self.adapter().deliver(self.connection)["written"], 0)
        self.assertEqual(self.connection.execute("SELECT status FROM execution_deliveries").fetchone(), ("failed",))

    def test_propr_requires_a_tradeable_assets_snapshot(self):
        self.persist(event())
        targets = {"propr": {"enabled": True, "tradeable_assets_path": Path(self.directory.name) / "missing.json"}}

        self.assertEqual(ExecutionAdapter(self.outbox, targets, now=lambda: self.now).deliver(self.connection)["skipped"], 1)
        self.assertEqual(self.connection.execute("SELECT reason FROM execution_deliveries").fetchone(), ("unsupported_symbol",))


if __name__ == "__main__":
    unittest.main()
