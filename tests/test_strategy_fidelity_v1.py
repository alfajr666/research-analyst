from datetime import datetime, timedelta, timezone
import json

import polars as pl
import pytest

import config
from evaluation_trigger import publish
from intent_outbox import build_executor_intent
from strategy_v2_context import ema_series, resample_ohlcv, stoch_rsi, wilder_atr, wilder_rsi
from strategy_plugins import _REGISTRY


def _five_minute_bars(count=60, start=datetime(2026, 1, 1, tzinfo=timezone.utc)):
    timestamps = [start + timedelta(minutes=5 * index) for index in range(count)]
    closes = [100.0 + index for index in range(count)]
    return pl.DataFrame({
        "timestamp": timestamps,
        "open": closes,
        "high": [value + 1.0 for value in closes],
        "low": [value - 1.0 for value in closes],
        "close": closes,
        "volume": [1.0] * count,
    })


def test_resampling_uses_end_stamps_and_omits_partial_buckets():
    bars = _five_minute_bars(4, datetime(2026, 1, 1, 11, 45, tzinfo=timezone.utc))
    result = resample_ohlcv(bars, "15m")

    assert result["timestamp"].to_list() == [datetime(2026, 1, 1, 12, tzinfo=timezone.utc)]
    assert result["source_provenance"].to_list() == [["unknown"]]
    assert result["data_purity"].to_list() == ["unknown"]


def test_resampling_accepts_millisecond_bar_end_timestamps():
    start = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    bars = _five_minute_bars(48, start)
    bars = bars.with_columns(
        pl.col("timestamp").dt.offset_by("-1ms"),
    )

    result = resample_ohlcv(bars, "4h")

    assert result["timestamp"].to_list() == [datetime(2026, 1, 1, 4, tzinfo=timezone.utc)]


def test_resampling_rejects_conflicting_boundary_aliases():
    start = datetime(2026, 1, 1, 11, 50, tzinfo=timezone.utc)
    bars = _five_minute_bars(3, start).with_columns(
        pl.lit("bybit_ws").alias("source"),
        pl.col("timestamp").map_elements(lambda value: [f"obs-{value.isoformat()}"], return_dtype=pl.List(pl.String)).alias("source_observation_ids"),
    )
    alias = bars.row(2, named=True)
    alias["timestamp"] = alias["timestamp"] - timedelta(milliseconds=1)
    alias["close"] = 999.0
    bars = pl.concat([bars, pl.DataFrame([alias])], how="vertical_relaxed")

    result = resample_ohlcv(bars, "15m")

    assert result.is_empty()


def test_resampling_omits_incomplete_bucket_and_marks_mixed_purity():
    start = datetime(2026, 1, 1, 11, 50, tzinfo=timezone.utc)
    bars = _five_minute_bars(6, start).with_columns(
        pl.Series("source", ["bybit_ws", "bybit_ws", "backfill", "backfill", "backfill", "backfill"]),
        pl.Series("source_observation_ids", [[f"obs-{index}"] for index in range(6)], dtype=pl.List(pl.String)),
    ).filter(pl.col("timestamp") != datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc))

    result = resample_ohlcv(bars, "15m")

    assert result.height == 1
    assert result["timestamp"].to_list() == [datetime(2026, 1, 1, 12, tzinfo=timezone.utc)]
    assert result["source_provenance"].to_list() == [["backfill", "bybit_ws"]]
    assert result["data_purity"].to_list() == ["unknown"]


def test_wilder_indicators_have_declared_warmup_and_zero_stoch_denominator():
    bars = _five_minute_bars(20)
    rsi = wilder_rsi(bars["close"].to_list(), 14)
    assert all(value is None for value in rsi[:14])
    assert rsi[14] == 100.0
    assert wilder_atr(bars, 14) == 2.0

    raw, k, d = stoch_rsi([100.0] * 50, 14, 14, 3, 3)
    assert raw[-1] == 0.0
    assert k[-1] == 0.0
    assert d[-1] == 0.0


