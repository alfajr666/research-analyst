from datetime import datetime, timezone

import config
from trade_quality import resolve, score_candidate
from trade_quality_profiles import effective_profile_weights


def _candidate(candidate_id="candidate-1", direction="long", strategy_id="dual-zone-follower-v3"):
    return {
        "candidate_id": candidate_id,
        "strategy_id": strategy_id,
        "plugin_version": "v1",
        "asset": "BTC",
        "direction": direction,
        "market_family": "trend",
        "observed_at": "2026-09-10T12:00:00+00:00",
        "valid_until": "2026-09-10T12:05:00+00:00",
        "entry_price": 100.0,
        "invalidation_price": 97.0 if direction == "long" else 103.0,
        "targets": [110.0] if direction == "long" else [90.0],
        "data_freshness_seconds": 1.0,
        "effective_universe_assets": ["BTC"],
        "effective_universe_version": "feed-1",
        "_execution_account": "fundamo",
        "_score_agreement": 4.0,
    }


def _structure(direction="bullish"):
    return {
        "asset": "BTC",
        "cutoff": datetime(2026, 9, 10, 12, tzinfo=timezone.utc),
        "zones": [{
            "zone_id": "zone-1",
            "asset": "BTC",
            "type": "order_block",
            "timeframe": "4h",
            "direction": direction,
            "low": 98.0,
            "high": 99.0,
            "state": "active",
            "created_at": "2026-09-10T08:00:00+00:00",
            "confirmed_at": "2026-09-10T08:00:00+00:00",
            "coverage_status": "covered",
            "source_evidence_ids": ["bar-1"],
        }],
        "atr_by_timeframe": {"4h": 1.0},
        "atr_source_bar_ids": {"4h": ["bar-1"]},
    }


def _bars():
    return {
        "volume": [100.0] * 96 + [250.0],
        "funding_rate": [0.0001] * 31 + [0.0002] + [0.001],
        "funding_rate_available": [True] * 33,
    }


def test_score_contains_independent_rvol_and_funding_evidence():
    result = score_candidate(
        _candidate(),
        structural_context=_structure(),
        market_bars=_bars(),
        regime_mode="off",
    )

    assert result["score_policy_version"] == "trade-quality-v2"
    assert result["score_decision"] == "eligible"
    assert 0.0 <= result["quality_score"] <= 1.0
    assert result["components"]["rvol"]["raw_inputs"]["rvol"] == 2.5
    assert result["components"]["funding_overheating"]["raw_inputs"]["heat"] > 0
    assert result["selected_zone_id"] == "zone-1"


def test_invalid_reward_risk_remains_diagnostic_only():
    candidate = _candidate()
    candidate["targets"] = [101.0]
    result = score_candidate(candidate, structural_context=_structure(), regime_mode="off")

    assert result["quality_score"] > 0.30
    assert result["score_decision"] == "eligible"
    assert result["components"]["reward_risk"]["status"] == "invalid"
    assert result["hard_gate"] == "not_applicable"


def test_legacy_admission_components_contribute_to_the_score():
    valid = score_candidate(_candidate(), structural_context=_structure(), regime_mode="off")
    weak = _candidate()
    weak["targets"] = [101.0]
    weak_result = score_candidate(weak, structural_context=_structure(), regime_mode="off")

    assert weak_result["quality_score"] < valid["quality_score"]
    assert weak_result["components"]["reward_risk"]["role"] == "quality"
    assert weak_result["components"]["structural_stop"]["role"] == "quality"


def test_profile_blending_is_normalized_and_intensity_bounded():
    name, weights = effective_profile_weights("trend", 0.5)

    assert name == "trend-v1"
    assert set(weights) >= {"rvol", "funding_overheating"}
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_opposing_candidates_require_normalized_score_margin(monkeypatch):
    monkeypatch.setattr(config, "REGIME_SESSION_MODE", "off")
    monkeypatch.setattr(config, "TRADE_QUALITY_CLASH_MIN_MARGIN", 0.10)
    long_candidate = _candidate("long", "long")
    short_candidate = _candidate("short", "short")
    short_candidate["invalidation_price"] = 103.0
    short_candidate["targets"] = [90.0]
    result = resolve(
        [long_candidate, short_candidate],
        structural_contexts={"BTC": _structure()},
        market_bars_by_asset={"BTC": _bars()},
        regime_scope={"mode": "off"},
    )

    assert len(result["selected_candidate_ids"]) <= 1
    assert all(0.0 <= item["quality_score"] <= 1.0 for item in result["results"])
