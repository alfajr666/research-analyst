from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import polars as pl

import config
from structural_stop import build_structural_contexts
from trade_admission import resolve


def test_declared_covered_structure_reaches_executor_selection():
    previous = {
        "STATIC_SYMBOLS_OVERRIDE": config.STATIC_SYMBOLS_OVERRIDE,
        "STRUCTURAL_STOP_ADMISSION_ENABLED": config.STRUCTURAL_STOP_ADMISSION_ENABLED,
    }
    try:
        config.STATIC_SYMBOLS_OVERRIDE = "BTC"
        config.STRUCTURAL_STOP_ADMISSION_ENABLED = True
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
            "structural_context": {
                "cutoff": observed,
                "zones": [{
                    "zone_id": "zone-1", "type": "order_block", "timeframe": "4h",
                    "direction": "bullish", "low": 96.0, "high": 97.0,
                    "state": "active", "created_at": datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
                    "coverage_status": "covered", "source_evidence_ids": ["obs-1", "obs-2"],
                }],
                "atr_by_timeframe": {"4h": 1.0},
                "atr_source_bar_ids": {"4h": ["obs-1", "obs-2"]},
            },
        }

        result = resolve([candidate], structural_contexts={"BTC": candidate["structural_context"]})

        assert result["selected_candidate_ids"] == ["candidate-1"]
        selected = result["results"][0]
        assert selected["status"] == "selected_for_executor"
        assert selected["structural_stop_gate"] == "pass"
        assert selected["selected_zone_id"] == "zone-1"
    finally:
        for name, value in previous.items():
            setattr(config, name, value)


def test_uncovered_structure_is_rejected_before_scoring():
    previous = {
        "STATIC_SYMBOLS_OVERRIDE": config.STATIC_SYMBOLS_OVERRIDE,
        "STRUCTURAL_STOP_ADMISSION_ENABLED": config.STRUCTURAL_STOP_ADMISSION_ENABLED,
    }
    try:
        config.STATIC_SYMBOLS_OVERRIDE = "BTC"
        config.STRUCTURAL_STOP_ADMISSION_ENABLED = True
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
            "structural_context": {
                "cutoff": observed, "zones": [], "atr_by_timeframe": {"4h": 1.0},
            },
        }

        result = resolve([candidate], structural_contexts={"BTC": candidate["structural_context"]})

        assert result["selected_candidate_ids"] == []
        rejected = result["results"][0]
        assert rejected["status"] == "hard_gate_failed"
        assert rejected["score_status"] == "not_evaluated"
        assert rejected["structural_stop_gate"] == "fail"
    finally:
        for name, value in previous.items():
            setattr(config, name, value)


def test_admission_builds_context_only_for_candidate_assets():
    observed = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)

    def bars(count, hours):
        return pl.DataFrame({
            "timestamp": [observed - timedelta(hours=hours * (count - index)) for index in range(count)],
            "open": [100.0 + index for index in range(count)],
            "high": [101.0 + index for index in range(count)],
            "low": [99.0 + index for index in range(count)],
            "close": [100.0 + index for index in range(count)],
            "volume": [1.0] * count,
            "bar_id": [f"bar-{hours}-{index}" for index in range(count)],
            "source_observation_ids": [[f"bar-{hours}-{index}"] for index in range(count)],
        })

    context_zone = {
        "type": "order_block", "direction": "bullish", "low": 98.0, "high": 99.0,
        "state": "active", "created_at": observed - timedelta(hours=4),
        "source_evidence_ids": ["bar-4h-1"],
    }
    candidate = {
        "candidate_id": "candidate-1", "asset": "BTC", "direction": "long",
        "entry_price": 100.0, "invalidation_price": 97.0, "targets": [110.0],
        "valid_until": "2099-09-03T00:00:00+00:00", "data_freshness_seconds": 1.0,
    }
    connection = MagicMock()
    with patch("structural_stop.config.get_db_connection", return_value=connection), \
            patch("regime_history.load_regime_4h_bars", return_value=bars(57, 4)) as load_4h, \
            patch("regime_history.load_regime_1h_bars", return_value=bars(57, 1)) as load_1h, \
            patch("structure_zones.detect_fvg", return_value=[context_zone]), \
            patch("structure_zones.detect_order_blocks", return_value=[]):
        contexts = build_structural_contexts([candidate], observed)

    assert set(contexts) == {"BTC"}
    assert load_4h.call_args.args[1] == "BTC"
    assert load_1h.call_args.args[1] == "BTC"
    decision = resolve([candidate], structural_contexts=contexts, now=observed)
    assert decision["selected_candidate_ids"] == ["candidate-1"]
