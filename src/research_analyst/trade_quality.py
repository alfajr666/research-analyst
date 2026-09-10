"""Unified, modular trade-quality scoring and candidate resolution."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

import config
from entry_policy import evaluate_entry_policy
from trade_quality_components import COMPONENTS, ScoreContext
from trade_quality_profiles import (
    QUALITY_SCORE_POLICY_VERSION,
    QUALITY_SCORE_PROFILE_VERSION,
    effective_profile_weights,
    profile_weights,
)


def _asset(value: Any) -> str:
    from trade_admission import canonical_asset

    return canonical_asset(value)


def _family_weight(decision: Mapping[str, Any] | None, family: str, mode: str) -> float:
    if mode == "off" or not isinstance(decision, Mapping):
        return 0.0
    if family == "reversal":
        return 1.0 if (decision.get("family_activation") or {}).get("families", {}).get("reversal", {}).get("active") else 0.0
    return float((decision.get("family_weights") or {}).get(family, 0.0) or 0.0)


def _regime_readiness(context: ScoreContext) -> dict[str, Any] | None:
    if context.regime_mode != "enforce":
        return None
    decision = context.regime_decision
    if not isinstance(decision, Mapping) or decision.get("decision") != "allow":
        return {"reason": "exact-cutoff regime scope is unavailable or blocked"}
    return None


def _aggregate(observations: list[dict[str, Any]], weights: Mapping[str, float]) -> float:
    total = 0.0
    weight_total = 0.0
    for observation in observations:
        if observation.get("role") != "quality":
            continue
        component_id = observation.get("component_id")
        weight = float(weights.get(component_id, 0.0))
        if weight <= 0:
            continue
        value = float(observation.get("value", 0.5))
        if observation.get("status") == "unavailable":
            value = 0.5 if observation.get("missing_policy", "neutral") == "neutral" else value
        total += weight * max(0.0, min(value, 1.0))
        weight_total += weight
    return round(max(0.0, min(total / weight_total if weight_total else 0.0, 1.0)), 6)


def score_candidate(
    candidate: dict,
    *,
    structural_context: Mapping[str, Any] | None = None,
    market_bars: Any = None,
    regime_decision: Mapping[str, Any] | None = None,
    regime_mode: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return one immutable, auditable score result for a candidate."""
    mode = str(regime_mode or getattr(config, "REGIME_SESSION_MODE", "off")).lower()
    family = str(candidate.get("market_family") or "unknown").lower()
    context = ScoreContext(candidate, structural_context, market_bars, regime_decision, mode, now)
    observations = [component(context).as_dict() for component in COMPONENTS]
    readiness = _regime_readiness(context)
    if readiness is not None:
        observations.append({
            "component_id": "regime_readiness",
            "component_version": "regime_readiness-v1",
            "role": "floor",
            "value": 0.0,
            "status": "invalid",
            "raw_inputs": {"mode": mode},
            "evidence": {},
            "reason": readiness["reason"],
            "missing_policy": "invalid",
        })
    floor_failures = [
        item for item in observations
        if item.get("role") == "floor" and item.get("status") != "support"
    ]
    family_weight = _family_weight(regime_decision, family, mode)
    profile_name, regime_weights = effective_profile_weights(family, family_weight)
    baseline_weights = profile_weights("neutral")
    baseline_score = 0.0 if floor_failures else _aggregate(observations, baseline_weights)
    regime_score = 0.0 if floor_failures else _aggregate(observations, regime_weights)
    operational_score = baseline_score if mode in {"off", "shadow"} else regime_score
    threshold = float(getattr(config, "TRADE_QUALITY_MIN_SCORE", 0.30))
    selected = not floor_failures and operational_score >= threshold
    status = "selected_for_scoring" if selected else "score_floor_failed" if floor_failures else "score_below_threshold"
    reasons = [str(item.get("reason")) for item in floor_failures if item.get("reason")]
    result = {
        "quality_score": operational_score,
        "score": operational_score,
        "baseline_score": baseline_score,
        "regime_weighted_score": regime_score,
        "score_threshold": threshold,
        "score_status": "scored" if not floor_failures else "floor_failed",
        "score_decision": "eligible" if selected else "rejected",
        "status": status,
        "hard_gate": "pass" if not floor_failures else "fail",
        "hard_gate_reasons": reasons,
        "score_reasons": reasons,
        "components": {item["component_id"]: item for item in observations},
        "score_components": {item["component_id"]: item for item in observations},
        "score_policy_version": QUALITY_SCORE_POLICY_VERSION,
        "score_profile": profile_name,
        "score_profile_version": QUALITY_SCORE_PROFILE_VERSION,
        "regime_mode": mode,
        "regime_family": family,
        "regime_family_weight": family_weight,
        "regime_observation_id": (regime_decision or {}).get("score_observation_id"),
        "family_activation_version": ((regime_decision or {}).get("family_activation") or {}).get("version"),
        "candidate_id": candidate.get("candidate_id") or candidate.get("dedupe_key"),
        "data_freshness_seconds": candidate.get("data_freshness_seconds"),
    }
    from trade_admission import candidate_admission_fingerprint
    result["candidate_fingerprint"] = candidate_admission_fingerprint(candidate)
    # Keep the immutable structural proof at the result top level for the
    # existing intent and audit contracts. The scorer still owns the gate.
    for item in observations:
        if item.get("component_id") == "structural_stop":
            result.update(item.get("evidence") or {})
        elif item.get("component_id") == "symbol_account_policy":
            result.update({
                key: value for key, value in (item.get("raw_inputs") or {}).items()
                if key in {"symbol_account_gate", "canonical_asset", "resolved_account", "effective_universe_assets", "effective_universe_version", "policy_version"}
            })
    result["atr14_4h"] = (
        ((structural_context or {}).get("atr_by_timeframe") or {}).get("4h")
        if isinstance(structural_context, Mapping) else None
    )
    return result


