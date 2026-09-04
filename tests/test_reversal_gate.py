from datetime import datetime, timedelta, timezone

from reversal_gate import reversal_gate


def _bars():
    start = datetime(2026, 9, 2, tzinfo=timezone.utc)
    bars = []
    for index in range(60):
        high = 100.0 + (index % 3)
        low = 90.0 - (index % 2)
        bars.append({"timestamp": start + timedelta(hours=index), "high": high, "low": low})
    bars[20]["high"] = 110.0
    bars[19]["high"] = 105.0
    bars[21]["high"] = 105.0
    bars[40]["high"] = 120.0
    bars[39]["high"] = 105.0
    bars[41]["high"] = 105.0
    return bars


def test_regular_bearish_divergence_and_adx_decay_activate_short_gate():
    rsi = [50.0] * 60
    rsi[20] = 65.0
    rsi[40] = 55.0
    adx = [20.0] * 60
    adx[42:55] = [30.0] * 13
    adx[-5:] = [30.0, 29.0, 28.0, 27.0, 26.0]

    result = reversal_gate("SOL", "2026-09-04T13:00:00Z", _bars(), adx, rsi)

    assert result["active"] is True
    assert result["direction"] == "short"
    assert result["divergence_type"] == "regular_bearish"
    assert result["recent_trend_detected"] is True
    assert result["adx_decay_detected"] is True


def test_hidden_divergence_does_not_activate_reversal():
    rsi = [50.0] * 60
    rsi[20] = 55.0
    rsi[40] = 65.0
    adx = [30.0] * 55 + [29.0, 28.0, 27.0, 26.0, 25.0]

    result = reversal_gate("SOL", "2026-09-04T13:00:00Z", _bars(), adx, rsi)

    assert result["active"] is False
    assert result["direction"] == "none"
    assert result["divergence_type"] == "none"


def test_opposing_regular_divergences_fail_closed_as_ambiguous():
    bars = []
    start = datetime(2026, 9, 2, tzinfo=timezone.utc)
    rsi = [50.0] * 60
    for index in range(60):
        bars.append({
            "timestamp": start + timedelta(hours=index),
            "high": 100.0,
            "low": 90.0,
        })
    bars[20]["high"] = 110.0
    bars[40]["high"] = 120.0
    rsi[20], rsi[40] = 65.0, 55.0
    bars[25]["low"] = 80.0
    bars[45]["low"] = 70.0
    rsi[25], rsi[45] = 35.0, 45.0
    adx = [30.0] * 55 + [29.0, 28.0, 27.0, 26.0, 25.0]

    result = reversal_gate("SOL", "2026-09-04T13:00:00Z", bars, adx, rsi)

    assert result["active"] is False
    assert result["divergence_type"] == "ambiguous"
    assert "ambiguous_divergence" in result["reasons"]
