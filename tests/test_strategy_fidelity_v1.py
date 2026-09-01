from datetime import datetime, timedelta, timezone
import json

import polars as pl

import config
from evaluation_trigger import publish
from intent_outbox import build_executor_intent
from strategy_v2_context import resample_ohlcv, stoch_rsi, wilder_atr, wilder_rsi
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


def test_all_registered_strategies_have_explicit_cadence_and_new_ids_are_registered():
    assert all(plugin.cadence in {"1m", "5m", "15m"} for plugin in _REGISTRY.values())
    for strategy_id in ("gold-trend-ema-bb-stoch-v1", "mtf-exhaustion-reversal-v1", "trend-wall-v1"):
        assert strategy_id in _REGISTRY
        assert _REGISTRY[strategy_id].cadence == "5m"


def test_new_fundamo_routes_are_account_agnostic_in_candidate_and_fixed_downstream():
    for strategy_id in ("gold-trend-ema-bb-stoch-v1", "mtf-exhaustion-reversal-v1", "trend-wall-v1"):
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
