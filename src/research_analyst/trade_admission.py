"""Deterministic execution admission and candidate clash resolution."""
from __future__ import annotations

import math
from datetime import datetime, timezone

import config


POLICY_VERSION = "symbol-account-policy-v1"
COMPACT_ASSETS = frozenset(("BTC", "ETH", "PAXG", "QQQ"))
FUNDAMO_STRATEGIES = frozenset((
    "dual-zone-follower-v2", "dual-zone-short-follower-v2",
    "ema20-pullback-h4-trend-v1", "ema-stack-15m-adx-stochrsi-5m-v1",
))
COMPACT_STRATEGIES = frozenset(getattr(config, "COMPACT_STRATEGY_IDS", ()))


def canonical_asset(value: object) -> str:
    """Normalize exchange symbols before applying the account policy."""
    text = str(value or "").upper().strip()
    if "/" in text:
        text = text.split("/", 1)[0]
    for suffix in ("_PERP.A", "_PERP", "-USDT-PERP", "USDT", "USD"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text


def resolved_account(strategy_id: object) -> str:
    """Resolve routing from strategy configuration, never candidate metadata."""
    strategy_id = str(strategy_id or "")
    if strategy_id in COMPACT_STRATEGIES:
        return "hyro"
    if strategy_id in FUNDAMO_STRATEGIES:
        return "fundamo"
    route = (getattr(config, "INTENT_ROUTING", {}) or {}).get(strategy_id, {}) or {}
    return str(route.get("account_id") or getattr(config, "INTENT_ACCOUNT_ID", "hyro"))


def admit_symbol_account(candidate: dict) -> dict:
    """Apply the deterministic symbol/account safety boundary before scoring."""
    strategy_id = str(candidate.get("strategy_id") or "")
    asset = canonical_asset(candidate.get("asset"))
    account = resolved_account(strategy_id)
    rejection_reason = None
    if strategy_id in COMPACT_STRATEGIES and (account != "hyro" or asset not in COMPACT_ASSETS):
        rejection_reason = f"compact Hyro policy permits only {', '.join(sorted(COMPACT_ASSETS))}"
    elif strategy_id in FUNDAMO_STRATEGIES:
        approved = {canonical_asset(asset) for asset in config.load_static_symbols()}
        if account != "fundamo":
            rejection_reason = "Fundamo strategy resolved to a non-Fundamo account"
        elif asset not in approved:
            rejection_reason = "asset is not in the approved universe"
    return {
        "symbol_account_gate": "pass" if rejection_reason is None else "fail",
        "strategy_id": strategy_id,
        "canonical_asset": asset,
        "resolved_account": account,
        "policy_version": POLICY_VERSION,
        "rejection_reason": rejection_reason,
    }


def format_symbol_account_rejection(result: dict) -> str:
    """Serialize policy identity into the durable status-history reason."""
    return (
        f"{result.get('rejection_reason')}; canonical_asset={result.get('canonical_asset')}; "
        f"resolved_account={result.get('resolved_account')}; policy_version={result.get('policy_version')}"
    )


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def derive_2r_target(direction, entry, stop):
    """Return the producer fallback target when all price inputs are valid."""
    if not (_number(entry) and entry > 0 and _number(stop) and stop > 0):
        return None
    direction = str(direction or "").lower()
    if direction == "long" and stop < entry:
        return entry + 2 * abs(entry - stop)
    if direction == "short" and stop > entry:
        return entry - 2 * abs(entry - stop)
    return None


def _time(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (result if result.tzinfo else result.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def admit(candidate: dict, now: datetime | None = None) -> dict:
    """Return an auditable hard-gate result; context is intentionally ignored."""
    symbol_policy = admit_symbol_account(candidate)
    reasons = []
    if symbol_policy["symbol_account_gate"] != "pass":
        reasons.append(f"symbol-account policy: {format_symbol_account_rejection(symbol_policy)}")
    entry = candidate.get("entry_price")
    if entry is None:
        entry = (candidate.get("entry_condition") or {}).get("price")
    targets = candidate.get("targets") or []
    target = targets[0] if targets else candidate.get("take_profit")
    stop = candidate.get("invalidation_price", candidate.get("stop_loss"))
    if target is None:
        target = derive_2r_target(candidate.get("direction"), entry, stop)
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
        atr14_4h = candidate.get("atr14_4h")
        multiplier = float(getattr(config, "INTENT_MIN_STOP_ATR_MULTIPLIER", 0.25))
        absolute_floor = float(getattr(config, "INTENT_MIN_STOP_DISTANCE_PCT", .001))
        if not _number(atr14_4h) or atr14_4h <= 0:
            reasons.append("4h ATR14 is unavailable or invalid")
            atr_floor = 0.0
        else:
            atr_floor = float(atr14_4h) / entry * multiplier
        if distance < max(absolute_floor, atr_floor):
            reasons.append("stop distance below ATR-based minimum")
        if distance > float(getattr(config, "INTENT_MAX_STOP_DISTANCE_PCT", .05)):
            reasons.append("stop distance above maximum")
    else:
        rr = None
        distance = None
        atr14_4h = None
        multiplier = float(getattr(config, "INTENT_MIN_STOP_ATR_MULTIPLIER", 0.25))
        absolute_floor = float(getattr(config, "INTENT_MIN_STOP_DISTANCE_PCT", .001))
        atr_floor = 0.0
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
    atr_pct = float(atr14_4h) / entry if _number(atr14_4h) and _number(entry) and entry > 0 else None
    return {"hard_gate": "pass" if not reasons else "fail", "hard_gate_reasons": reasons,
            **symbol_policy,
            "rr": rr, "stop_distance_pct": distance, "selected_take_profit": target,
            "atr14_4h": atr14_4h, "stop_atr_multiple": distance / atr_pct if distance is not None and atr_pct else None,
            "effective_min_stop_distance_pct": max(absolute_floor, atr_floor)}


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
        symbol_policy = admit_symbol_account(candidate)
        if symbol_policy["symbol_account_gate"] != "pass":
            results.append({
                "candidate_id": candidate.get("candidate_id"),
                **symbol_policy,
                "hard_gate": "fail",
                "hard_gate_reasons": [f"symbol-account policy: {format_symbol_account_rejection(symbol_policy)}"],
                "score_status": "not_evaluated",
            })
            continue
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
