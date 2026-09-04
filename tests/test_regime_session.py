from datetime import datetime, timezone
from pathlib import Path

import config
from regime_session import gate_decision, load_gate_scope, publish_regime_batch


def _score(status="ok"):
    return {
        "status": status,
        "trend_weight": 0.6 if status == "ok" else 0.0,
        "mean_reversion_weight": 0.4 if status == "ok" else 0.0,
        "reversal_weight": 0.0,
        "confidence": 0.9 if status == "ok" else 0.0,
        "components": {},
        "market_data": {},
    }


def test_gate_blocks_cooldown_but_allows_active_session():
    blocked = gate_decision(
        "SOL", "2026-09-04T13:20:00Z", _score(), "feed-1"
    )
    allowed = gate_decision(
        "SOL", "2026-09-04T13:40:00Z", _score(), "feed-1"
    )

    assert blocked["decision"] == "block"
    assert blocked["reasons"] == ["session_open_cooldown"]
    assert allowed["decision"] == "allow"


def test_missing_score_data_is_a_per_asset_block():
    result = gate_decision(
        "SOL", "2026-09-04T13:40:00Z", _score("insufficient_data"), "feed-1"
    )

    assert result["decision"] == "block"
    assert result["reasons"] == ["regime_score_insufficient_data"]


def test_family_activation_is_independent_per_asset_family():
    result = gate_decision(
        "SOL",
        "2026-09-04T13:40:00Z",
        {
            **_score(),
            "trend_weight": 0.8,
            "mean_reversion_weight": 0.1,
            "reversal_weight": 0.0,
        },
        "feed-1",
    )

    assert result["decision"] == "allow"
    assert result["active_families"] == ["trend"]
    assert result["family_activation"]["families"]["mean_reversion"]["reason"] == "below_threshold"


def test_family_activation_holds_between_hysteresis_thresholds():
    result = gate_decision(
        "SOL",
        "2026-09-04T13:40:00Z",
        {**_score(), "trend_weight": 0.3, "mean_reversion_weight": 0.1},
        "feed-1",
        previous_score={"trend_weight": 0.4, "mean_reversion_weight": 0.1, "reversal_weight": 0.0},
    )

    assert result["active_families"] == ["trend"]
    assert result["family_activation"]["families"]["trend"]["reason"] == "hysteresis_hold"


def test_publish_and_load_gate_scope_require_exact_feed_and_cutoff(monkeypatch, tmp_path):
    market_db = Path(tmp_path) / "market.sqlite3"
    regime_db = Path(tmp_path) / "regime.sqlite3"
    config.init_market_db(market_db)

    def score(_conn, asset, _cutoff):
        return _score("insufficient_data" if asset == "SOL" else "ok")

    monkeypatch.setattr("regime_session.regime_score_for_asset", score)
    cutoff = datetime(2026, 9, 4, 13, 40, tzinfo=timezone.utc)
    summary = publish_regime_batch(
        cutoff,
        assets=["SOL", "ETH"],
        feed_id="feed-1",
        market_db_path=market_db,
        regime_db_path=regime_db,
    )

    assert summary["allowed"] == ["ETH"]
    assert summary["blocked"] == ["SOL"]
    scope = load_gate_scope(
        ["SOL", "ETH"], cutoff, "feed-1", mode="enforce", regime_db_path=regime_db
    )
    assert scope["allowed_assets"] == ["ETH"]
    assert scope["blocked_assets"] == ["SOL"]

    mismatch = load_gate_scope(
        ["SOL", "ETH"], cutoff, "wrong-feed", mode="enforce", regime_db_path=regime_db
    )
    assert mismatch["allowed_assets"] == []
    assert mismatch["missing_assets"] == ["ETH", "SOL"]


def test_scope_activates_families_per_asset(monkeypatch, tmp_path):
    market_db = Path(tmp_path) / "market.sqlite3"
    regime_db = Path(tmp_path) / "regime.sqlite3"
    config.init_market_db(market_db)

    def score(_conn, asset, _cutoff):
        if asset == "SOL":
            return {**_score(), "trend_weight": 0.8, "mean_reversion_weight": 0.1}
        return {**_score(), "trend_weight": 0.1, "mean_reversion_weight": 0.8}

    monkeypatch.setattr("regime_session.regime_score_for_asset", score)
    cutoff = datetime(2026, 9, 4, 13, 40, tzinfo=timezone.utc)
    publish_regime_batch(
        cutoff,
        assets=["SOL", "ETH"],
        feed_id="feed-1",
        market_db_path=market_db,
        regime_db_path=regime_db,
    )

    scope = load_gate_scope(
        ["SOL", "ETH"], cutoff, "feed-1", mode="enforce", regime_db_path=regime_db
    )

    assert scope["family_assets"] == {
        "trend": ["SOL"],
        "mean_reversion": ["ETH"],
        "reversal": [],
    }
