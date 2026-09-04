from datetime import datetime, timezone

from structural_stop import validate_structural_stop


NOW = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)


def _candidate(direction, stop, boundary):
    return {
        "asset": "BTC",
        "direction": direction,
        "entry_price": 100.0,
        "invalidation_price": stop,
        "observed_at": NOW.isoformat(),
        "structural_reference": {
            "kind": "swing_low" if direction == "long" else "swing_high",
            "timeframe": "4h",
            "asset": "BTC",
            "reference_id": "structure-1",
            "boundary_price": boundary,
            "formed_at": "2026-09-01T08:00:00+00:00",
            "confirmed_at": "2026-09-01T12:00:00+00:00",
            "cutoff_at": NOW.isoformat(),
            "coverage_status": "covered",
            "source_evidence_ids": ["obs-1"],
        },
    }


def test_long_stop_must_clear_lower_reference():
    result = validate_structural_stop(
        _candidate("long", 94.0, 95.0),
        now=NOW,
        max_reference_gap_pct=0.02,
    )

    assert result["structural_stop_gate"] == "pass"
    assert result["stop_buffer"] == 1.0


def test_short_stop_must_clear_upper_reference():
    result = validate_structural_stop(
        _candidate("short", 106.0, 105.0),
        now=NOW,
        max_reference_gap_pct=0.02,
    )

    assert result["structural_stop_gate"] == "pass"
    assert result["stop_buffer"] == 1.0


def test_wrong_side_stop_fails_without_mutating_candidate():
    candidate = _candidate("long", 96.0, 95.0)
    original = candidate["invalidation_price"]

    result = validate_structural_stop(candidate, now=NOW, max_reference_gap_pct=0.02)

    assert result["structural_stop_gate"] == "fail"
    assert "stop does not clear lower structural boundary" in result["structural_stop_reasons"]
    assert candidate["invalidation_price"] == original


def test_uncovered_reference_fails_closed():
    candidate = _candidate("short", 106.0, 105.0)
    candidate["structural_reference"]["coverage_status"] = "incomplete"

    result = validate_structural_stop(candidate, now=NOW, max_reference_gap_pct=0.02)

    assert result["structural_stop_gate"] == "fail"
    assert "structural reference coverage is not complete" in result["structural_stop_reasons"]


def test_reference_gap_policy_is_required_for_enforcement():
    result = validate_structural_stop(_candidate("long", 94.0, 95.0), now=NOW)

    assert result["structural_stop_gate"] == "unavailable"
    assert "structural stop distance policy is unavailable" in result["structural_stop_reasons"]
