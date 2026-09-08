from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import polars as pl

import config
from structural_stop import build_structural_contexts
from trade_admission import resolve


def test_declared_covered_structure_reaches_executor_selection():
    previous = {
        "STRUCTURAL_STOP_ADMISSION_ENABLED": config.STRUCTURAL_STOP_ADMISSION_ENABLED,
    }
    try:
        config.STRUCTURAL_STOP_ADMISSION_ENABLED = True
        observed = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)
        candidate = {
            "candidate_id": "candidate-1",
            "strategy_id": "trend-wall-v1",
            "asset": "BTC",
            "direction": "long",
            "entry_price": 100.0,
            "invalidation_price": 95.1,
            "targets": [110.0],
            "atr14_4h": 10.0,
                "valid_until": datetime(2099, 9, 3, tzinfo=timezone.utc),
            "observed_at": observed,
            "data_freshness_seconds": 60.0,
            "structural_context": {
                "asset": "BTC",
                "cutoff": observed,
                "zones": [{
                    "zone_id": "zone-1", "asset": "BTC", "type": "order_block", "timeframe": "4h",
                    "direction": "bullish", "low": 96.0, "high": 97.0,
                        "state": "active", "created_at": datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
                        "confirmed_at": datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
                    "coverage_status": "covered", "source_evidence_ids": ["obs-1", "obs-2"],
                }],
                "atr_by_timeframe": {"4h": 1.0},
                "atr_source_bar_ids": {"4h": ["obs-1", "obs-2"]},
            },
        }

        result = resolve(
            [candidate],
            structural_contexts={"BTC": candidate["structural_context"]},
            effective_universe=["BTC"],
        )

        assert result["selected_candidate_ids"] == ["candidate-1"]
        selected = result["results"][0]
        assert selected["status"] == "selected_for_executor"
        assert selected["structural_stop_gate"] == "pass"
        assert selected["selected_zone_id"] == "zone-1"
    finally:
        for name, value in previous.items():
            setattr(config, name, value)


def test_uncovered_structure_is_rejected_before_scoring():
    previous = {
        "STRUCTURAL_STOP_ADMISSION_ENABLED": config.STRUCTURAL_STOP_ADMISSION_ENABLED,
    }
    try:
        config.STRUCTURAL_STOP_ADMISSION_ENABLED = True
        observed = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)
        candidate = {
            "candidate_id": "candidate-2",
            "strategy_id": "trend-wall-v1",
            "asset": "BTC",
            "direction": "long",
            "entry_price": 100.0,
            "invalidation_price": 94.0,
            "targets": [110.0],
            "atr14_4h": 10.0,
            "valid_until": datetime(2099, 9, 3, tzinfo=timezone.utc),
            "observed_at": observed,
            "data_freshness_seconds": 60.0,
            "structural_context": {
            "asset": "BTC", "cutoff": observed, "zones": [], "atr_by_timeframe": {"4h": 1.0},
            },
        }

        result = resolve(
            [candidate],
            structural_contexts={"BTC": candidate["structural_context"]},
            effective_universe=["BTC"],
        )

        assert result["selected_candidate_ids"] == []
        rejected = result["results"][0]
        assert rejected["status"] == "hard_gate_failed"
        assert rejected["score_status"] == "not_evaluated"
        assert rejected["structural_stop_gate"] == "fail"
    finally:
        for name, value in previous.items():
            setattr(config, name, value)


