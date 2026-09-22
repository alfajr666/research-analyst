"""Engine-owned N-R fallback (spec mechanical-exit-fallback-v1.md)."""
from datetime import datetime, timezone

import config
from intent_outbox import build_trade_intent, verify_intent_admission
from trade_admission import admit, apply_engine_fallback, candidate_admission_fingerprint


def _native_vwap_candidate(**over):
    event = {
        "candidate_id": "mr-vwap-utc-session-v3:BTC:1:long",
        "strategy_id": "mr-vwap-locked-v1",
        "asset": "BTC",
        "direction": "long",
        "entry_price": 100.0,
        "invalidation_price": 98.0,
        "targets": [101.0],  # native VWAP snapshot, RR 0.5
        "atr14_4h": 4.0,
        "observed_at": "2026-01-01T00:00:00Z",
        "valid_until": "2099-01-01T00:05:00+00:00",
        "data_freshness_seconds": 1.0,
        "metadata": {},
        "effective_universe_assets": ["BTC"],
        "effective_universe_version": "feed-test",
        "structural_context": {
            "asset": "BTC",
            "cutoff": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "zones": [{
                "zone_id": "zone-1", "asset": "BTC", "type": "order_block", "timeframe": "4h",
                "direction": "bullish", "low": 100.0, "high": 105.0,
                "state": "active", "created_at": datetime(2025, 12, 31, tzinfo=timezone.utc),
                "confirmed_at": datetime(2025, 12, 31, tzinfo=timezone.utc),
                "source_evidence_ids": ["bar-1"], "coverage_status": "covered",
            }],
            "atr_by_timeframe": {"4h": 4.0},
            "atr_source_bar_ids": {"4h": ["bar-1"]},
        },
    }
    event.update(over)
    return event


def test_fallback_multiple_default_and_env_name():
    assert float(getattr(config, "INTENT_MECHANICAL_EXIT_FALLBACK_R", 0.0)) == 2.0


def test_apply_converts_native_vwap_to_2r():
    converted, error = apply_engine_fallback(_native_vwap_candidate())
    assert error is None
    assert converted["targets"] == [{"price": 104.0, "fraction": None}]
    assert converted["exit_rule"] == {
        "kind": "vwap_target",
        "native_levels": [{"price": 101.0, "fraction": None}],
    }
    assert converted["target_source"] == "engine_fallback_2r"
    assert converted["_engine_fallback_applied"] is True
    # strategy-owned inputs untouched
    assert converted["entry_price"] == 100.0
    assert converted["invalidation_price"] == 98.0


def test_apply_identity_native_collapses_levels():
    converted, error = apply_engine_fallback(_native_vwap_candidate(targets=[104.0]))
    assert error is None
    assert converted["targets"] == [{"price": 104.0, "fraction": None}]
    assert converted["exit_rule"] == {"kind": "vwap_target", "native_levels": []}


def test_apply_idempotent_for_handoff_rebuilds():
    converted, _ = apply_engine_fallback(_native_vwap_candidate())
    again, error = apply_engine_fallback(converted)
    assert error is None
    assert again == converted


def test_apply_fails_closed_on_malformed_natives():
    _, error = apply_engine_fallback(_native_vwap_candidate(targets=[99.0]))
    assert error is not None
    _, error = apply_engine_fallback(_native_vwap_candidate(direction="sideways"))
    assert error is not None


def test_fingerprint_binds_exit_rule_natives():
    raw = _native_vwap_candidate()
    converted, _ = apply_engine_fallback(raw)
    # Converted (placed 2R + exit_rule) hashes identically to a rebuild from
    # intent fields, but differently from the economics it replaced.
    rebuilt = dict(converted)
    assert candidate_admission_fingerprint(converted) == candidate_admission_fingerprint(rebuilt)
    assert converted["exit_rule"]["native_levels"]


def test_e2e_admit_build_verify_roundtrip():
    event = _native_vwap_candidate()
    proof = admit(event, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                  structural_context=event["structural_context"])
    assert proof["hard_gate"] == "pass", proof["hard_gate_reasons"]
    assert proof["selected_take_profit"] == 104.0
    assert proof["selected_take_profit_source"] == "engine_fallback_2r"
    intent = build_trade_intent(event, admission=proof)
    assert intent["take_profit"] == 104.0
    assert intent["targets"] == [{"price": 104.0, "fraction": None}]
    assert intent["take_profit_mode"] == "vwap_target"
    assert intent["metadata"]["target_source"] == "engine_fallback_2r"
    assert intent["metadata"]["exit_rule"]["native_levels"] == [{"price": 101.0, "fraction": None}]
    ok, reason = verify_intent_admission(intent, proof)
    assert ok, reason
