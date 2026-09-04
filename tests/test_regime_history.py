from datetime import datetime, timezone

from regime_history import (
    REGIME_4H_BAR_VERSION,
    ensure_asset_ready,
    ensure_asset_1h_ready,
    fetch_bybit_1h,
    init_regime_history_schema,
    load_regime_4h_bars,
    load_regime_1h_bars,
)


def _rows(cutoff, *, gap_at=None, interval_hours=4, count=300):
    through = cutoff.replace(
        hour=cutoff.hour - cutoff.hour % interval_hours,
        minute=0,
        second=0,
        microsecond=0,
    )
    rows = []
    for index in range(count):
        start_ms = int((through.timestamp() - (count - index) * interval_hours * 3600) * 1000)
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


def test_direct_1h_history_bootstrap_is_ready_and_loadable(tmp_path):
    import config

    db = tmp_path / "regime.sqlite3"
    conn = config.get_db_connection(db_path=db)
    init_regime_history_schema(conn)
    cutoff = datetime(2026, 9, 4, 13, 40, tzinfo=timezone.utc)

    result = ensure_asset_1h_ready(
        conn,
        "VELVET",
        cutoff,
        fetcher=lambda _asset, _start, _end: _rows(
            cutoff, interval_hours=1, count=360
        ),
    )
    bars = load_regime_1h_bars(conn, "VELVET", cutoff)

    assert result["status"] == "ready"
    assert result["covered_bars"] == 336
    assert result["missing_bars"] == 0
    assert bars.height == 72
    assert bars["source"].unique().to_list() == ["bybit_rest"]
    assert bars["bar_version"].unique().to_list() == ["bybit-rest-1h-v1"]


def test_direct_1h_history_gap_is_retryable(tmp_path):
    import config

    db = tmp_path / "regime.sqlite3"
    conn = config.get_db_connection(db_path=db)
    init_regime_history_schema(conn)
    cutoff = datetime(2026, 9, 4, 13, 40, tzinfo=timezone.utc)

    result = ensure_asset_1h_ready(
        conn,
        "VELVET",
        cutoff,
        fetcher=lambda _asset, _start, _end: _rows(
            cutoff, interval_hours=1, count=360, gap_at=40
        ),
    )

    assert result["status"] == "retryable"
    assert result["missing_bars"] == 1


def test_direct_1h_fetch_uses_bybit_hourly_interval(monkeypatch):
    import regime_history

    requests = []

    class Response:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {"retCode": 0, "result": {"list": []}}

    def get(_url, *, params, timeout):
        requests.append((params, timeout))
        return Response()

    monkeypatch.setattr(regime_history.httpx, "get", get)

    assert fetch_bybit_1h("VELVET", 1, 2) == []
    assert requests[0][0]["symbol"] == "VELVETUSDT"
    assert requests[0][0]["interval"] == "60"


def test_direct_history_fetch_paginates_beyond_provider_limit(monkeypatch):
    import regime_history

    requests = []

    class Response:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            page = list(range(200)) if len(requests) == 1 else list(range(40))
            start = 1000 if len(requests) == 1 else 500
            return {"retCode": 0, "result": {"list": [[start - index, "1", "2", "0.5", "1", "1"] for index in page]}}

    def get(_url, *, params, timeout):
        requests.append((params, timeout))
        return Response()

    monkeypatch.setattr(regime_history.httpx, "get", get)

    rows = regime_history.fetch_bybit_1h("VELVET", 0, 2000)

    assert len(rows) == 240
    assert len(requests) == 2
    assert requests[1][0]["end"] < requests[0][0]["end"]


def test_duplicate_direct_candle_fails_readiness(tmp_path):
    import config

    db = tmp_path / "regime.sqlite3"
    conn = config.get_db_connection(db_path=db)
    init_regime_history_schema(conn)
    cutoff = datetime(2026, 9, 4, 13, 40, tzinfo=timezone.utc)
    rows = _rows(cutoff)
    rows.insert(120, rows[120].copy())

    result = ensure_asset_ready(
        conn, "ETH", cutoff, fetcher=lambda _asset, _start, _end: rows
    )

    assert result["status"] == "retryable"
    assert result["error"] == "RegimeHistoryError"
