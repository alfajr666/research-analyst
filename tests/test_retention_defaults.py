"""Lock the retention defaults from specs/resource-footprint-retention-v1.md.

These values are a recorded decision. They may only change together with a
spec-version bump; tests import live config (not copies) so a local .env value
that overrides a locked default fails loudly here.
"""
import config


def test_prune_interval_defaults_match_locked_spec():
    assert config.PRUNE_5M_DAYS == 21
    assert config.PRUNE_15M_DAYS == 30
    assert config.PRUNE_1H_DAYS == 30
    assert config.PRUNE_4H_DAYS == 45
    assert config.PRUNE_INTERVAL_DAYS == {
        "5m": 21, "15m": 30, "1h": 30, "4h": 45,
    }


def test_regime_retention_defaults_match_locked_spec():
    assert config.REGIME_SCORE_RETENTION_DAYS == 14
    assert config.REGIME_GATE_RETENTION_DAYS == 30


def test_market_auxiliary_defaults_match_locked_spec():
    assert config.MARKET_WATCHLIST_RETENTION_DAYS == 90
    assert config.MARKET_REGIME_RETENTION_DAYS == 30


def test_retention_covers_maximum_declared_strategy_lookback():
    """Retention-floor invariant: specs/resource-footprint-retention-v1.md 4.1.

    The 7 vectorbt engine-handoff ports declare lookback_days=20; retention
    must never truncate a requested load_bars window (20d + 1d margin).
    If a strategy ever declares a longer lookback, raise the TTL with it.
    """
    from strategy_plugins import _REGISTRY

    max_lookback = max(
        (plugin.lookback_days for plugin in _REGISTRY.values()), default=16
    )
    assert config.PRUNE_5M_DAYS >= max_lookback + 1
    assert config.PRUNE_15M_DAYS >= max_lookback + 1


def test_tier_values_are_positive():
    # 0 disables a tier (keeps rows forever); legacy 1h/4h tiers must keep
    # draining residue, never freeze it.
    for value in config.PRUNE_INTERVAL_DAYS.values():
        assert value > 0
