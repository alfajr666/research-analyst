from datetime import datetime, timezone

import config
from trade_admission import admit, candidate_admission_fingerprint, resolve, score


def _candidate(stop, atr=10):
    return {
        "candidate_id": "candidate-1", "asset": "BTC", "direction": "long",
        "entry_price": 100.0, "invalidation_price": stop, "targets": [120.0],
        "atr14_4h": atr, "valid_until": "2099-01-01T00:05:00+00:00",
        "data_freshness_seconds": 1.0,
        "structural_context": {
            "asset": "BTC",
            "cutoff": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "zones": [{
                "zone_id": "zone-1", "asset": "BTC", "type": "order_block", "timeframe": "4h",
                "direction": "bullish", "low": 97.0, "high": 97.5,
                "state": "active", "created_at": datetime(2025, 12, 31, tzinfo=timezone.utc),
                "confirmed_at": datetime(2025, 12, 31, tzinfo=timezone.utc),
                "source_evidence_ids": ["bar-1"], "coverage_status": "covered",
            }],
            "atr_by_timeframe": {"4h": 4.0},
            "atr_source_bar_ids": {"4h": ["bar-1"]},
        },
    }


def test_stop_at_configured_atr_floor_passes():
    result = admit(_candidate(95.0, atr=10), now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert result["hard_gate"] == "pass"
    assert result["stop_atr_multiple"] == 1.25


def test_admission_fingerprint_normalises_closed_bar_aliases():
    exact = {"candidate_id": "id", "strategy_id": "s", "asset": "BTC", "direction": "long",
             "entry_price": 100, "invalidation_price": 99, "take_profit": 102,
             "observed_at": "2026-01-01T00:00:00Z", "valid_until": "2026-01-01T00:05:00Z"}
    alias = dict(exact, observed_at="2025-12-31T23:59:59.999000Z", valid_until="2026-01-01T00:04:59.999000Z")

    assert candidate_admission_fingerprint(exact) == candidate_admission_fingerprint(alias)


def test_stop_below_atr_floor_fails(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURAL_STOP_ADMISSION_ENABLED", False)
    result = admit(_candidate(99.5), now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert result["hard_gate"] == "fail"
    assert "stop distance below ATR-based minimum" in result["hard_gate_reasons"]


def test_missing_atr_fails_closed():
    event = _candidate(95.0)
    event.pop("atr14_4h")
    event["structural_context"]["atr_by_timeframe"] = {}
    result = admit(event, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert result["hard_gate"] == "fail"
    assert "4h ATR14 is unavailable or invalid" in result["hard_gate_reasons"]


def test_structural_buffer_replaces_global_entry_stop_maximum():
    event = _candidate(80.0)
    event["targets"] = [140.0]
    event["structural_context"]["zones"][0]["low"] = 81.0
    event["structural_context"]["atr_by_timeframe"] = {"4h": 1.0}

    result = admit(event, now=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert result["hard_gate"] == "pass"
    assert result["structural_stop_buffer_atr"] == 1.0


def test_structural_stop_is_an_independent_hard_gate(monkeypatch):
    event = _candidate(96.0, atr=10)
    event.update({
        "strategy_id": "failed-break-v3",
        "observed_at": "2026-01-01T00:00:00+00:00",
        "structural_context": {
            "asset": "BTC",
            "cutoff": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "zones": [{
                "zone_id": "zone-1", "asset": "BTC", "type": "fvg", "timeframe": "4h",
                "direction": "bullish", "low": 97.0, "high": 98.0,
                    "state": "active", "created_at": datetime(2025, 12, 31, tzinfo=timezone.utc),
                    "confirmed_at": datetime(2025, 12, 31, tzinfo=timezone.utc),
                "source_evidence_ids": ["bar-1"], "coverage_status": "covered",
                "confirmed_at": datetime(2025, 12, 31, tzinfo=timezone.utc),
            }],
            "atr_by_timeframe": {"4h": 1.0},
            "atr_source_bar_ids": {"4h": ["bar-1"]},
        },
    })
    monkeypatch.setattr(config, "STRUCTURAL_STOP_ADMISSION_ENABLED", True)

    result = admit(event, now=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert result["hard_gate"] == "pass"
    assert result["structural_stop_gate"] == "pass"


def test_structural_stop_failure_does_not_get_scored(monkeypatch):
    event = _candidate(96.0, atr=10)
    event.update({"strategy_id": "failed-break-v3"})
    monkeypatch.setattr(config, "STRUCTURAL_STOP_ADMISSION_ENABLED", True)
    event.pop("structural_context")

    from trade_admission import resolve

    result = resolve([event])

    assert result["results"][0]["hard_gate"] == "fail"
    assert result["results"][0]["score_status"] == "not_evaluated"


def test_stop_too_far_from_htf_zone_is_rejected_before_scoring(monkeypatch):
    event = _candidate(75.0, atr=10)
    event.update({
        "strategy_id": "failed-break-v3",
        "targets": [200.0],
        "structural_context": {
            "asset": "BTC",
            "cutoff": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "zones": [{
                "zone_id": "zone-1", "asset": "BTC", "type": "order_block", "timeframe": "4h",
                    "direction": "bullish", "low": 81.0, "high": 82.0,
                    "state": "active", "created_at": datetime(2025, 12, 31, tzinfo=timezone.utc),
                    "confirmed_at": datetime(2025, 12, 31, tzinfo=timezone.utc),
                    "source_evidence_ids": ["bar-1"], "coverage_status": "covered",
            }],
            "atr_by_timeframe": {"4h": 1.0},
            "atr_source_bar_ids": {"4h": ["bar-1"]},
        },
    })
    monkeypatch.setattr(config, "STRUCTURAL_STOP_ADMISSION_ENABLED", True)

    def score_must_not_run(_candidate):
        raise AssertionError("structural rejection must happen before scoring")

    monkeypatch.setattr("trade_admission.score", score_must_not_run)
    from trade_admission import resolve

    result = resolve([event], now=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert result["results"][0]["hard_gate"] == "fail"
    assert result["results"][0]["structural_stop_gate"] == "fail"
    assert "structural stop buffer is above maximum ATR multiple" in result["results"][0]["hard_gate_reasons"][-1]
    assert result["results"][0]["score_status"] == "not_evaluated"


def test_score_is_independent_of_strategy_confluence():
    event = _candidate(95.0, atr=10)
    event["data_freshness_seconds"] = 1.0
    event["_confluence_score"] = 1.0

    scored = score(event, structural_context=event["structural_context"], agreement=0.0)

    assert scored["score_status"] == "scored"
    assert "strategy_component" not in scored["components"]
    assert "swing_component" not in scored["components"]
    assert scored["components"]["htf_bias_component"]["status"] == "support"
    assert scored["components"]["freshness_component"]["status"] == "support"


def test_same_direction_clash_uses_independent_score():
    first = _candidate(95.0)
    first.update({"candidate_id": "first", "strategy_id": "first-strategy", "data_freshness_seconds": 1.0})
    second = _candidate(95.0)
    second.update({"candidate_id": "second", "strategy_id": "second-strategy", "data_freshness_seconds": 500.0})

    result = resolve([first, second], now=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert result["selected_candidate_ids"] == ["first"]
    assert result["results"][0]["score_status"] == "scored"
    assert result["results"][1]["status"] == "eligible_suppressed_by_same_direction_rank"


def test_opposite_direction_clash_requires_score_margin():
    long_event = _candidate(95.0)
    long_event.update({"candidate_id": "long", "strategy_id": "long-strategy", "data_freshness_seconds": 1.0})
    short_event = _candidate(106.0)
    short_event.update({
        "candidate_id": "short", "strategy_id": "short-strategy", "direction": "short",
        "targets": [80.0], "data_freshness_seconds": 1.0,
        "structural_context": {
            **short_event["structural_context"],
            "zones": [{
                **short_event["structural_context"]["zones"][0],
                "direction": "bearish", "low": 103.0, "high": 104.0,
            }],
        },
    })

    result = resolve([long_event, short_event], now=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert result["selected_candidate_ids"] == []
    assert all(item["status"] == "eligible_suppressed_by_opposite_direction_clash"
               for item in result["results"])
