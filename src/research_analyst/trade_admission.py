"""Deterministic execution admission and candidate clash resolution."""
from __future__ import annotations

import math
from datetime import datetime, timezone

import config


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _time(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (result if result.tzinfo else result.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def admit(candidate: dict, now: datetime | None = None) -> dict:
    """Return an auditable hard-gate result; context is intentionally ignored."""
    reasons = []
    entry = candidate.get("entry_price")
    if entry is None:
        entry = (candidate.get("entry_condition") or {}).get("price")
    targets = candidate.get("targets") or []
    target = targets[0] if targets else candidate.get("take_profit")
    stop = candidate.get("invalidation_price", candidate.get("stop_loss"))
    if not all(_number(v) and v > 0 for v in (entry, stop, target)):
        reasons.append("prices must be finite and positive")
    direction = str(candidate.get("direction", "")).lower()
    if direction not in {"long", "short"}:
        reasons.append("direction is invalid")
    if _number(entry) and _number(stop) and _number(target):
        geometry = stop < entry < target if direction == "long" else target < entry < stop
        if not geometry:
            reasons.append("directional SL/TP geometry is invalid")
        risk = abs(entry - stop)
        reward = abs(target - entry)
        rr = reward / risk if risk else 0.0
        distance = risk / entry
        if rr < float(getattr(config, "INTENT_MIN_RR", 2.0)):
            reasons.append("reward/risk below minimum")
        if distance < float(getattr(config, "INTENT_MIN_STOP_DISTANCE_PCT", .001)):
            reasons.append("stop distance below minimum")
        if distance > float(getattr(config, "INTENT_MAX_STOP_DISTANCE_PCT", .05)):
            reasons.append("stop distance above maximum")
    else:
        rr = None
        distance = None
    try:
        expiry = _time(candidate["valid_until"])
        if expiry <= (now or datetime.now(timezone.utc)).astimezone(timezone.utc):
            reasons.append("entry expiry is not in the future")
    except (KeyError, TypeError, ValueError, OverflowError):
        reasons.append("valid_until is invalid")
    freshness = candidate.get("data_freshness_seconds")
    if not _number(freshness) or freshness < 0 or freshness > float(getattr(config, "DATA_FRESHNESS_MAX_SECONDS", 600)):
        reasons.append("market data is stale or freshness is unavailable")
    identity = candidate.get("candidate_id") or candidate.get("dedupe_key")
    if not isinstance(identity, str) or not identity.strip():
        reasons.append("event identity is invalid")
    return {"hard_gate": "pass" if not reasons else "fail", "hard_gate_reasons": reasons,
            "rr": rr, "stop_distance_pct": distance, "selected_take_profit": target}


def score(candidate: dict) -> dict:
    """Bounded additive score. Missing context remains unavailable, never support."""
    context = candidate.get("context") or candidate.get("feature_snapshot") or {}
    components = {}
    for name, key in (("strategy_component", "strategy_score"), ("htf_bias_component", "htf_bias"),
                      ("swing_component", "swings"), ("fvg_component", "fvg"),
                      ("order_block_component", "order_block"), ("alignment_component", "alignment"),
                      ("freshness_component", "freshness"), ("agreement_component", "agreement")):
        value = context.get(key)
        status = "unavailable" if value is None else ("support" if float(value) > 0 else "contradict" if float(value) < 0 else "neutral")
        components[name] = {"value": max(-10.0, min(10.0, float(value))) if value is not None else 0.0, "status": status}
    total = round(sum(item["value"] for item in components.values()), 6)
    return {"score": total, "components": components, "score_policy_version": "trade-admission-v1"}


def resolve(candidates: list[dict]) -> dict:
    eligible = []
    results = []
    for candidate in candidates:
        admission = admit(candidate)
        scored = score(candidate)
        result = {"candidate_id": candidate.get("candidate_id"), **admission, **scored}
        results.append(result)
        if admission["hard_gate"] == "pass":
            eligible.append((candidate, result))
    selected = []
    for asset in sorted({c.get("asset") for c, _ in eligible}):
        groups = {d: [(c, r) for c, r in eligible if c.get("asset") == asset and str(c.get("direction")).lower() == d] for d in ("long", "short")}
        priority = getattr(config, "STRATEGY_PRIORITY", {}) or {}
        winners = {d: max(items, key=lambda x: (x[1]["score"], -int(priority.get(x[0].get("strategy_id"), x[0].get("strategy_priority", 999999))), str(x[0].get("strategy_id", "")))) for d, items in groups.items() if items}
        if len(winners) == 2:
            ordered = sorted(winners.values(), key=lambda x: x[1]["score"], reverse=True)
            if ordered[0][1]["score"] - ordered[1][1]["score"] < float(getattr(config, "CLASH_MIN_SCORE_MARGIN", 2.0)):
                continue
            selected.append(ordered[0][0].get("candidate_id"))
        elif winners:
            selected.append(next(iter(winners.values()))[0].get("candidate_id"))
    selected_set = set(selected)
    for result in results:
        cid = result["candidate_id"]
        if result["hard_gate"] != "pass":
            result["status"] = "hard_gate_failed"
        elif cid in selected_set:
            result["status"] = "selected_for_executor"
        else:
            result["status"] = "eligible_suppressed_by_same_direction_rank"
    for asset in sorted({c.get("asset") for c, r in eligible}):
        groups = {d: [(c, r) for c, r in eligible if c.get("asset") == asset and str(c.get("direction")).lower() == d] for d in ("long", "short")}
        if groups["long"] and groups["short"]:
            winners = []
            priority = getattr(config, "STRATEGY_PRIORITY", {}) or {}
            for direction in ("long", "short"):
                winners.append(max(groups[direction], key=lambda x: (x[1]["score"], -int(priority.get(x[0].get("strategy_id"), x[0].get("strategy_priority", 999999))), str(x[0].get("strategy_id", ""))))[1])
            if abs(winners[0]["score"] - winners[1]["score"]) < float(getattr(config, "CLASH_MIN_SCORE_MARGIN", 2.0)):
                for result in winners:
                    result["status"] = "advisory_only"
                for result in results:
                    if result.get("candidate_id") in {w["candidate_id"] for w in winners}:
                        result["status"] = "eligible_suppressed_by_opposite_direction_clash"
    return {"results": results, "selected_candidate_ids": selected, "conflict_group_key": "asset+cutoff"}
