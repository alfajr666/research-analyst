from unittest.mock import patch

from trade_admission import admit_symbol_account, resolve


def _candidate(strategy_id, asset, account_id="hyro"):
    return {
        "candidate_id": f"{strategy_id}-{asset}",
        "strategy_id": strategy_id,
        "asset": asset,
        "account_id": account_id,
        "direction": "long",
        "entry_price": 100.0,
        "invalidation_price": 95.0,
        "targets": [110.0],
        "atr14_4h": 10.0,
        "valid_until": "2099-01-01T00:05:00+00:00",
        "data_freshness_seconds": 1.0,
        "structural_context": {
            "asset": "BTC",
            "cutoff": "2026-01-01T00:00:00+00:00",
            "zones": [{"zone_id": "zone-1", "asset": "BTC", "type": "order_block", "timeframe": "4h",
                       "direction": "bullish", "low": 97.0, "high": 98.0,
                       "state": "active", "created_at": "2025-12-31T20:00:00+00:00", "confirmed_at": "2025-12-31T20:00:00+00:00",
                       "coverage_status": "covered", "source_evidence_ids": ["bar-1"]}],
             "atr_by_timeframe": {"4h": 1.0},
             "atr_source_bar_ids": {"4h": ["bar-1"]},
        },
    }


def test_candidate_account_cannot_override_compact_route():
    result = admit_symbol_account(_candidate("bb-rsi-meanrev-v1", "SOLUSDT", "fundamo"))
    assert result["symbol_account_gate"] == "fail"
    assert result["canonical_asset"] == "SOL"
    assert result["resolved_account"] == "hyro"
    assert result["policy_version"] == "symbol-account-policy-v2"


def test_compact_btc_and_fundamo_watchlist_symbol_passes():
    assert admit_symbol_account(_candidate("failed-break-v3", "BTC"))["symbol_account_gate"] == "pass"
    result = admit_symbol_account(
        _candidate("ema20-pullback-h4-trend-v1", "SOL", "hyro"),
        effective_universe=["BTC", "SOL"],
        effective_universe_version="universe-test",
    )
    assert result["symbol_account_gate"] == "pass"
    assert result["resolved_account"] == "fundamo"
    assert result["effective_universe_version"] == "universe-test"


def test_fundamo_requires_effective_watchlist_scope():
    result = admit_symbol_account(_candidate("ema20-pullback-h4-trend-v1", "SOL"))
    assert result["symbol_account_gate"] == "fail"
    assert result["rejection_reason"] == "effective watchlist universe is unavailable"


def test_all_hyro_routes_require_permanent_assets():
    result = admit_symbol_account(_candidate("unclassified-strategy", "SOL"))
    assert result["symbol_account_gate"] == "fail"
    assert result["rejection_reason"] == "Hyro policy permits only BTC, ETH, PAXG, QQQ"


def test_symbol_rejection_happens_before_score():
    candidates = [_candidate("bb-rsi-meanrev-v1", "SOL"), _candidate("bb-rsi-meanrev-v1", "BTC")]
    with patch("trade_admission.score", wraps=lambda candidate: {"score": 0.0}) as score:
        decision = resolve(candidates)
    assert score.call_count == 1
    rejected = next(item for item in decision["results"] if item["candidate_id"].endswith("SOL"))
    assert rejected["hard_gate"] == "fail"
    assert rejected["score_status"] == "not_evaluated"
    assert "symbol-account policy" in rejected["hard_gate_reasons"][0]
