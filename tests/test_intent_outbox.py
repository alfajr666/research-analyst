import unittest
from datetime import datetime, timezone

import config
from intent_outbox import (
    build_executor_intent,
    to_ccxt_perp_symbol,
    validate_geometry,
    verify_intent_admission,
)
from trade_admission import admit
from trade_quality import score_candidate


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
    def test_deployment_defaults_select_bybit_hyro(self):
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

    def test_compact_fundamo_leg_uses_admission_route(self):
        event = _alpha_event(
            strategy_id="bb-rsi-meanrev-v1",
            asset="SOL",
        )
        intent = build_executor_intent(
            event,
            admission={"resolved_account": "fundamo"},
        )
        self.assertEqual((intent["exchange_id"], intent["account_id"]), ("bybit", "fundamo"))

    def test_new_portfolio_strategies_route_to_the_agreed_accounts(self):
        for strategy in ("ema9-adx-stochrsi-state-v1",):
            intent = build_executor_intent(_alpha_event(strategy_id=strategy), account_id="fundamo")
            self.assertEqual((intent["exchange_id"], intent["account_id"]), ("bybit", "hyro"))
        for strategy in (
            "dual-zone-follower-v3",
            "dual-zone-short-follower-v3",
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

    def test_accepts_below_minimum_rr_when_directional_geometry_is_valid(self):
        ok, reason = validate_geometry(
            build_executor_intent(_alpha_event(targets=[105]))
        )
        self.assertTrue(ok, reason)

    def test_accepts_tight_stop_when_directional_geometry_is_valid(self):
        ok, reason = validate_geometry(
            build_executor_intent(_alpha_event(invalidation_price=99.95))
        )
        self.assertTrue(ok, reason)

    def test_market_entry_skips_relative_geometry(self):
        ok, reason = validate_geometry(
            build_executor_intent(_alpha_event(order_type="market"))
        )
        self.assertTrue(ok, reason)


def test_15m_proof_is_accepted_only_while_the_feature_is_enabled(monkeypatch):
    observed = "2026-09-01T12:05:00Z"
    event = {
        "strategy_id": "impulse-ignition-v1",
        "candidate_id": "candidate-15m-proof",
        "asset": "BTC",
        "direction": "long",
        "entry_price": 104.0,
        "invalidation_price": 98.0,
        "targets": [120.0],
        "valid_until": "2099-01-01T00:05:00Z",
        "observed_at": observed,
        "data_freshness_seconds": 1.0,
        "structural_context": {
            "asset": "BTC",
            "cutoff": observed,
            "zones": [{
                "zone_id": "15m-zone", "asset": "BTC", "type": "fvg", "timeframe": "15m",
                "direction": "bullish", "low": 100.0, "high": 101.0, "state": "active",
                "created_at": "2026-09-01T12:00:00Z", "confirmed_at": "2026-09-01T12:00:00Z",
                "coverage_status": "covered", "source_evidence_ids": ["5m-1"],
                "source_mode": "market_5m_resampled", "source_exchange": "bybit",
                "resampling_contract_version": "execution-5m-to-15m-v1",
                "zone_detector_version": "structure-zones-v1",
            }],
            "atr_by_timeframe": {"4h": 10.0, "15m": 1.0},
            "atr_source_bar_ids": {"4h": ["4h-1"], "15m": ["5m-1", "5m-2"]},
            "frame_source_bar_ids": {"15m": ["5m-1", "5m-2"]},
        },
    }
    monkeypatch.setattr(config, "STRUCTURAL_15M_ZONES_ENABLED", True)
    admission = admit(event, now=datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc),
                      structural_context=event["structural_context"])
    assert admission["hard_gate"] == "pass"
    intent = build_executor_intent(event, admission=admission)
    monkeypatch.setattr("structural_stop.build_structural_contexts", lambda *_args, **_kwargs: {"BTC": event["structural_context"]})

    ok, reason = verify_intent_admission(intent)
    assert ok, reason

    intent["metadata"]["admission_result"]["structural_frame_bar_ids"] = ["changed"]
    ok, reason = verify_intent_admission(intent)
    assert not ok
    assert "structural_frame_bar_ids is inconsistent" in reason

    monkeypatch.setattr(config, "STRUCTURAL_15M_ZONES_ENABLED", False)
    ok, reason = verify_intent_admission(intent)
    assert not ok
    assert "15m structural admission is disabled" in reason


def test_score_eligible_schema_v2_handoff_does_not_require_structural_admission():
    event = _alpha_event(
        candidate_id="score-only-candidate",
        valid_until="2099-01-01T00:05:00Z",
        data_freshness_seconds=1.0,
    )
    admission = score_candidate(event, regime_mode="off")
    assert admission["hard_gate"] == "not_applicable"
    assert admission["score_decision"] == "eligible"

    intent = build_executor_intent(event, admission=admission)
    ok, reason = verify_intent_admission(intent)
    assert ok, reason


def test_score_eligible_compact_fundamo_handoff_preserves_account_fingerprint():
    event = _alpha_event(
        strategy_id="bb-rsi-meanrev-v1",
        asset="SOL",
        candidate_id="compact-fundamo-candidate",
        valid_until="2099-01-01T00:05:00Z",
        data_freshness_seconds=1.0,
        effective_universe_assets=["SOL"],
        effective_universe_version="feed-1",
        _execution_account="fundamo",
    )
    admission = score_candidate(event, regime_mode="off")
    assert admission["score_decision"] == "eligible"
    assert admission["resolved_account"] == "fundamo"

    intent = build_executor_intent(event, admission=admission)
    ok, reason = verify_intent_admission(intent)
    assert ok, reason


if __name__ == "__main__":
    unittest.main()
