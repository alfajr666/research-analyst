from datetime import datetime, timezone

import config
from structural_stop import admit_selected_structural_stop, select_structural_zone


NOW = datetime(2026, 9, 4, 12, 5, tzinfo=timezone.utc)


def _zone(zone_id, timeframe, created_at, *, direction="bullish", low=100.0, high=101.0, state="active"):
    return {
        "zone_id": zone_id,
        "asset": "BTC",
        "type": "order_block",
        "timeframe": timeframe,
        "direction": direction,
        "low": low,
        "high": high,
        "state": state,
        "created_at": created_at,
        "confirmed_at": created_at,
        "coverage_status": "covered",
        "source_evidence_ids": [f"evidence-{zone_id}"],
    }


def _candidate(stop, direction="long", entry=104.0):
    return {
        "asset": "BTC",
        "direction": direction,
        "entry_price": entry,
        "invalidation_price": stop,
    }


def _context(zones, atr_4h=2.0, atr_1h=1.0):
    return {
        "asset": "BTC",
        "cutoff": NOW,
        "zones": zones,
        "atr_by_timeframe": {"4h": atr_4h, "1h": atr_1h},
        "atr_source_bar_ids": {"4h": ["4h-1"], "1h": ["1h-1"]},
    }


def test_four_hour_zone_has_priority_over_newer_one_hour_zone():
    selected = select_structural_zone(
        [
            _zone("old-4h", "4h", datetime(2026, 9, 4, 8, tzinfo=timezone.utc)),
            _zone("new-1h", "1h", datetime(2026, 9, 4, 12, tzinfo=timezone.utc)),
        ],
        asset="BTC", direction="long", entry=100.0, cutoff=NOW,
    )

    assert selected["zone_id"] == "old-4h"


def test_most_recent_eligible_zone_wins_within_timeframe():
    selected = select_structural_zone(
        [
            _zone("old", "4h", datetime(2026, 9, 4, 4, tzinfo=timezone.utc)),
            _zone("new", "4h", datetime(2026, 9, 4, 8, tzinfo=timezone.utc)),
            _zone("future", "4h", datetime(2026, 9, 4, 13, tzinfo=timezone.utc)),
        ],
        asset="BTC", direction="long", entry=100.0, cutoff=NOW,
    )

    assert selected["zone_id"] == "new"


def test_missing_zone_rejects_structural_admission():
    result = admit_selected_structural_stop(_candidate(98.0), _context([]))

    assert result["structural_stop_gate"] == "fail"
    assert result["structural_stop_reasons"] == ["no eligible HTF structural zone"]


def test_structural_buffer_boundaries_are_inclusive():
    context = _context([_zone("zone-1", "4h", datetime(2026, 9, 4, 8, tzinfo=timezone.utc))])

    minimum = admit_selected_structural_stop(_candidate(99.0), context)
    maximum = admit_selected_structural_stop(_candidate(94.0), context)
    below = admit_selected_structural_stop(_candidate(99.01), context)
    above = admit_selected_structural_stop(_candidate(93.99), context)

    assert minimum["structural_stop_gate"] == "pass"
    assert maximum["structural_stop_gate"] == "pass"
    assert below["structural_stop_gate"] == "fail"
    assert above["structural_stop_gate"] == "fail"


def test_short_uses_upper_boundary_and_selected_timeframe_atr():
    context = _context([
        _zone("zone-1", "1h", datetime(2026, 9, 4, 8, tzinfo=timezone.utc), direction="bearish", low=99.0, high=100.0),
    ])
    result = admit_selected_structural_stop(_candidate(101.5, direction="short", entry=98.0), context)

    assert result["structural_stop_gate"] == "pass"
    assert result["selected_zone_timeframe"] == "1h"
    assert result["structural_stop_buffer_atr"] == 1.5


def test_context_asset_and_cutoff_are_part_of_admission_identity():
    context = _context([_zone("zone-1", "4h", datetime(2026, 9, 4, 8, tzinfo=timezone.utc))])
    context["cutoff"] = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
    candidate = _candidate(99.0)
    candidate["observed_at"] = NOW.isoformat()

    result = admit_selected_structural_stop(candidate, context, now=NOW)

    assert result["structural_stop_gate"] == "fail"
    assert "structural context cutoff does not match candidate" in result["structural_stop_reasons"]


def test_boundary_minus_one_millisecond_observation_matches_context_cutoff():
    context = _context([_zone("zone-1", "4h", datetime(2026, 9, 4, 8, tzinfo=timezone.utc))])
    context["cutoff"] = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
    candidate = _candidate(99.0)
    candidate["observed_at"] = "2026-09-04T11:59:59.999000+00:00"

    result = admit_selected_structural_stop(candidate, context, now=context["cutoff"])

    assert result["structural_stop_gate"] == "pass"
    assert "structural context cutoff does not match candidate" not in result["structural_stop_reasons"]
def test_missing_atr_source_lineage_fails_closed():
    context = _context([_zone("zone-1", "4h", datetime(2026, 9, 4, 8, tzinfo=timezone.utc))])
    context["atr_source_bar_ids"] = {}

    result = admit_selected_structural_stop(_candidate(99.0), context)

    assert result["structural_stop_gate"] == "fail"
    assert "4h structural ATR source bar IDs are unavailable" in result["structural_stop_reasons"]


def test_structural_admission_is_enabled_by_default():
    assert config.STRUCTURAL_STOP_ADMISSION_ENABLED is True
    assert config.STRUCTURAL_STOP_MIN_ATR_MULTIPLE == 0.5
    assert config.STRUCTURAL_STOP_MAX_ATR_MULTIPLE == 3.0
