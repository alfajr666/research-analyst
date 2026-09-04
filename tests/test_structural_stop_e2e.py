from datetime import datetime, timedelta, timezone

import config
from trade_admission import resolve


def test_declared_covered_structure_reaches_executor_selection():
    previous = {
        "STATIC_SYMBOLS_OVERRIDE": config.STATIC_SYMBOLS_OVERRIDE,
        "STRUCTURAL_STOP_ADMISSION_ENABLED": config.STRUCTURAL_STOP_ADMISSION_ENABLED,
        "STRUCTURAL_STOP_REQUIRED_STRATEGIES": config.STRUCTURAL_STOP_REQUIRED_STRATEGIES,
        "STRUCTURAL_STOP_MAX_REFERENCE_GAP_PCT": config.STRUCTURAL_STOP_MAX_REFERENCE_GAP_PCT,
    }
    try:
        config.STATIC_SYMBOLS_OVERRIDE = "BTC"
        config.STRUCTURAL_STOP_ADMISSION_ENABLED = True
        config.STRUCTURAL_STOP_REQUIRED_STRATEGIES = frozenset({"trend-wall-v1"})
        config.STRUCTURAL_STOP_MAX_REFERENCE_GAP_PCT = 0.02
        observed = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)
        candidate = {
            "candidate_id": "candidate-1",
            "strategy_id": "trend-wall-v1",
            "asset": "BTC",
            "direction": "long",
            "entry_price": 100.0,
            "invalidation_price": 95.1,
            "targets": [110.0],
            "atr14_4h": 10.0,
                "valid_until": datetime(2099, 9, 3, tzinfo=timezone.utc),
            "observed_at": observed,
            "data_freshness_seconds": 60.0,
            "structural_reference": {
                "reference_id": "zone-1",
                "kind": "swing_low",
                "timeframe": "4h",
                "asset": "BTC",
                "boundary_price": 96.0,
                "formed_at": datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
                "confirmed_at": datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
                "cutoff_at": observed,
                "coverage_status": "covered",
                "source_evidence_ids": ["obs-1", "obs-2"],
            },
        }

        result = resolve([candidate])

        assert result["selected_candidate_ids"] == ["candidate-1"]
        selected = result["results"][0]
        assert selected["status"] == "selected_for_executor"
        assert selected["structural_stop_gate"] == "pass"
        assert selected["reference_id"] == "zone-1"
    finally:
        for name, value in previous.items():
            setattr(config, name, value)


def test_uncovered_structure_is_rejected_before_scoring():
    previous = {
        "STATIC_SYMBOLS_OVERRIDE": config.STATIC_SYMBOLS_OVERRIDE,
        "STRUCTURAL_STOP_ADMISSION_ENABLED": config.STRUCTURAL_STOP_ADMISSION_ENABLED,
        "STRUCTURAL_STOP_REQUIRED_STRATEGIES": config.STRUCTURAL_STOP_REQUIRED_STRATEGIES,
        "STRUCTURAL_STOP_MAX_REFERENCE_GAP_PCT": config.STRUCTURAL_STOP_MAX_REFERENCE_GAP_PCT,
    }
    try:
        config.STATIC_SYMBOLS_OVERRIDE = "BTC"
        config.STRUCTURAL_STOP_ADMISSION_ENABLED = True
        config.STRUCTURAL_STOP_REQUIRED_STRATEGIES = frozenset({"trend-wall-v1"})
        config.STRUCTURAL_STOP_MAX_REFERENCE_GAP_PCT = 0.02
        observed = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)
        candidate = {
            "candidate_id": "candidate-2",
            "strategy_id": "trend-wall-v1",
            "asset": "BTC",
            "direction": "long",
            "entry_price": 100.0,
            "invalidation_price": 94.0,
            "targets": [110.0],
            "atr14_4h": 10.0,
            "valid_until": datetime(2099, 9, 3, tzinfo=timezone.utc),
            "observed_at": observed,
            "data_freshness_seconds": 60.0,
            "structural_reference": {
                "reference_id": "zone-2",
                "kind": "swing_low",
                "timeframe": "4h",
                "asset": "BTC",
                "boundary_price": 96.0,
                "formed_at": datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
                "confirmed_at": datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
                "cutoff_at": observed,
                "coverage_status": "incomplete",
                "source_evidence_ids": ["obs-1"],
            },
        }

        result = resolve([candidate])

        assert result["selected_candidate_ids"] == []
        rejected = result["results"][0]
        assert rejected["status"] == "hard_gate_failed"
        assert rejected["score_status"] == "not_evaluated"
        assert rejected["structural_stop_gate"] == "fail"
    finally:
        for name, value in previous.items():
            setattr(config, name, value)
