from datetime import datetime, timedelta, timezone

import config
from regime_session import load_gate_scope, publish_regime_batch


def test_enforced_regime_scope_filters_each_plugin_by_asset_family(monkeypatch, tmp_path):
    import strategy_plugins
    from strategy_v2_context import evaluation_symbols

    market_db = tmp_path / "market.sqlite3"
    analyst_db = tmp_path / "analyst.sqlite3"
    regime_db = tmp_path / "regime.sqlite3"
    config.init_market_db(market_db)
    config.init_analyst_db(analyst_db)
    old_registry = strategy_plugins._REGISTRY.copy()
    old_enabled = config.STRATEGY_ENABLED_IDS
    old_active = config.STRATEGY_ACTIVE_IDS
    old_analyst_db = config.ANALYST_DB_PATH
    monkeypatch.setattr(config, "DEEP_WARMUP_GATE_ENABLED", False)
    config.ANALYST_DB_PATH = str(analyst_db)
    cutoff = datetime(2026, 9, 4, 13, 40, tzinfo=timezone.utc)
    market_conn = config.get_db_connection(db_path=market_db)
    market_conn.execute(
        """
        INSERT INTO source_observations
            (observation_id, source, venue, native_symbol, asset, market_kind,
             interval, source_start, source_end, retrieved_at, retrieval_kind,
             payload_json)
            VALUES (?, 'bybit_ws', 'bybit', 'SOLUSDT', 'SOL', 'perpetual',
                '5m', ?, ?, ?, 'test', ?)
        """,
        (
            "sol-bar",
            cutoff - timedelta(minutes=5),
            cutoff,
            cutoff,
            '{"open": 100, "high": 101, "low": 99, "close": 100}',
        ),
    )
    market_conn.commit()
    market_conn.close()
    monkeypatch.setattr(
        "regime_session.regime_score_for_asset",
        lambda _conn, asset, _cutoff: {
            "status": "ok",
            "trend_weight": 0.8 if asset == "SOL" else 0.1,
            "mean_reversion_weight": 0.1 if asset == "SOL" else 0.8,
            "reversal_weight": 0.0,
            "confidence": 0.9,
            "components": {},
            "market_data": {},
        },
    )
    publish_regime_batch(
        cutoff,
        assets=["SOL", "ETH"],
        feed_id="feed-1",
        market_db_path=market_db,
        regime_db_path=regime_db,
    )
    regime_scope = load_gate_scope(
        ["SOL", "ETH"], cutoff, "feed-1", mode="enforce", regime_db_path=regime_db
    )
    seen_assets = []
    seen_by_plugin = {}
    writes = []

    def run_plugin(_cutoff_id, snapshot):
        seen_assets.extend(asset for _, asset in evaluation_symbols(None, cutoff, snapshot))
        seen_by_plugin["failed-break-v3"] = list(seen_assets)
        return [{
            "candidate_id": "sol-candidate",
            "strategy_id": "failed-break-v3",
            "asset": "SOL",
            "direction": "long",
            "observed_at": cutoff.isoformat(),
            "valid_until": "2026-09-04T13:45:00+00:00",
            "entry_price": 100.0,
            "invalidation_price": 95.0,
            "targets": [110.0],
            "atr14_4h": 10.0,
            "data_freshness_seconds": 1.0,
        }]

    def run_mean_plugin(_cutoff_id, snapshot):
        seen_by_plugin["bb-rsi-meanrev-v1"] = [
            asset for _, asset in evaluation_symbols(None, cutoff, snapshot)
        ]
        return []

    try:
        monkeypatch.setattr(
            strategy_plugins,
            "subscription_assets",
            lambda _cutoff: (["SOL", "ETH"], {"feed_id": "feed-1"}),
        )
        strategy_plugins._REGISTRY["failed-break-v3"] = strategy_plugins.StrategyPlugin(
            "failed-break-v3", "test", (), (), run_plugin, "5m", "trend"
        )
        strategy_plugins._REGISTRY["bb-rsi-meanrev-v1"] = strategy_plugins.StrategyPlugin(
            "bb-rsi-meanrev-v1", "test", (), (), run_mean_plugin, "5m", "mean_reversion"
        )
        config.STRATEGY_ENABLED_IDS = ("failed-break-v3", "bb-rsi-meanrev-v1")
        config.STRATEGY_ACTIVE_IDS = ("failed-break-v3", "bb-rsi-meanrev-v1")
        strategy_plugins.write_event = lambda event: (writes.append(event) or (True, tmp_path / "event.json"))

        result = strategy_plugins._run_plugins_for_cutoff(
            analyst_db,
            "5m:2026-09-04T13:40:00Z",
            cutoff,
            False,
            snapshot={
                "eval_interval": "5m",
                "cutoff_id": "5m:2026-09-04T13:40:00Z",
                "feature_snapshots": {},
                "market_db_path": str(market_db),
                "now": cutoff,
                "subscription_symbols": [("SOLUSDT", "SOL"), ("ETHUSDT", "ETH")],
                "regime_scope": regime_scope,
            },
            market_db_path=market_db,
        )
    finally:
        strategy_plugins._REGISTRY.clear()
        strategy_plugins._REGISTRY.update(old_registry)
        config.STRATEGY_ENABLED_IDS = old_enabled
        config.STRATEGY_ACTIVE_IDS = old_active
        config.ANALYST_DB_PATH = old_analyst_db

    assert seen_assets == ["SOL"], result
    assert seen_by_plugin["failed-break-v3"] == ["SOL"]
    assert seen_by_plugin["bb-rsi-meanrev-v1"] == ["ETH"]
    assert result["failed-break-v3"]["emitted"] == 1
    assert result["bb-rsi-meanrev-v1"]["emitted"] == 0
    assert result["_attempted_symbols"] == 2