def test_admission_builds_context_only_for_candidate_assets():
    observed = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)

    def bars(count, hours, end=observed):
        through = end.replace(hour=end.hour - end.hour % (hours // 1), minute=0, second=0, microsecond=0)
        return pl.DataFrame({
            "timestamp": [through - timedelta(hours=hours * (count - index - 1)) for index in range(count)],
            "open": [100.0 + index for index in range(count)],
            "high": [101.0 + index for index in range(count)],
            "low": [99.0 + index for index in range(count)],
            "close": [100.0 + index for index in range(count)],
            "volume": [1.0] * count,
            "bar_id": [f"bar-{hours}-{index}" for index in range(count)],
            "source_observation_ids": [[f"bar-{hours}-{index}"] for index in range(count)],
        })

    context_zone = {
        "type": "order_block", "timeframe": "4h", "direction": "bullish", "low": 98.0, "high": 99.0,
        "state": "active", "created_at": observed - timedelta(hours=4),
        "source_evidence_ids": ["bar-4h-1"],
    }
    candidate = {
        "candidate_id": "candidate-1", "asset": "BTC", "direction": "long",
        "entry_price": 100.0, "invalidation_price": 97.0, "targets": [110.0],
        "valid_until": "2099-09-03T00:00:00+00:00", "data_freshness_seconds": 1.0,
    }
    connection = MagicMock()
    with patch("structural_stop.config.get_db_connection", return_value=connection), \
            patch("regime_history.load_regime_4h_bars", return_value=bars(57, 4)) as load_4h, \
            patch("regime_history.load_regime_1h_bars", return_value=bars(57, 1)) as load_1h, \
            patch("structure_zones.detect_fvg", return_value=[context_zone]), \
            patch("structure_zones.detect_order_blocks", return_value=[]):
        contexts = build_structural_contexts([candidate], observed)

    assert set(contexts) == {"BTC"}
    assert load_4h.call_args.args[1] == "BTC"
    assert load_1h.call_args.args[1] == "BTC"
    decision = resolve([candidate], structural_contexts=contexts, now=observed)
    assert decision["selected_candidate_ids"] == ["candidate-1"]


def test_stale_direct_history_cannot_supply_structural_context():
    observed = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)
    stale = pl.DataFrame({
        "timestamp": [observed - timedelta(hours=4 * (56 - index + 2)) for index in range(57)],
        "open": [100.0 + index for index in range(57)],
        "high": [101.0 + index for index in range(57)],
        "low": [99.0 + index for index in range(57)],
        "close": [100.0 + index for index in range(57)],
        "volume": [1.0] * 57,
        "bar_id": [f"stale-{index}" for index in range(57)],
        "source_observation_ids": [[f"stale-{index}"] for index in range(57)],
    })
    candidate = {"candidate_id": "candidate-stale", "asset": "BTC"}
    connection = MagicMock()
    with patch("structural_stop.config.get_db_connection", return_value=connection), \
            patch("regime_history.load_regime_4h_bars", return_value=stale), \
            patch("regime_history.load_regime_1h_bars", return_value=stale):
        contexts = build_structural_contexts([candidate], observed)

    assert contexts["BTC"]["coverage_status"] == {"4h": "incomplete", "1h": "incomplete"}
    assert contexts["BTC"]["atr_by_timeframe"] == {}


def test_enabled_15m_context_uses_canonical_market_frame_and_provenance(monkeypatch):
    observed = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)
    fifteen_minute_bars = pl.DataFrame({
        "timestamp": [observed.replace(minute=0) - timedelta(minutes=15 * (56 - index)) for index in range(57)],
        "open": [100.0 + index for index in range(57)],
        "high": [101.0 + index for index in range(57)],
        "low": [99.0 + index for index in range(57)],
        "close": [100.0 + index for index in range(57)],
        "volume": [1.0] * 57,
        "source": ["bybit_ws"] * 57,
        "source_provenance": [["bybit_ws"]] * 57,
        "source_observation_ids": [[f"5m-{index}-a", f"5m-{index}-b"] for index in range(57)],
    })
    candidate = {"candidate_id": "candidate-15m", "asset": "BTC"}
    zone = {
        "type": "fvg", "timeframe": "15m", "direction": "bullish", "low": 98.0, "high": 99.0,
        "state": "active", "created_at": observed - timedelta(minutes=15),
        "source_evidence_ids": ["5m-55-a", "5m-56-a"],
    }
    connection = MagicMock()
    monkeypatch.setattr(config, "STRUCTURAL_15M_ZONES_ENABLED", True)

    def detect_fvg(_bars, *, tf, **_kwargs):
        return [zone] if tf == "15m" else []

    with patch("structural_stop.config.get_db_connection", return_value=connection), \
            patch("regime_history.load_regime_4h_bars", return_value=pl.DataFrame()), \
            patch("regime_history.load_regime_1h_bars", return_value=pl.DataFrame()), \
            patch("strategy_v2_context.load_bars_for_interval", return_value=fifteen_minute_bars) as load_15m, \
            patch("strategy_v2_context.wilder_atr", return_value=1.0), \
            patch("structure_zones.detect_fvg", side_effect=detect_fvg), \
            patch("structure_zones.detect_order_blocks", return_value=[]):
        contexts = build_structural_contexts([candidate], observed, market_db_path="market.db")

    assert load_15m.call_args.args[1:4] == ("BTC", "15m", observed)
    context = contexts["BTC"]
    assert context["coverage_status"]["15m"] == "covered"
    assert context["atr_source_bar_ids"]["15m"] == sorted(
        f"5m-{index}-{suffix}" for index in range(57) for suffix in ("a", "b")
    )
    assert context["zones"][0]["timeframe"] == "15m"
    assert context["zones"][0]["source_mode"] == "market_5m_resampled"


def test_enabled_15m_context_fails_closed_on_a_gap(monkeypatch):
    observed = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)
    timestamps = [observed.replace(minute=0) - timedelta(minutes=15 * (56 - index)) for index in range(57)]
    timestamps[30] += timedelta(minutes=15)
    bars = pl.DataFrame({
        "timestamp": timestamps,
        "open": [100.0] * 57,
        "high": [101.0] * 57,
        "low": [99.0] * 57,
        "close": [100.0] * 57,
        "source": ["bybit_ws"] * 57,
        "source_provenance": [["bybit_ws"]] * 57,
        "source_observation_ids": [[f"5m-{index}"] for index in range(57)],
    })
    connection = MagicMock()
    monkeypatch.setattr(config, "STRUCTURAL_15M_ZONES_ENABLED", True)

    with patch("structural_stop.config.get_db_connection", return_value=connection), \
            patch("regime_history.load_regime_4h_bars", return_value=pl.DataFrame()), \
            patch("regime_history.load_regime_1h_bars", return_value=pl.DataFrame()), \
            patch("strategy_v2_context.load_bars_for_interval", return_value=bars):
        contexts = build_structural_contexts([{"asset": "BTC"}], observed)

    assert contexts["BTC"]["coverage_status"]["15m"] == "incomplete"
    assert contexts["BTC"]["atr_by_timeframe"].get("15m") is None


def test_disabled_15m_context_does_not_open_or_mention_market_fallback(monkeypatch):
    observed = datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)
    connection = MagicMock()
    monkeypatch.setattr(config, "STRUCTURAL_15M_ZONES_ENABLED", False)

    with patch("structural_stop.config.get_db_connection", return_value=connection), \
            patch("regime_history.load_regime_4h_bars", return_value=pl.DataFrame()), \
            patch("regime_history.load_regime_1h_bars", return_value=pl.DataFrame()), \
            patch("strategy_v2_context.load_bars_for_interval") as load_15m:
        contexts = build_structural_contexts([{"asset": "BTC"}], observed, market_db_path="market.db")

    load_15m.assert_not_called()
    context = contexts["BTC"]
    assert "15m" not in context["coverage_status"]
    assert "15m" not in context["atr_by_timeframe"]
    assert "15m" not in context["timeframe_provenance"]
