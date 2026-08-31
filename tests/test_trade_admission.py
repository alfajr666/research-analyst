from datetime import datetime, timezone

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
