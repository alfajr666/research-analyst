from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from strategy_features import build_feature_frame, cached_feature_frame
from strategy_v2_context import stoch_rsi, wilder_atr, wilder_rsi


def _bars(count=40):
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    closes = [100.0 + index * 0.4 + (index % 3) for index in range(count)]
    return pl.DataFrame({
        "timestamp": [start + timedelta(minutes=5 * index) for index in range(count)],
        "open": closes,
        "high": [value + 1.0 for value in closes],
        "low": [value - 1.0 for value in closes],
        "close": closes,
        "volume": [1.0] * count,
    })


def test_feature_frame_preserves_shared_indicator_outputs():
    bars = _bars()
    frame = build_feature_frame(
        bars,
        ema={"ema_9": 9},
        rsi={"rsi_14": 14},
        stoch={"stoch": (14, 14, 3, 3)},
        atr={"atr_14": 14},
        bollinger={"bb": (20, 2.0)},
    )

    raw, k, d = stoch_rsi(bars["close"].to_list(), 14, 14, 3, 3)
    assert frame["ema_9"].to_list()[-1] is not None
    assert frame["rsi_14"].to_list() == wilder_rsi(bars["close"].to_list(), 14)
    assert frame["stoch_raw"].to_list() == raw
    assert frame["stoch_k"].to_list() == k
    assert frame["stoch_d"].to_list() == d
    assert frame["atr_14"][-1] == pytest.approx(wilder_atr(bars, 14), abs=1e-12)
    window = bars["close"].tail(20).to_list()
    middle = sum(window) / 20
    deviation = (sum((value - middle) ** 2 for value in window) / 20) ** 0.5
    assert frame["bb_middle"][-1] == pytest.approx(middle)
    assert frame["bb_lower"][-1] == pytest.approx(middle - 2.0 * deviation)
    assert frame["bb_upper"][-1] == pytest.approx(middle + 2.0 * deviation)


def test_feature_frame_cache_is_invocation_scoped_and_reuses_frame():
    bars = _bars()
    snapshot = {}
    calls = []

    def builder(frame):
        calls.append(frame)
        return build_feature_frame(frame, ema={"ema_9": 9})

    first = cached_feature_frame(snapshot, "asset:5m", bars, builder)
    second = cached_feature_frame(snapshot, "asset:5m", bars, builder)

    assert first is second
    assert len(calls) == 1
    assert snapshot["_strategy_feature_cache"]["asset:5m"] is first