def _rank_key(candidate: dict, result: dict) -> tuple:
    priority = getattr(config, "STRATEGY_PRIORITY", {}) or {}
    return (
        -float(result.get("quality_score", 0.0)),
        int(priority.get(candidate.get("strategy_id"), candidate.get("strategy_priority", 999999))),
        str(candidate.get("strategy_id", "")),
    )


def resolve(
    candidates: list[dict],
    *,
    structural_contexts: dict[str, dict] | None = None,
    market_bars_by_asset: dict[str, Any] | None = None,
    regime_scope: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    effective_universe: Iterable[object] | None = None,
    effective_universe_version: object | None = None,
) -> dict[str, Any]:
    """Score all candidates, then resolve deterministic same/opposite clashes."""
    mode = str((regime_scope or {}).get("mode") or getattr(config, "REGIME_SESSION_MODE", "off"))
    results: list[dict] = []
    eligible: list[tuple[dict, dict]] = []
    peer_counts: dict[tuple[str, str], int] = {}
    for candidate in candidates:
        key = (_asset(candidate.get("asset")), str(candidate.get("direction") or "").lower())
        peer_counts[key] = peer_counts.get(key, 0) + 1
    for candidate in candidates:
        candidate = dict(candidate)
        candidate["effective_universe_assets"] = list(effective_universe or candidate.get("effective_universe_assets") or [])
        candidate["effective_universe_version"] = str(effective_universe_version or candidate.get("effective_universe_version") or "")
        candidate["_score_agreement"] = max(0.0, min(10.0, (peer_counts.get((_asset(candidate.get("asset")), str(candidate.get("direction") or "").lower()), 1) - 1) * 2.0))
        policy = evaluate_entry_policy(candidate)
        asset = _asset(candidate.get("asset"))
        decision = ((regime_scope or {}).get("decisions") or {}).get(asset)
        result = score_candidate(
            candidate,
            structural_context=(structural_contexts or {}).get(asset),
            market_bars=(market_bars_by_asset or {}).get(asset),
            regime_decision=decision,
            regime_mode=mode,
            now=now,
        )
        result["entry_policy_status"] = "shadow_would_block" if policy.get("decision") == "would_block" else policy.get("decision")
        result["entry_policy_reasons"] = policy.get("reasons", [])
        if policy.get("enforced_block"):
            result.update({
                "quality_score": 0.0,
                "score": 0.0,
                "score_decision": "rejected",
                "status": "entry_policy_blocked",
                "hard_gate": "fail",
                "hard_gate_reasons": policy.get("reasons", []),
            })
        account = result.get("resolved_account") or candidate.get("_execution_account")
        if account is None:
            from trade_admission import admit_symbol_account
            policy_result = admit_symbol_account(
                candidate,
                effective_universe=effective_universe,
                effective_universe_version=effective_universe_version,
                account_id=candidate.get("_execution_account"),
            )
            result.update(policy_result)
            account = policy_result.get("resolved_account")
        result["conflict_group_key"] = f"{asset}+{account}+{candidate.get('cutoff_at') or (candidate.get('feature_snapshot') or {}).get('cutoff') or candidate.get('observed_at') or ''}"
        results.append(result)
        if result.get("score_decision") == "eligible":
            eligible.append((candidate, result))

    selected: list[str] = []
    groups = sorted({(_asset(candidate.get("asset")), result.get("resolved_account")) for candidate, result in eligible})
    clash_margin = float(getattr(config, "TRADE_QUALITY_CLASH_MIN_MARGIN", 0.10))
    selected_by_group: dict[tuple[str, str], set[str]] = {}
    for asset, account in groups:
        directional = {
            direction: [
                (candidate, result) for candidate, result in eligible
                if _asset(candidate.get("asset")) == asset
                and result.get("resolved_account") == account
                and str(candidate.get("direction") or "").lower() == direction
            ]
            for direction in ("long", "short")
        }
        winners = {direction: min(items, key=lambda item: _rank_key(*item)) for direction, items in directional.items() if items}
        if len(winners) == 2:
            ordered = sorted(winners.values(), key=lambda item: _rank_key(*item))
            margin = round(ordered[0][1]["quality_score"] - ordered[1][1]["quality_score"], 6)
            for _, result in ordered:
                result["score_margin"] = margin
            if margin < clash_margin:
                for _, result in ordered:
                    result["status"] = "eligible_suppressed_by_opposite_direction_clash"
                continue
            winner = ordered[0]
        elif winners:
            winner = next(iter(winners.values()))
        else:
            continue
        selected_id = winner[0].get("candidate_id")
        selected.append(selected_id)
        selected_by_group[(asset, account)] = {selected_id}

    selected_set = set(selected)
    for candidate, result in eligible:
        candidate_id = candidate.get("candidate_id")
        if candidate_id in selected_set:
            result["status"] = "selected_for_executor"
        elif result.get("status") == "selected_for_scoring":
            result["status"] = "eligible_suppressed_by_same_direction_rank"
    for result in results:
        if result.get("status") == "selected_for_scoring":
            result["status"] = "score_below_threshold"
    return {"results": results, "selected_candidate_ids": selected, "conflict_group_key": "asset+account+cutoff"}
