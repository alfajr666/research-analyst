from datetime import datetime, timezone
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "research_analyst"))
import open_interest
from trade_quality import score_candidate


def test_oi_observations_are_cutoff_bound_and_deduplicated(tmp_path):
    import sqlite3

    db = sqlite3.connect(tmp_path / "analyst.sqlite3")
    open_interest.init_schema(db)
    rows = [
        {"venue": "bybit", "native_symbol": "BTCUSDT", "asset": "BTC",
         "interval": "5m", "source_at": "2026-09-18T00:00:00Z",
         "retrieved_at": "2026-09-18T00:01:00Z", "open_interest": 100.0,
         "source_version": "test"},
        {"venue": "bybit", "native_symbol": "BTCUSDT", "asset": "BTC",
         "interval": "5m", "source_at": "2026-09-18T00:05:00Z",
         "retrieved_at": "2026-09-18T00:06:00Z", "open_interest": 110.0,
         "source_version": "test"},
    ]
    assert open_interest.insert_observations(db, rows) == 2
    assert open_interest.insert_observations(db, rows) == 0
    out = open_interest.load_observations(
        db, "bybit", "BTCUSDT", "5m", "2026-09-18T00:04:59Z", limit=32)
    assert [row["open_interest"] for row in out] == [100.0]


def test_oi_component_scores_directional_participation_and_missing_neutral():
    candidate = {"direction": "long", "market_family": "trend"}
    rows = [
        {"source_at": f"2026-09-17T{i:02d}:00:00Z", "open_interest": 100.0 + i}
        for i in range(32)
    ]
    result = open_interest.oi_participation_score(candidate, rows, [100.0, 101.0, 102.0])
    assert result["status"] == "support"
    assert result["value"] > 0.5
    missing = open_interest.oi_participation_score(candidate, [], [100.0])
    assert missing["value"] == 0.5 and missing["status"] == "unavailable"


def test_trade_quality_exposes_neutral_oi_component_without_changing_score():
    candidate = {
        "candidate_id": "oi-test", "direction": "long", "market_family": "trend",
        "observed_at": "2026-09-18T00:00:00+00:00", "valid_until": "2026-09-18T00:05:00+00:00",
        "entry_price": 100.0, "invalidation_price": 97.0, "targets": [110.0],
        "data_freshness_seconds": 1.0, "effective_universe_assets": ["BTC"],
        "effective_universe_version": "v1", "_execution_account": "fundamo",
    }
    result = score_candidate(candidate, regime_mode="off")
    assert result["components"]["oi_participation"]["value"] == 0.5
    assert result["components"]["oi_participation"]["status"] == "unavailable"
