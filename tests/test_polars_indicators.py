import polars as pl
import pytest

from polars_indicators import (
    dmi_adx,
    dmi_adx_series,
    rolling_rsi_series,
    rolling_stoch_rsi,
    vwma_last,
)


def _bars(count=90):
    return pl.DataFrame({
        "high": [100.0 + index * 0.7 + (index % 3) for index in range(count)],
        "low": [98.0 + index * 0.7 - (index % 2) for index in range(count)],
        "close": [99.0 + index * 0.7 + ((index % 4) - 1.5) for index in range(count)],
        "volume": [1.0 + index % 5 for index in range(count)],
    })


def test_aligned_adx_preserves_compact_contract_and_warmup_alignment():
    bars = _bars()
    compact, plus_di, minus_di = dmi_adx(bars, 14, 14)
    aligned, aligned_plus, aligned_minus = dmi_adx_series(bars, 14, 14)

    assert [value for value in aligned if value is not None] == pytest.approx(compact)
    assert aligned_plus == plus_di
    assert aligned_minus == minus_di
    assert all(value is None for value in aligned[:28])
    assert aligned[28] is not None


def test_rolling_rsi_matches_legacy_window_contract():
    closes = [100.0, 101.0, 99.5, 100.25, 102.0, 101.5, 103.0, 104.5, 103.25,
              104.0, 103.0, 105.0, 106.0, 105.5, 107.0, 106.5, 108.0]
    actual = rolling_rsi_series(closes, 5).to_list()
    expected = [None] * len(closes)
    for index in range(5, len(closes)):
        changes = [closes[j] - closes[j - 1] for j in range(index - 4, index + 1)]
        gains = sum(max(value, 0.0) for value in changes) / 5
        losses = sum(max(-value, 0.0) for value in changes) / 5
        expected[index] = 100.0 if losses == 0 else 100.0 - 100.0 / (1.0 + gains / losses)
    assert [value is None for value in actual] == [value is None for value in expected]
    assert [value for value in actual if value is not None] == pytest.approx(
        [value for value in expected if value is not None], abs=1e-12,
    )


def test_rolling_stoch_rsi_and_vwma_are_columnar_and_aligned():
    bars = _bars(50)
    raw, k, d = rolling_stoch_rsi(bars["close"].to_list(), 5, 5, 3, 3)
    assert len(raw) == len(k) == len(d) == bars.height
    assert raw[-1] is not None
    assert k[-1] is not None
    assert d[-1] is not None
    expected = sum(
        float(price) * float(volume)
        for price, volume in zip(bars["close"].tail(10), bars["volume"].tail(10))
    ) / float(bars["volume"].tail(10).sum())
    assert vwma_last(bars, 10) == pytest.approx(expected)