def test_polars_indicator_kernels_match_reference_recurrences():
    closes = [100.0, 101.5, 100.25, 102.0, 103.5, 102.75, 104.0, 105.25, 103.75, 106.0]
    span = 4
    expected_ema = [None] * (span - 1)
    expected_ema.append(sum(closes[:span]) / span)
    alpha = 2.0 / (span + 1.0)
    for value in closes[span:]:
        expected_ema.append(alpha * value + (1.0 - alpha) * expected_ema[-1])
    actual_ema = ema_series(closes, span)
    assert actual_ema[:span - 1] == expected_ema[:span - 1]
    assert actual_ema[span - 1:] == pytest.approx(expected_ema[span - 1:], abs=1e-12)

    length = 3
    gains = [max(closes[index] - closes[index - 1], 0.0) for index in range(1, len(closes))]
    losses = [max(closes[index - 1] - closes[index], 0.0) for index in range(1, len(closes))]
    gain = sum(gains[:length]) / length
    loss = sum(losses[:length]) / length
    expected_rsi = [None] * length
    for index in range(length, len(closes)):
        if index > length:
            gain = (gain * (length - 1) + gains[index - 1]) / length
            loss = (loss * (length - 1) + losses[index - 1]) / length
        expected_rsi.append(100.0 if loss == 0 and gain > 0 else 0.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss))
    actual_rsi = wilder_rsi(closes, length)
    assert actual_rsi[:length] == expected_rsi[:length]
    assert actual_rsi[length:] == pytest.approx(expected_rsi[length:], abs=1e-12)

    bars = pl.DataFrame({
        "high": [value + 1.0 for value in closes],
        "low": [value - 1.0 for value in closes],
        "close": closes,
    })
    true_ranges = [2.0]
    true_ranges.extend(
        max(bars["high"][index] - bars["low"][index],
            abs(bars["high"][index] - bars["close"][index - 1]),
            abs(bars["low"][index] - bars["close"][index - 1]))
        for index in range(1, len(closes))
    )
    atr = sum(true_ranges[:length]) / length
    for value in true_ranges[length:]:
        atr = (atr * (length - 1) + value) / length
    assert wilder_atr(bars, length) == pytest.approx(atr, abs=1e-12)


def test_all_registered_strategies_have_explicit_cadence_and_new_ids_are_registered():
    assert all(plugin.cadence in {"1m", "5m", "15m"} for plugin in _REGISTRY.values())
    for strategy_id in (
        "gold-trend-ema-bb-stoch-v1", "mtf-exhaustion-reversal-v1", "trend-wall-v1",
        "ema99-double-touch-stochrsi-state-v1",
        "ema7-26-cross-hammer-shooting-star-1h-adx-v1",
    ):
        assert strategy_id in _REGISTRY
        assert _REGISTRY[strategy_id].cadence == "5m"


def test_new_fundamo_routes_are_account_agnostic_in_candidate_and_fixed_downstream():
    for strategy_id in (
        "gold-trend-ema-bb-stoch-v1", "mtf-exhaustion-reversal-v1", "trend-wall-v1",
        "ema99-double-touch-stochrsi-state-v1",
        "ema7-26-cross-hammer-shooting-star-1h-adx-v1",
    ):
        event = {
            "strategy_id": strategy_id, "asset": "BTC", "direction": "long",
            "observed_at": "2099-01-01T00:00:00Z", "entry_price": 100.0,
            "invalidation_price": 95.0, "input_snapshot_id": "5m:2099-01-01T00:00:00Z",
        }
        intent = build_executor_intent(event, account_id="hyro")
        assert intent["account_id"] == "fundamo"
        assert all(key not in event for key in ("account_id", "exchange_id", "quantity", "leverage", "order_type"))


def test_one_minute_evaluation_trigger_keeps_its_interval_and_cutoff(tmp_path):
    cutoff = datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc)
    created, path = publish(cutoff, tmp_path, interval="1m")
    assert created
    payload = json.loads(path.read_text())
    assert payload["interval"] == "1m"
    assert payload["cutoff_at"] == "2026-01-01T12:01:00+00:00"
