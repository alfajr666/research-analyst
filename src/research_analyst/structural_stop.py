"""Validation for strategy-declared structural stop references."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any


ALLOWED_KINDS = {"swing", "swing_low", "swing_high", "fvg", "order_block", "strategy_boundary"}
ALLOWED_TIMEFRAMES = {"1h", "4h"}


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _finite_positive(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def validate_structural_stop(
    candidate: dict[str, Any],
    *,
    now: datetime | None = None,
    max_reference_gap_pct: float | None = None,
    required: bool = True,
) -> dict[str, Any]:
    """Validate a proposed stop without mutating the candidate.

    The optional gap policy is deliberately explicit because its numeric value
    is a deployment decision, not part of the structural reference contract.
    """
    result: dict[str, Any] = {
        "structural_stop_gate": "pass",
        "structural_stop_reasons": [],
        "reference_id": None,
        "reference_kind": None,
        "reference_boundary": None,
        "stop_buffer": None,
    }
    reference = candidate.get("structural_reference")
    if not reference:
        result["structural_stop_gate"] = "fail" if required else "unavailable"
        result["structural_stop_reasons"].append("structural reference is missing")
        return result
    if not isinstance(reference, dict):
        result["structural_stop_gate"] = "fail"
        result["structural_stop_reasons"].append("structural reference is malformed")
        return result

    result["reference_id"] = reference.get("reference_id")
    result["reference_kind"] = reference.get("kind")
    result["reference_boundary"] = reference.get("boundary_price")
    reasons = result["structural_stop_reasons"]
    if reference.get("kind") not in ALLOWED_KINDS:
        reasons.append("structural reference kind is invalid")
    if reference.get("timeframe") not in ALLOWED_TIMEFRAMES:
        reasons.append("structural reference timeframe is invalid")
    if str(reference.get("asset", "")).upper() != str(candidate.get("asset", "")).upper():
        reasons.append("structural reference asset does not match candidate")
    if not isinstance(reference.get("reference_id"), str) or not reference["reference_id"].strip():
        reasons.append("structural reference identity is invalid")
    if not _finite_positive(reference.get("boundary_price")):
        reasons.append("structural reference boundary is invalid")
    if not _finite_positive(candidate.get("invalidation_price", candidate.get("stop_loss"))):
        reasons.append("candidate stop is invalid")
    if reference.get("coverage_status") != "covered":
        reasons.append("structural reference coverage is not complete")
    evidence = reference.get("source_evidence_ids")
    if not isinstance(evidence, list) or not evidence or not all(isinstance(item, str) and item for item in evidence):
        reasons.append("structural reference evidence is missing")

    current = _utc(now or datetime.now(timezone.utc))
    try:
        observed_at = _utc(candidate["observed_at"])
        formed_at = _utc(reference["formed_at"])
        confirmed_at = _utc(reference["confirmed_at"])
        cutoff_at = _utc(reference["cutoff_at"])
        if formed_at > confirmed_at or confirmed_at > observed_at or observed_at > cutoff_at:
            reasons.append("structural reference timestamps are not point-in-time valid")
        if cutoff_at > current and now is not None:
            reasons.append("structural reference cutoff is in the future")
    except (KeyError, TypeError, ValueError, OverflowError):
        reasons.append("structural reference timestamps are invalid")

    direction = str(candidate.get("direction", "")).lower()
    stop = float(candidate.get("invalidation_price", candidate.get("stop_loss"))) if _finite_positive(candidate.get("invalidation_price", candidate.get("stop_loss"))) else None
    boundary = float(reference["boundary_price"]) if _finite_positive(reference.get("boundary_price")) else None
    entry = float(candidate.get("entry_price")) if _finite_positive(candidate.get("entry_price")) else None
    if stop is not None and boundary is not None:
        if direction == "long":
            buffer = boundary - stop
            if stop >= boundary:
                reasons.append("stop does not clear lower structural boundary")
        elif direction == "short":
            buffer = stop - boundary
            if stop <= boundary:
                reasons.append("stop does not clear upper structural boundary")
        else:
            buffer = None
            reasons.append("candidate direction is invalid")
        if buffer is not None:
            result["stop_buffer"] = buffer
            if buffer <= 0:
                reasons.append("structural stop buffer is not positive")
            if max_reference_gap_pct is None:
                reasons.append("structural stop distance policy is unavailable")
            elif entry is None or buffer / entry > float(max_reference_gap_pct):
                reasons.append("stop is too far from structural boundary")

    if reasons:
        result["structural_stop_gate"] = "unavailable" if "structural stop distance policy is unavailable" in reasons and len(reasons) == 1 else "fail"
    return result
