from datetime import datetime, timezone
from pathlib import Path
import tempfile

import config
from entry_policy import annotate_candidate, evaluate_entry_policy, session_context


def test_session_context_marks_the_first_thirty_minutes_as_cooldown():
    context = session_context(datetime(2026, 9, 4, 8, 20, tzinfo=timezone.utc))

    assert context["session_name"] == "europe"
    assert context["session_phase"] == "cooldown"
    assert context["session_elapsed_minutes"] == 20


def test_session_context_marks_rollover_as_off_hours():
    context = session_context(datetime(2026, 9, 4, 22, 0, tzinfo=timezone.utc))

    assert context["session_name"] == "rollover"
    assert context["session_phase"] == "off_hours"


def test_shadow_policy_records_a_would_block_without_enforcing_it():
    result = evaluate_entry_policy({
        "strategy_id": "bb-rsi-meanrev-v1",
        "observed_at": "2026-09-04T00:20:00Z",
    }, mode="shadow")

    assert result["market_family"] == "mean_reversion"
    assert result["environment_state"] == "unknown"
    assert result["decision"] == "would_block"
    assert result["enforced_block"] is False
    assert result["reasons"] == ["session_open_cooldown"]


def test_annotation_preserves_candidate_and_adds_policy_metadata():
    candidate = {
        "strategy_id": "mtf-exhaustion-reversal-v1",
        "observed_at": "2026-09-04T12:00:00Z",
        "feature_snapshot": {"market_regime": "high_vol"},
    }

    annotated = annotate_candidate(candidate, mode="shadow")

    assert "entry_policy" not in candidate
    assert annotated["entry_policy"]["market_family"] == "reversal"
    assert annotated["entry_policy"]["environment_state"] == "shock"
    assert annotated["entry_policy"]["decision"] == "allow"


def test_capture_persists_policy_observation():
    from raw_signal_batch import capture

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "analyst.db"
        config.init_analyst_db(db_path)
        capture({
            "candidate_id": "candidate-1",
            "strategy_id": "bb-rsi-meanrev-v1",
            "asset": "BTC",
            "direction": "long",
            "observed_at": "2026-09-04T00:20:00Z",
            "valid_until": "2026-09-04T00:25:00Z",
        }, db_path=db_path)
        conn = config.get_db_connection(read_only=True, db_path=db_path)
        row = conn.execute(
            "SELECT decision, session_name, market_family FROM entry_policy_observations"
        ).fetchone()
        conn.close()

    assert row == ("would_block", "asia", "mean_reversion")


def test_shadow_mode_does_not_suppress_an_admissible_candidate(monkeypatch):
    from trade_admission import resolve

    monkeypatch.setattr(config, "ENTRY_POLICY_MODE", "shadow")
    result = resolve([{
        "candidate_id": "candidate-1",
        "strategy_id": "bb-rsi-meanrev-v1",
        "asset": "BTC",
        "direction": "long",
        "observed_at": "2026-09-04T00:20:00Z",
        "valid_until": "2099-09-04T00:25:00Z",
        "entry_price": 100.0,
        "invalidation_price": 95.0,
        "targets": [110.0],
        "atr14_4h": 10.0,
        "data_freshness_seconds": 1.0,
    }])

    assert result["selected_candidate_ids"] == ["candidate-1"]
    assert result["results"][0]["entry_policy_status"] == "shadow_would_block"


def test_entry_policy_enforce_mode_does_not_create_a_second_session_gate(monkeypatch):
    from trade_admission import resolve

    monkeypatch.setattr(config, "ENTRY_POLICY_MODE", "enforce")
    result = resolve([{
        "candidate_id": "candidate-1",
        "strategy_id": "bb-rsi-meanrev-v1",
        "asset": "BTC",
        "direction": "long",
        "observed_at": "2026-09-04T00:20:00Z",
        "valid_until": "2099-09-04T00:25:00Z",
        "entry_price": 100.0,
        "invalidation_price": 95.0,
        "targets": [110.0],
        "atr14_4h": 10.0,
        "data_freshness_seconds": 1.0,
    }])

    assert result["selected_candidate_ids"] == ["candidate-1"]
    assert result["results"][0]["entry_policy_status"] == "shadow_would_block"
    assert result["results"][0]["hard_gate"] == "pass"
