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
            "cutoff": "2026-01-01T00:00:00+00:00",
            "zones": [{"zone_id": "zone-1", "type": "order_block", "timeframe": "4h",
                       "direction": "bullish", "low": 97.0, "high": 98.0,
                       "state": "active", "created_at": "2025-12-31T20:00:00+00:00",
                       "coverage_status": "covered", "source_evidence_ids": ["bar-1"]}],
            "atr_by_timeframe": {"4h": 1.0},
        },
    }


def test_candidate_account_cannot_override_compact_route():
    result = admit_symbol_account(_candidate("bb-rsi-meanrev-v1", "SOLUSDT", "fundamo"))
    assert result["symbol_account_gate"] == "fail"
    assert result["canonical_asset"] == "SOL"
    assert result["resolved_account"] == "hyro"
    assert result["policy_version"] == "symbol-account-policy-v1"


def test_compact_btc_and_fundamo_approved_symbol_pass():
    assert admit_symbol_account(_candidate("failed-break-v3", "BTC"))["symbol_account_gate"] == "pass"
    with patch("config.load_static_symbols", return_value=["SOL"]):
        result = admit_symbol_account(_candidate("dual-zone-follower-v2", "SOL", "hyro"))
    assert result["symbol_account_gate"] == "pass"
    assert result["resolved_account"] == "fundamo"


def test_symbol_rejection_happens_before_score():
    candidates = [_candidate("bb-rsi-meanrev-v1", "SOL"), _candidate("bb-rsi-meanrev-v1", "BTC")]
    with patch("trade_admission.score", wraps=lambda candidate: {"score": 0.0}) as score:
        decision = resolve(candidates)
    assert score.call_count == 1
    rejected = next(item for item in decision["results"] if item["candidate_id"].endswith("SOL"))
    assert rejected["hard_gate"] == "fail"
    assert rejected["score_status"] == "not_evaluated"
    assert "symbol-account policy" in rejected["hard_gate_reasons"][0]
