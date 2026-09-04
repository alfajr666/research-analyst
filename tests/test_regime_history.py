from datetime import datetime, timezone

from regime_history import (
    REGIME_4H_BAR_VERSION,
    ensure_asset_ready,
    init_regime_history_schema,
    load_regime_4h_bars,
)


def _rows(cutoff, *, gap_at=None):
    through = cutoff.replace(hour=cutoff.hour - cutoff.hour % 4, minute=0, second=0, microsecond=0)
    rows = []
    for index in range(90):
        start_ms = int((through.timestamp() - (90 - index) * 4 * 3600) * 1000)
        if index == gap_at:
            continue
        close = 100.0 + index
        rows.append([start_ms, str(close - 1), str(close + 1), str(close - 2), str(close), "10", "0"])
    return rows


def test_direct_history_bootstrap_is_ready_and_idempotent(tmp_path):
    import config

    db = tmp_path / "regime.sqlite3"
    config.init_market_db(tmp_path / "market.sqlite3")
    conn = config.get_db_connection(db_path=db)
    init_regime_history_schema(conn)
    cutoff = datetime(2026, 9, 4, 13, 40, tzinfo=timezone.utc)
    calls = []

    def fetcher(asset, start_ms, end_ms):
        calls.append((asset, start_ms, end_ms))
        return _rows(cutoff)

    first = ensure_asset_ready(conn, "MARSCOIN", cutoff, fetcher=fetcher)
    second = ensure_asset_ready(conn, "MARSCOIN", cutoff, fetcher=fetcher)
    bars = load_regime_4h_bars(conn, "MARSCOIN", cutoff)

    assert first["status"] == "ready"
    assert second["status"] == "ready"
    assert len(calls) == 1
    assert bars.height == 84
    assert bars["source"].unique().to_list() == ["bybit_rest"]
    assert bars["bar_version"].unique().to_list() == [REGIME_4H_BAR_VERSION]


def test_direct_history_gap_blocks_only_the_asset(tmp_path):
    import config

    db = tmp_path / "regime.sqlite3"
    conn = config.get_db_connection(db_path=db)
    init_regime_history_schema(conn)
    cutoff = datetime(2026, 9, 4, 13, 40, tzinfo=timezone.utc)

    result = ensure_asset_ready(
        conn,
        "SOL",
        cutoff,
        fetcher=lambda _asset, _start, _end: _rows(cutoff, gap_at=50),
    )

    assert result["status"] == "retryable"
    assert result["missing_bars"] == 1
    row = conn.execute(
        "SELECT status, missing_bars FROM regime_4h_backfill_jobs WHERE asset = 'SOL'"
    ).fetchone()
    assert row == ("retryable", 1)


def test_duplicate_direct_candle_fails_readiness(tmp_path):
    import config

    db = tmp_path / "regime.sqlite3"
    conn = config.get_db_connection(db_path=db)
    init_regime_history_schema(conn)
    cutoff = datetime(2026, 9, 4, 13, 40, tzinfo=timezone.utc)
    rows = _rows(cutoff)
    rows.insert(10, rows[10].copy())

    result = ensure_asset_ready(
        conn, "ETH", cutoff, fetcher=lambda _asset, _start, _end: rows
    )

    assert result["status"] == "retryable"
    assert result["error"] == "RegimeHistoryError"
