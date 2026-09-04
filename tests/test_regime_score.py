from datetime import datetime, timedelta, timezone

import polars as pl

from regime_score import market_data_from_bars, regime_score, regime_score_for_asset
from strategy_v2_context import _asset_from_symbol


def _market(**overrides):
    data = {
        "adx_1h": 25.0,
        "adx_4h": 27.0,
        "adx_1h_previous": 25.0,
        "adx_4h_previous": 27.0,
        "realized_vol_recent": 1.0,
        "realized_vol_prior": 1.0,
        "btc_spx_correlation": 0.1,
    }
    data.update(overrides)
    return data


def test_strong_agreeing_adx_prefers_trend():
    result = regime_score(
        datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc),
        _market(adx_1h=42.0, adx_4h=46.0, adx_1h_previous=40.0, adx_4h_previous=44.0),
    )

    assert result["trend_weight"] > result["mean_reversion_weight"]
    assert result["reversal_weight"] == 0.0
    assert result["confidence"] > 0.5
    assert result["components"]["tf_agreement"] > 0.9


def test_weak_agreeing_adx_prefers_mean_reversion():
    result = regime_score(
        datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc),
        _market(adx_1h=11.0, adx_4h=13.0),
    )

    assert result["mean_reversion_weight"] > result["trend_weight"]
    assert result["reversal_weight"] == 0.0


def test_fast_trend_decay_adds_reversal_weight():
    result = regime_score(
        datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc),
        _market(adx_1h=20.0, adx_4h=22.0, adx_1h_previous=42.0, adx_4h_previous=44.0),
    )

    assert result["reversal_weight"] > 0.0
    assert result["components"]["trend_decay"] > 0.1


def test_transition_discount_is_continuous_at_europe_us_handoff():
    before = regime_score(
        datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc), _market()
    )
    near = regime_score(
        datetime(2026, 9, 4, 12, 30, tzinfo=timezone.utc), _market()
    )

    assert before["components"]["transition_discount"] == 1.0
    assert 0.0 < near["components"]["transition_discount"] < 1.0
    assert near["confidence"] < before["confidence"]


def test_missing_inputs_fail_closed_to_unknown_score():
    result = regime_score(datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc), {})

    assert result["status"] == "insufficient_data"
    assert result["confidence"] == 0.0
    assert result["trend_weight"] == 0.0
    assert result["mean_reversion_weight"] == 0.0
    assert result["reversal_weight"] == 0.0


def test_empty_polars_frames_fail_closed_without_column_error():
    data = market_data_from_bars(pl.DataFrame(), pl.DataFrame(), pl.DataFrame())

    assert data["adx_1h"] is None
    assert data["adx_4h"] is None
    assert data["realized_vol_recent"] is None
    assert data["realized_vol_prior"] is None


def test_asset_mapping_preserves_bare_and_usdt_asset_names():
    assert _asset_from_symbol("ANKR") == "ANKR"
    assert _asset_from_symbol("ANKRUSDT") == "ANKR"
    assert _asset_from_symbol("MARSCOINUSDT") == "MARSCOIN"
    assert _asset_from_symbol("PYTHUSDT") == "PYTH"


def test_market_data_adapter_uses_one_asset_and_no_cross_asset_proxy():
    def bars(count):
        return [
            {"high": 100.0 + index + 1, "low": 100.0 + index - 1,
             "close": 100.0 + index}
            for index in range(count)
        ]

    data = market_data_from_bars(bars(80), bars(80), bars(30))

    assert data["adx_1h"] is not None
    assert data["adx_4h"] is not None
    assert data["realized_vol_recent"] is not None
    assert "btc_spx_correlation" not in data


def test_score_without_regime_history_connection_fails_closed(monkeypatch):
    cutoff = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "strategy_v2_context.load_bars_for_interval",
        lambda *_args: pl.DataFrame(),
    )
    result = regime_score_for_asset(object(), "ETH", cutoff)

    assert result["status"] == "insufficient_data"
    assert result["regime_history"]["reason"] == "regime_history_connection_missing"


def test_rotated_asset_adapter_loads_each_timeframe_for_the_requested_asset(monkeypatch, tmp_path):
    import config
    from regime_history import ensure_asset_ready, init_regime_history_schema

    calls = []
    cutoff = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

    def loader(_conn, asset, interval, _cutoff):
        calls.append((asset, interval))
        rows = []
        for index in range(2200):
            close = 100.0 + index
            rows.append({
                "timestamp": cutoff - timedelta(minutes=5 * (2200 - index - 1)),
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 1.0,
            })
        return pl.DataFrame(rows)

    # The public adapter delegates to the canonical loader, so replacing the
    # module import at the seam verifies rotation is handled per asset.
    import strategy_v2_context
    monkeypatch.setattr(strategy_v2_context, "load_bars_for_interval", loader)
    regime_conn = config.get_db_connection(db_path=tmp_path / "regime.sqlite3")
    init_regime_history_schema(regime_conn)
    through = cutoff.replace(hour=cutoff.hour - cutoff.hour % 4, minute=0, second=0, microsecond=0)
    direct_rows = []
    for index in range(300):
        start = through - timedelta(hours=(300 - index) * 4)
        close = 100.0 + index
        direct_rows.append([
            int(start.timestamp() * 1000), str(close - 1), str(close + 1),
            str(close - 2), str(close), "1", "0",
        ])
    one_hour_through = cutoff.replace(minute=0, second=0, microsecond=0)
    direct_1h_rows = []
    for index in range(360):
        start = one_hour_through - timedelta(hours=360 - index)
        close = 200.0 + index
        direct_1h_rows.append([
            int(start.timestamp() * 1000), str(close - 1), str(close + 1),
            str(close - 2), str(close), "1", "0",
        ])
    ensure_asset_ready(
        regime_conn, "SOL", cutoff,
        fetcher=lambda _asset, _start, _end: direct_rows,
    )
    result = regime_score_for_asset(
        object(), "SOL", cutoff, regime_conn=regime_conn,
        history_1h_fetcher=lambda _asset, _start, _end: direct_1h_rows,
    )
    regime_conn.close()

    assert calls == [("SOL", "5m")]
    assert result["asset"] == "SOL"
    assert result["status"] == "ok"
