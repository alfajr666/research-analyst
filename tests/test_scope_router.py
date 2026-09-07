from datetime import datetime, timezone

import pytest

from scope_router import build_strategy_scope


UTC = timezone.utc
CUTOFF = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def test_scope_router_keeps_strategy_symbol_dumb_and_preserves_metadata():
    scope = build_strategy_scope(
        ["SOL", "BTC"],
        plugin_id="failed-break-v3",
        market_family="reversal",
        cutoff=CUTOFF,
        feed_metadata={"feed_id": "feed-1", "effective_universe_version": "universe-1"},
        regime_scope={"mode": "off"},
    )

    assert scope["allowed_assets"] == ["BTC", "SOL"]
    assert scope["excluded_assets"] == []
    assert scope["effective_universe_version"] == "universe-1"
    assert scope["scope_contract_version"] == "strategy-scope-v1"
    assert scope["evaluation_cutoff"] == "2026-09-07T12:00:00Z"


def test_scope_router_applies_regime_family_without_account_policy():
    scope = build_strategy_scope(
        ["SOL", "ETH"],
        plugin_id="failed-break-v3",
        market_family="trend",
        cutoff=CUTOFF,
        feed_metadata={"feed_id": "feed-1"},
        regime_scope={
            "mode": "enforce",
            "allowed_assets": ["SOL", "ETH"],
            "family_assets": {"trend": ["SOL"], "mean_reversion": ["ETH"]},
            "cutoff_at": "2026-09-07T11:55:00Z",
        },
    )

    assert scope["allowed_assets"] == ["SOL"]
    assert scope["excluded_assets"] == [{"asset": "ETH", "reason": "regime_family:trend"}]
    assert scope["regime_scope_id"] == "feed-1:2026-09-07T11:55:00Z:enforce"


def test_scope_router_rejects_unknown_enforced_family():
    with pytest.raises(ValueError, match="unsupported regime scope mode"):
        build_strategy_scope(
            ["BTC"],
            plugin_id="test",
            market_family="unknown",
            cutoff=CUTOFF,
            regime_scope={"mode": "invalid"},
        )
