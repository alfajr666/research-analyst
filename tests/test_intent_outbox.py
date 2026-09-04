import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from intent_outbox import (
    build_executor_intent,
    to_ccxt_perp_symbol,
    validate_geometry,
    write_intent,
)
from trade_admission import candidate_admission_fingerprint
from alpha_outbox import write_event


def _alpha_event(**over):
    ev = {
        "schema_version": 1,
        "strategy_id": "impulse-ignition-v1",
        "asset": "BTC",
        "direction": "long",
        "observed_at": "2026-08-28T12:00:00+00:00",
        "entry_condition": {"type": "limit", "price": 100},
        "invalidation_price": 95,
        "targets": [110],
        "atr14_4h": 10,
        "alpha_id": "deliv-1",
    }
    ev.update(over)
    return ev


class IntentBuildTests(unittest.TestCase):
    def test_deployment_defaults_enable_pm_sidecar(self):
        self.assertTrue(config.PM_SIDECAR_ENABLED)
        self.assertEqual(config.INTENT_EXCHANGE_ID, "bybit")
        self.assertEqual(config.INTENT_ACCOUNT_ID, "hyro")

    def test_maps_internal_event_to_executor_envelope(self):
        intent = build_executor_intent(_alpha_event())
        self.assertEqual(intent["schema_version"], 1)
        self.assertEqual(intent["delivery_id"], "deliv-1")
        self.assertEqual(intent["source"], "research-analyst")
        self.assertEqual(intent["exchange_id"], "bybit")
        self.assertEqual(intent["account_id"], "hyro")
        self.assertEqual(intent["asset"], "BTC")
        self.assertEqual(intent["symbol"], "BTC/USDT:USDT")
        self.assertEqual(intent["direction"], "LONG")
        self.assertEqual(intent["entry_price"], 100)
        self.assertEqual(intent["stop_loss"], 95)
        self.assertEqual(intent["take_profit"], 110)
        self.assertEqual(intent["take_profit_mode"], "fixed_full_close")
        # Analyst never sizes: intent carries no quantity/risk_amount.
        self.assertNotIn("quantity", intent["metadata"])
        self.assertNotIn("risk_amount", intent["metadata"])
        self.assertEqual(intent["metadata"]["strategy_id"], "impulse-ignition-v1")
        self.assertEqual(intent["metadata"]["target_source"], "strategy_target")

    def test_derives_two_r_target_when_strategy_target_is_missing(self):
        intent = build_executor_intent(_alpha_event(targets=[]))
        self.assertEqual(intent["take_profit"], 110)
        self.assertEqual(intent["metadata"]["target_source"], "producer_derived_2r")

    def test_derives_two_r_target_for_short(self):
        intent = build_executor_intent(_alpha_event(
            direction="short", invalidation_price=105, targets=[]))
        self.assertEqual(intent["take_profit"], 90)

    def test_order_type_is_executor_owned(self):
        intent = build_executor_intent(_alpha_event(direction="SHORT", order_type="market"))
        self.assertEqual(intent["direction"], "SHORT")
        self.assertEqual(intent["entry_price"], 100)
        self.assertNotIn("order_type", intent)

    def test_never_carries_risk_or_sizing(self):
        # Even if a strategy attaches sizing hints, the executor-owned sizing must
        # not leak into the intent.
        intent = build_executor_intent(_alpha_event(
            metadata={"quantity": 3, "amount": 3, "risk_amount": 50, "strategy_id": "impulse-ignition-v1"}
        ))
        self.assertNotIn("quantity", intent["metadata"])
        self.assertNotIn("amount", intent["metadata"])
        self.assertNotIn("risk_amount", intent["metadata"])
        # non-sizing metadata still passes through
        self.assertEqual(intent["metadata"].get("strategy_id"), "impulse-ignition-v1")

    def test_compact_strategies_are_forced_to_hyro(self):
        config.INTENT_ROUTING = {
            strategy: {"exchange_id": "binance", "account_id": "stale-account"}
            for strategy in config.COMPACT_STRATEGY_IDS
        }
        try:
            for strategy in config.COMPACT_STRATEGY_IDS:
                intent = build_executor_intent(_alpha_event(strategy_id=strategy, alpha_id=None))
                self.assertEqual((intent["exchange_id"], intent["account_id"]), ("bybit", "hyro"))
        finally:
            config.INTENT_ROUTING = {}

    def test_new_portfolio_strategies_route_to_the_agreed_accounts(self):
        for strategy in ("ema9-adx-stochrsi-state-v1",):
            intent = build_executor_intent(_alpha_event(strategy_id=strategy), account_id="fundamo")
            self.assertEqual((intent["exchange_id"], intent["account_id"]), ("bybit", "hyro"))
        for strategy in (
            "ema99-double-touch-stochrsi-state-v1",
            "ema7-26-cross-hammer-shooting-star-1h-adx-v1",
        ):
            intent = build_executor_intent(_alpha_event(strategy_id=strategy), account_id="hyro")
            self.assertEqual((intent["exchange_id"], intent["account_id"]), ("bybit", "fundamo"))

    def test_swapped_strategy_families_are_routed_to_their_accounts(self):
        for strategy in ("ema9-adx-stochrsi-state-v1",):
            intent = build_executor_intent(_alpha_event(strategy_id=strategy), account_id="fundamo")
            self.assertEqual((intent["exchange_id"], intent["account_id"]), ("bybit", "hyro"))
        for strategy in (
            "ema99-double-touch-stochrsi-state-v1",
            "ema7-26-cross-hammer-shooting-star-1h-adx-v1",
        ):
            intent = build_executor_intent(_alpha_event(strategy_id=strategy), account_id="hyro")
            self.assertEqual((intent["exchange_id"], intent["account_id"]), ("bybit", "fundamo"))

    def test_symbol_helper(self):
        self.assertEqual(to_ccxt_perp_symbol("eth"), "ETH/USDT:USDT")

    def test_validity_falls_back_to_window(self):
        intent = build_executor_intent(_alpha_event())
        # 2026-08-28T12:05:00Z given INTENT_VALIDITY_MINUTES=5 default
        self.assertEqual(intent["entry_valid_until"], "2026-08-28T12:05:00Z")


