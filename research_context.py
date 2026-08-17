"""Deterministic construction of canonical event-review packets."""

from research_contracts import canonical_json, reject_sensitive_keys
from research_tools import ResearchTools


def build_event_review(connection, request_id, alpha_id, as_of, max_input_chars: int = 24000) -> dict:
    tools = ResearchTools(connection, as_of)
    event = tools.get_event(alpha_id)
    payload = event["value"]
    symbol = payload["feature_snapshot"].get("source_symbol", payload["asset"])
    tier = payload["feature_snapshot"].get("liquidity_tier", "unknown")
    packet = {"schema_version": 1, "request_id": request_id, "request_kind": "event_review", "subject": {"type": "alpha_event", "id": alpha_id}, "as_of": as_of.isoformat(), "event": event, "evidence": {"data_quality": tools.get_data_quality(symbol), "completed_bars": tools.get_completed_bars(symbol), "discovery_context": tools.get_discovery_context(payload["asset"]), "regime_context": tools.get_regime_context(payload["asset"]), "descriptive_prior_outcomes": tools.get_prior_outcomes(payload["strategy_id"], tier)}, "policy": {"no_execution_advice": True, "no_probability_claims": True, "must_cite_evidence_ids": True, "external_sources_allowed": False}}
    reject_sensitive_keys(packet)
    if len(canonical_json(packet)) > max_input_chars:
        raise ValueError("canonical research input exceeds configured bound")
    return packet
