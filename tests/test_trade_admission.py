from datetime import datetime, timezone

import config
from trade_admission import admit


def _candidate(stop, atr=10):
    return {
        "candidate_id": "candidate-1", "asset": "BTC", "direction": "long",
        "entry_price": 100.0, "invalidation_price": stop, "targets": [120.0],
        "atr14_4h": atr, "valid_until": "2099-01-01T00:05:00+00:00",
        "data_freshness_seconds": 1.0,
    }


def test_stop_at_configured_atr_floor_passes():
    result = admit(_candidate(97.5), now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert result["hard_gate"] == "pass"
    assert result["stop_atr_multiple"] == 0.25


def test_stop_below_atr_floor_fails():
    result = admit(_candidate(99.0), now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert result["hard_gate"] == "fail"
    assert "stop distance below ATR-based minimum" in result["hard_gate_reasons"]


def test_missing_atr_fails_closed():
    event = _candidate(95.0)
    event.pop("atr14_4h")
    result = admit(event, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert result["hard_gate"] == "fail"
    assert "4h ATR14 is unavailable or invalid" in result["hard_gate_reasons"]


def test_structural_stop_is_an_independent_opt_in_gate(monkeypatch):
    event = _candidate(96.0, atr=10)
    event.update({
        "strategy_id": "failed-break-v3",
        "observed_at": "2026-01-01T00:00:00+00:00",
        "structural_reference": {
            "kind": "swing_low",
            "timeframe": "4h",
            "asset": "BTC",
            "reference_id": "pivot-1",
            "boundary_price": 96.5,
            "formed_at": "2025-12-31T20:00:00+00:00",
            "confirmed_at": "2025-12-31T21:00:00+00:00",
            "cutoff_at": "2026-01-01T00:00:00+00:00",
            "coverage_status": "covered",
            "source_evidence_ids": ["bar-1"],
        },
    })
    monkeypatch.setattr(config, "STRUCTURAL_STOP_ADMISSION_ENABLED", True)
    monkeypatch.setattr(config, "STRUCTURAL_STOP_REQUIRED_STRATEGIES", {"failed-break-v3"})
    monkeypatch.setattr(config, "STRUCTURAL_STOP_MAX_REFERENCE_GAP_PCT", 0.02)

    result = admit(event, now=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert result["hard_gate"] == "pass"
    assert result["structural_stop_gate"] == "pass"


def test_structural_stop_failure_does_not_get_scored(monkeypatch):
    event = _candidate(96.0, atr=10)
    event.update({"strategy_id": "failed-break-v3"})
    monkeypatch.setattr(config, "STRUCTURAL_STOP_ADMISSION_ENABLED", True)
    monkeypatch.setattr(config, "STRUCTURAL_STOP_REQUIRED_STRATEGIES", {"failed-break-v3"})
    monkeypatch.setattr(config, "STRUCTURAL_STOP_MAX_REFERENCE_GAP_PCT", 0.02)

    from trade_admission import resolve

    result = resolve([event])

    assert result["results"][0]["hard_gate"] == "fail"
    assert result["results"][0]["score_status"] == "not_evaluated"
