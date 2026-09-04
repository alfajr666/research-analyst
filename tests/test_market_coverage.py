from datetime import datetime, timedelta, timezone

from market_coverage import assess_coverage, validate_ohlcv_payload


def _rows(start, count, *, source="bybit_ws"):
    return [
        {
            "source_end": start + timedelta(minutes=5 * index),
            "source": source,
            "purity": "pure_ws",
        }
        for index in range(count)
    ]


def test_complete_asset_window_is_covered():
    cutoff = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)

    result = assess_coverage(
        _rows(cutoff - timedelta(minutes=10), 3),
        asset="BTC",
        interval="5m",
        cutoff=cutoff,
        expected_bars=3,
    )

    assert result.status == "covered"
    assert result.observed_bars == 3
    assert result.missing_ends == ()
    assert result.freshness_seconds == 0


def test_missing_internal_bar_is_incomplete():
    cutoff = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)
    rows = _rows(cutoff - timedelta(minutes=10), 3)
    rows.pop(1)

    result = assess_coverage(
        rows,
        asset="BTC",
        interval="5m",
        cutoff=cutoff,
        expected_bars=3,
    )

    assert result.status == "incomplete"
    assert result.missing_ends == (datetime(2026, 9, 1, 12, tzinfo=timezone.utc),)


def test_stale_asset_is_not_rescued_by_other_asset_data():
    cutoff = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)

    result = assess_coverage(
        _rows(cutoff - timedelta(minutes=30), 3),
        asset="SOL",
        interval="5m",
        cutoff=cutoff,
        expected_bars=3,
        max_age_seconds=600,
    )

    assert result.status == "stale"
    assert result.asset == "SOL"


def test_duplicate_timestamp_is_not_covered():
    cutoff = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)
    rows = _rows(cutoff - timedelta(minutes=10), 3)
    rows.append(dict(rows[-1]))

    result = assess_coverage(
        rows,
        asset="BTC",
        interval="5m",
        cutoff=cutoff,
        expected_bars=3,
    )

    assert result.status == "incomplete"
    assert result.duplicate_ends == (cutoff,)


def test_invalid_ohlcv_payload_is_not_valid_market_data():
    assert not validate_ohlcv_payload({"open": 10, "high": 9, "low": 8, "close": 8.5})
    assert not validate_ohlcv_payload({"open": 10, "high": 11, "low": 8, "close": float("nan")})


def test_exchange_boundary_minus_one_millisecond_counts_as_closed_bar():
    cutoff = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)
    rows = [{
        "source_end": datetime(2026, 9, 1, 12, 4, 59, 999000, tzinfo=timezone.utc),
        "source": "bybit_ws",
        "purity": "pure_ws",
    }]

    result = assess_coverage(rows, asset="BTC", interval="5m", cutoff=cutoff, expected_bars=1)

    assert result.status == "covered"


def test_backfill_and_stream_boundary_alias_is_not_a_duplicate():
    cutoff = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)
    rows = [
        {
            "source_end": cutoff,
            "source": "bybit_ws",
            "purity": "pure_ws",
        },
        {
            "source_end": cutoff - timedelta(milliseconds=1),
            "source": "bybit_ws",
            "purity": "pure_ws",
        },
    ]

    result = assess_coverage(rows, asset="BTC", interval="5m", cutoff=cutoff, expected_bars=1)

    assert result.status == "covered"
    assert result.duplicate_ends == ()
