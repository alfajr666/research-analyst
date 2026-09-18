from datetime import datetime, timedelta, timezone

import reaction_scorer


def _bars(count=288, start=None):
    start = start or datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(count):
        ts = start + timedelta(minutes=15 * i)
        price = 100.0 + (i % 8) * 0.05
        rows.append({
            "source_start": int(ts.timestamp() * 1000),
            "source_end": int((ts + timedelta(minutes=15)).timestamp() * 1000) - 1,
            "open": price,
            "high": price + 0.2,
            "low": price - 0.2,
            "close": price + 0.05,
            "volume": 10.0,
        })
    rows[260]["volume"] = 1000.0
    return rows


def test_profile_requires_three_complete_days_and_returns_poc_and_hvns():
    bars = _bars()
    result = reaction_scorer.build_value_profile(
        "BTC", bars[-1]["source_end"], bars, None
    )
    assert result["status"] == "ok"
    assert result["bars"] == 288
    assert result["anchor_hour_start"] is not None
    assert result["poc"] is not None
    assert isinstance(result["hvn_nodes"], list)


def test_candidate_resamples_aligned_five_minute_bars_before_profile_build():
    bars15 = _bars()
    bars5 = []
    for bar in bars15:
        start = bar["source_start"]
        for offset in (0, 300_000, 600_000):
            price = float(bar["open"]) + offset / 600_000 * 0.01
            bars5.append({
                "source_start": start + offset,
                "source_end": start + offset + 300_000 - 1,
                "open": price, "high": price + 0.07, "low": price - 0.07,
                "close": price + 0.02, "volume": float(bar["volume"]) / 3.0,
            })
    result = reaction_scorer.score_candidate_v3(
        {"direction": "long", "entry_price": 100.0, "reaction_bar": bars5[-1]},
        bars5,
        evaluation_cutoff=bars5[-1]["source_end"],
    )
    assert result["profile"]["status"] == "ok"
    assert result["profile"]["bars"] == 287  # final reaction bar is held out


def test_funding_is_mirrored_and_opposite_crowding_requires_confirmation():
    history = [{"source_at": i, "rate": -0.00001 * (i + 1)} for i in range(40)]
    long_boost = reaction_scorer.score_funding_crowding(
        "long", history, {"status": "support"}, {"status": "support"}
    )
    short_penalty = reaction_scorer.score_funding_crowding(
        "short", history, {"status": "support"}, {"status": "support"}
    )
    blocked = reaction_scorer.score_funding_crowding(
        "long", history, {"status": "neutral"}, {"status": "support"}
    )
    assert long_boost["value"] > 0.5
    assert short_penalty["value"] < 0.5
    assert blocked["value"] == 0.5


def test_funding_cutoff_deduplicates_and_rejects_future_observations():
    cutoff = 1_700_000_000_000
    history = [
        {"source_at": cutoff - (40 - index) * 3_600_000, "rate": -0.00001 * (index + 1)}
        for index in range(32)
    ]
    history.extend([
        dict(history[-1]),
        {"source_at": cutoff + 3_600_000, "rate": -0.5},
    ])
    result = reaction_scorer.score_funding_crowding(
        "long", history, {"status": "support"}, {"status": "support"},
        evaluation_cutoff=cutoff,
    )
    assert result["observations"] == 32


def test_v3_oi_support_has_fixed_weight_and_enforce_selects_it():
    candidate = {
        "candidate_id": "reaction-1", "direction": "long", "entry_price": 100.0,
        "invalidation_price": 98.0, "targets": [104.0],
        "reaction_bar": {"open": 99.5, "high": 101.0, "low": 99.0, "close": 100.8},
    }
    bars = _bars()
    result = reaction_scorer.score_candidate_v3(
        candidate, bars, [{"source_at": i, "open_interest": 100 + i} for i in range(40)],
        mode="enforce", evaluation_cutoff=bars[-1]["source_end"],
    )
    assert result["scorer_version"] == "reaction-scorer-v3"
    assert result["weights"]["oi-participation-v1"] == 0.15
    assert result["operational_score"] == result["reaction_score"]
    assert "oi-participation-v1" in result["observations"]


def test_shadow_keeps_v2_operational_but_persists_v3():
    candidate = {
        "candidate_id": "reaction-2", "direction": "long", "entry_price": 100.0,
        "invalidation_price": 98.0, "targets": [104.0],
        "reaction_bar": {"open": 99.5, "high": 101.0, "low": 99.0, "close": 100.8},
    }
    result = reaction_scorer.select_operational_result(
        {"quality_score": 0.41, "verdict": "reject"},
        {"reaction_score": 0.90, "verdict": "pass"},
        "shadow",
    )
    assert result["quality_score"] == 0.41
    assert result["operational_version"] == "trade-quality-v2"