class IntentGeometryTests(unittest.TestCase):
    def test_accepts_valid_long(self):
        ok, reason = validate_geometry(build_executor_intent(_alpha_event()))
        self.assertTrue(ok, reason)

    def test_rejects_bad_long_geometry(self):
        ok, reason = validate_geometry(
            build_executor_intent(_alpha_event(invalidation_price=110))
        )
        self.assertFalse(ok)
        self.assertIn("LONG", reason)

    def test_accepts_producer_derived_target(self):
        ok, _ = validate_geometry(build_executor_intent(_alpha_event(targets=[])))
        self.assertTrue(ok)

    def test_rejects_below_minimum_rr(self):
        ok, reason = validate_geometry(
            build_executor_intent(_alpha_event(targets=[105]))
        )
        self.assertFalse(ok)
        self.assertIn("reward/risk", reason)

    def test_rejects_too_tight_stop(self):
        ok, reason = validate_geometry(
            build_executor_intent(_alpha_event(invalidation_price=99.95))
        )
        self.assertFalse(ok)
        self.assertIn("stop distance", reason)

    def test_market_entry_skips_relative_geometry(self):
        ok, reason = validate_geometry(
            build_executor_intent(_alpha_event(order_type="market"))
        )
        self.assertTrue(ok, reason)


class IntentWriteTests(unittest.TestCase):
    def test_atomic_write_and_dedupe(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory)
            candidate = _alpha_event(
                observed_at=datetime.now(timezone.utc).isoformat(),
                valid_until=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            )
            observed = candidate["observed_at"]
            candidate["structural_context"] = {
                "asset": "BTC", "cutoff": observed,
                "zones": [{"zone_id": "zone-1", "asset": "BTC", "type": "order_block", "timeframe": "4h",
                            "direction": "bullish", "low": 97.0, "high": 98.0, "state": "active",
                            "created_at": observed, "confirmed_at": observed, "coverage_status": "covered",
                            "source_evidence_ids": ["zone-bar-1"]}],
                "atr_by_timeframe": {"4h": 1.0}, "atr_source_bar_ids": {"4h": ["bar-1"]},
            }
            admission = {
                "hard_gate": "pass", "structural_stop_gate": "pass", "selected_zone_id": "zone-1",
                "selected_zone_asset": "BTC", "selected_zone_kind": "order_block", "selected_zone_state": "active",
                "selected_zone_created_at": observed, "selected_zone_confirmed_at": observed,
                "selected_zone_coverage_status": "covered", "selected_zone_source_evidence_ids": ["zone-bar-1"],
                "selected_zone_timeframe": "4h", "structural_atr_method": "wilder",
                "structural_atr_period": 14, "structural_atr_source_bar_ids": ["bar-1"],
                "structural_atr": 1.0, "selected_zone_low": 97.0, "selected_zone_high": 98.0,
                "selected_zone_boundary": 97.0,
                "entry_zone_buffer": 2.0, "entry_zone_buffer_atr": 2.0,
                "structural_stop_buffer": 2.0, "structural_stop_buffer_atr": 2.0,
                "atr14_4h": 10.0,
                "data_freshness_seconds": 1.0,
                "structural_context_cutoff": observed,
                "candidate_id": "deliv-1",
            }
            admission["candidate_fingerprint"] = candidate_admission_fingerprint({
                "candidate_id": "deliv-1", "strategy_id": candidate["strategy_id"],
                "asset": candidate["asset"], "direction": candidate["direction"],
                "entry_price": candidate["entry_condition"]["price"],
                "invalidation_price": candidate["invalidation_price"],
                "take_profit": candidate["targets"][0], "observed_at": candidate["observed_at"],
                "valid_until": candidate["valid_until"],
            })
            intent = build_executor_intent(candidate, admission=admission)
            created, path = write_intent(intent, inbox, admission=admission)
            again, again_path = write_intent(intent, inbox, admission=admission)
            self.assertTrue(created)
            self.assertFalse(again)
            self.assertEqual(path, again_path)
            self.assertEqual(path.name, "deliv-1.json")
            self.assertEqual(list(inbox.glob(".intent-*.tmp")), [])


class IntentDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.prev_enabled = config.INTENT_DELIVERY_ENABLED
        self.prev_inbox = config.INTENT_INBOX
        self.prev_legacy = config.INTENT_BUS_LEGACY_INBOX_ENABLED
        self.dirname = tempfile.TemporaryDirectory()
        config.INTENT_DELIVERY_ENABLED = True
        config.INTENT_INBOX = Path(self.dirname.name)
        config.INTENT_BUS_LEGACY_INBOX_ENABLED = True

    def tearDown(self):
        config.INTENT_DELIVERY_ENABLED = self.prev_enabled
        config.INTENT_INBOX = self.prev_inbox
        config.INTENT_BUS_LEGACY_INBOX_ENABLED = self.prev_legacy
        self.dirname.cleanup()

    def test_write_event_delivers_intent_when_enabled(self):
        outbox = Path(self.dirname.name) / "alpha"
        created, _ = write_event(_alpha_event(
            valid_until="2099-01-01T00:05:00+00:00",
            data_freshness_seconds=1.0,
            structural_context={
                "asset": "BTC",
                "cutoff": "2026-08-28T12:00:00+00:00",
                "zones": [{
                    "zone_id": "zone-1", "asset": "BTC", "type": "order_block", "timeframe": "4h",
                    "direction": "bullish", "low": 97.0, "high": 98.0,
                    "state": "active", "created_at": "2026-08-28T08:00:00+00:00",
                    "confirmed_at": "2026-08-28T08:00:00+00:00",
                    "coverage_status": "covered", "source_evidence_ids": ["bar-1"],
                }],
                "atr_by_timeframe": {"4h": 1.0},
                "atr_source_bar_ids": {"4h": ["bar-1"]},
            },
            _admission_result={"structural_stop_gate": "pass", "selected_zone_id": "stale-zone"},
        ), outbox)
        self.assertTrue(created)
        intents = list(config.INTENT_INBOX.glob("*.json"))
        self.assertEqual(len(intents), 1)
        with intents[0].open() as fh:
            written = json.load(fh)
        self.assertEqual(written["schema_version"], 1)
        self.assertEqual(written["direction"], "LONG")
        # delivery_id is the event's regenerated alpha_id
        alpha_file = list(outbox.glob("*.json"))[0]
        with alpha_file.open() as fh:
            alpha = json.load(fh)
        self.assertEqual(written["delivery_id"], alpha["alpha_id"])
        self.assertEqual(alpha["_admission_result"]["selected_zone_id"], "zone-1")

    def test_incomplete_advisory_event_cannot_deliver_intent(self):
        outbox = Path(self.dirname.name) / "alpha-incomplete"
        created, _ = write_event(_alpha_event(), outbox)
        self.assertTrue(created)
        self.assertEqual(list(config.INTENT_INBOX.glob("*.json")), [])

    def test_direct_intent_write_requires_admission_proof(self):
        outbox = Path(self.dirname.name) / "alpha-proof"
        intent = build_executor_intent(_alpha_event())

        created, path = write_intent(intent, outbox)

        self.assertFalse(created)
        self.assertEqual(path.name, "blocked.json")


if __name__ == "__main__":
    unittest.main()
