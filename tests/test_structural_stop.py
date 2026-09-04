from datetime import datetime, timezone

from structural_stop import admit_selected_structural_stop, select_structural_zone


NOW = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)


def _candidate(direction, stop):
    return {
        "asset": "BTC",
        "direction": direction,
        "entry_price": 100.0,
        "invalidation_price": stop,
        "observed_at": NOW.isoformat(),
    }


def _context(direction="bullish", low=95.0, high=96.0, state="active"):
    return {
        "asset": "BTC",
        "cutoff": NOW,
        "zones": [{
            "zone_id": "structure-1",
            "asset": "BTC",
            "type": "order_block",
            "timeframe": "4h",
            "direction": direction,
            "low": low,
            "high": high,
            "state": state,
            "created_at": datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
            "confirmed_at": datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
            "coverage_status": "covered",
            "source_evidence_ids": ["obs-1"],
        }],
        "atr_by_timeframe": {"4h": 2.0},
        "atr_source_bar_ids": {"4h": ["bar-1"]},
    }


def test_long_stop_uses_lower_reference_and_atr_buffer():
    result = admit_selected_structural_stop(_candidate("long", 94.0), _context())

    assert result["structural_stop_gate"] == "pass"
    assert result["structural_stop_buffer"] == 1.0
    assert result["structural_stop_buffer_atr"] == 0.5


def test_short_stop_uses_upper_reference_and_atr_buffer():
    result = admit_selected_structural_stop(
        _candidate("short", 106.0), _context("bearish", low=104.0, high=105.0),
    )

    assert result["structural_stop_gate"] == "pass"
    assert result["structural_stop_buffer"] == 1.0


def test_small_structural_buffer_fails_closed():
    result = admit_selected_structural_stop(_candidate("long", 94.5), _context())

    assert result["structural_stop_gate"] == "fail"
    assert "structural stop buffer is below minimum ATR multiple" in result["structural_stop_reasons"]


def test_uncovered_zone_fails_closed():
    context = _context("bearish", low=104.0, high=105.0, state="inactive")
    result = admit_selected_structural_stop(_candidate("short", 106.0), context)

    assert result["structural_stop_gate"] == "fail"
    assert "no eligible HTF structural zone" in result["structural_stop_reasons"]


def test_zone_selection_prioritizes_4h_then_latest_zone():
    zones = [
        {
            "zone_id": "old-4h", "asset": "BTC", "type": "fvg", "timeframe": "4h", "direction": "bullish",
            "low": 94.0, "high": 95.0, "state": "active",
                "created_at": datetime(2026, 9, 1, 4, tzinfo=timezone.utc),
                "confirmed_at": datetime(2026, 9, 1, 4, tzinfo=timezone.utc),
            "coverage_status": "covered", "source_evidence_ids": ["old"],
        },
        {
            "zone_id": "new-4h", "asset": "BTC", "type": "fvg", "timeframe": "4h", "direction": "bullish",
            "low": 96.0, "high": 97.0, "state": "active",
                "created_at": datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
                "confirmed_at": datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
            "coverage_status": "covered", "source_evidence_ids": ["new"],
        },
        {
            "zone_id": "new-1h", "asset": "BTC", "type": "fvg", "timeframe": "1h", "direction": "bullish",
            "low": 98.0, "high": 99.0, "state": "active",
                "created_at": datetime(2026, 9, 1, 10, tzinfo=timezone.utc),
                "confirmed_at": datetime(2026, 9, 1, 10, tzinfo=timezone.utc),
            "coverage_status": "covered", "source_evidence_ids": ["one-hour"],
        },
    ]

    selected = select_structural_zone(
        zones, asset="BTCUSDT", direction="long", entry=100.0, cutoff=NOW,
    )

    assert selected["zone_id"] == "new-4h"


def test_zone_selection_rejects_other_assets():
    zone = {
        "zone_id": "eth-zone", "asset": "ETH", "type": "fvg", "timeframe": "4h",
        "direction": "bullish", "low": 95.0, "high": 96.0, "state": "active",
        "created_at": datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
        "source_evidence_ids": ["eth"],
    }

    assert select_structural_zone(
        [zone], asset="BTC", direction="long", entry=100.0, cutoff=NOW,
    ) is None
