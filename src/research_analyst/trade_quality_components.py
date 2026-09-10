"""Independent trade-quality score components.

Each function is deliberately side-effect free. The aggregator owns weighting,
thresholding, and persistence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Any, Callable, Mapping, Sequence

import config
from structural_stop import _normalise_closed_bar_timestamp, admit_selected_structural_stop


@dataclass(frozen=True)
class ComponentObservation:
    component_id: str
    component_version: str
    role: str
    value: float
    status: str
    raw_inputs: Mapping[str, Any]
    evidence: Mapping[str, Any]
    reason: str
    missing_policy: str = "neutral"

    def as_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "component_version": self.component_version,
            "role": self.role,
            "value": round(max(0.0, min(float(self.value), 1.0)), 6),
            "status": self.status,
            "raw_inputs": dict(self.raw_inputs),
            "evidence": dict(self.evidence),
            "reason": self.reason,
            "missing_policy": self.missing_policy,
        }


@dataclass(frozen=True)
class ScoreContext:
    candidate: Mapping[str, Any]
    structural_context: Mapping[str, Any] | None = None
    market_bars: Any = None
    regime_decision: Mapping[str, Any] | None = None
    regime_mode: str = "off"
    now: datetime | None = None


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _positive(value: Any) -> bool:
    return _finite(value) and float(value) > 0


def _utc(value: Any) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _entry_stop_target(candidate: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    entry = candidate.get("entry_price")
    if entry is None:
        entry = (candidate.get("entry_condition") or {}).get("price")
    stop = candidate.get("invalidation_price", candidate.get("stop_loss"))
    targets = candidate.get("targets") or []
    target = targets[0] if targets else candidate.get("take_profit")
    return entry, stop, target


def _observation(
    component_id: str,
    role: str,
    value: float,
    status: str,
    reason: str,
    *,
    raw_inputs: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    version: str = "v1",
    missing_policy: str = "neutral",
) -> ComponentObservation:
    return ComponentObservation(
        component_id=component_id,
        component_version=f"{component_id}-{version}",
        role=role,
        value=max(0.0, min(float(value), 1.0)),
        status=status,
        raw_inputs=raw_inputs or {},
        evidence=evidence or {},
        reason=reason,
        missing_policy=missing_policy,
    )


def identity_validity(context: ScoreContext) -> ComponentObservation:
    candidate = context.candidate
    identity = candidate.get("candidate_id") or candidate.get("dedupe_key")
    direction = str(candidate.get("direction") or "").lower()
    observed = _utc(candidate.get("observed_at"))
    valid_until = _utc(candidate.get("valid_until"))
    valid = bool(identity and direction in {"long", "short"} and observed and valid_until and valid_until > observed)
    return _observation(
        "identity_validity", "quality", 1.0 if valid else 0.0,
        "support" if valid else "invalid",
        "candidate identity and temporal fields are valid" if valid else "candidate identity or temporal fields are invalid",
        raw_inputs={"candidate_id": identity, "direction": direction},
    )


def price_geometry(context: ScoreContext) -> ComponentObservation:
    entry, stop, target = _entry_stop_target(context.candidate)
    direction = str(context.candidate.get("direction") or "").lower()
    valid = all(_positive(value) for value in (entry, stop, target)) and (
        (direction == "long" and stop < entry < target)
        or (direction == "short" and target < entry < stop)
    )
    return _observation(
        "price_geometry", "quality", 1.0 if valid else 0.0,
        "support" if valid else "invalid",
        "directional price geometry is valid" if valid else "directional price geometry is invalid",
        raw_inputs={"entry": entry, "stop": stop, "target": target, "direction": direction},
    )


def reward_risk(context: ScoreContext) -> ComponentObservation:
    entry, stop, target = _entry_stop_target(context.candidate)
    if not all(_positive(value) for value in (entry, stop, target)):
        return _observation("reward_risk", "quality", 0.0, "invalid", "RR inputs are invalid")
    risk = abs(float(entry) - float(stop))
    reward = abs(float(target) - float(entry))
    rr = reward / risk if risk else 0.0
    minimum = float(getattr(config, "INTENT_MIN_RR", 2.0))
    value = max(0.0, min(1.0, rr / minimum)) if minimum > 0 else 0.0
    valid = rr >= minimum
    return _observation(
        "reward_risk", "quality", value,
        "support" if valid else "invalid",
        f"reward/risk {rr:.4f} meets minimum {minimum:.4f}" if valid else f"reward/risk {rr:.4f} is below minimum {minimum:.4f}",
        raw_inputs={"rr": rr, "minimum_rr": minimum},
    )


def stop_distance(context: ScoreContext) -> ComponentObservation:
    entry, stop, _ = _entry_stop_target(context.candidate)
    if not (_positive(entry) and _positive(stop)):
        return _observation("stop_distance", "quality", 0.0, "invalid", "stop distance inputs are invalid")
    structural = context.structural_context or {}
    atr = ((structural.get("atr_by_timeframe") or {}).get("4h") if isinstance(structural, Mapping) else None)
    atr = atr if _positive(atr) else context.candidate.get("atr14_4h")
    distance = abs(float(entry) - float(stop)) / float(entry)
    floor_pct = float(getattr(config, "INTENT_MIN_STOP_DISTANCE_PCT", 0.001))
    atr_floor = float(atr) / float(entry) * float(getattr(config, "INTENT_MIN_STOP_ATR_MULTIPLIER", 0.25)) if _positive(atr) else None
    minimum_distance = max(floor_pct, atr_floor or 0.0)
    value = max(0.0, min(1.0, distance / minimum_distance)) if minimum_distance > 0 else 0.0
    valid = atr_floor is not None and distance >= minimum_distance
    return _observation(
        "stop_distance", "quality", value,
        "support" if valid else "invalid",
        "stop distance meets configured floors" if valid else "stop distance or 4h ATR is invalid",
        raw_inputs={"distance_pct": distance, "atr14_4h": atr, "minimum_pct": floor_pct, "atr_floor_pct": atr_floor},
    )


def freshness_readiness(context: ScoreContext) -> ComponentObservation:
    freshness = context.candidate.get("data_freshness_seconds")
    maximum = float(getattr(config, "DATA_FRESHNESS_MAX_SECONDS", 600))
    valid = _finite(freshness) and 0 <= float(freshness) <= maximum
    return _observation(
        "freshness_readiness", "diagnostic", 1.0 if valid else 0.0,
        "support" if valid else "invalid",
        "market data freshness is within the execution limit" if valid else "market data is stale or unavailable",
        raw_inputs={"freshness_seconds": freshness, "maximum_seconds": maximum},
    )


def symbol_account_policy(context: ScoreContext) -> ComponentObservation:
    from trade_admission import admit_symbol_account

    candidate = context.candidate
    policy = admit_symbol_account(
        candidate,
        effective_universe=candidate.get("effective_universe_assets"),
        effective_universe_version=candidate.get("effective_universe_version"),
        account_id=candidate.get("_execution_account"),
    )
    valid = policy.get("symbol_account_gate") == "pass"
    return _observation(
        "symbol_account_policy", "quality", 1.0 if valid else 0.0,
        "support" if valid else "invalid",
        "symbol-account policy passes" if valid else str(policy.get("rejection_reason") or "symbol-account policy failed"),
        raw_inputs=policy,
    )


def structural_stop(context: ScoreContext) -> ComponentObservation:
    result = admit_selected_structural_stop(
        context.candidate,
        context.structural_context,
        cutoff=_utc(context.candidate.get("cutoff_at") or context.candidate.get("observed_at")),
        now=context.now,
    )
    valid = result.get("structural_stop_gate") == "pass"
    return _observation(
        "structural_stop", "quality", 1.0 if valid else 0.0,
        "support" if valid else "invalid",
        "structural stop context passes" if valid else "; ".join(result.get("structural_stop_reasons", [])),
        raw_inputs={"structural_stop_gate": result.get("structural_stop_gate")},
        evidence=result,
    )


def _zone_distances(context: ScoreContext) -> tuple[float | None, float | None]:
    structural = context.structural_context or {}
    entry, _, _ = _entry_stop_target(context.candidate)
    if not _positive(entry) or not isinstance(structural, Mapping):
        return None, None
    atrs = structural.get("atr_by_timeframe") or {}
    distances = {"long": [], "short": []}
    for zone in structural.get("zones") or []:
        if not isinstance(zone, Mapping) or zone.get("state", "active") not in {"active", "partial"}:
            continue
        low, high = zone.get("low"), zone.get("high")
        atr = atrs.get(str(zone.get("timeframe")))
        if not (_positive(low) and _positive(high) and _positive(atr) and low <= high):
            continue
        distance = max(0.0, float(low) - float(entry)) if entry < low else max(0.0, float(entry) - float(high))
        direction = str(zone.get("direction") or "").lower()
        if direction in {"long", "bullish"}:
            distances["long"].append(distance / float(atr))
        elif direction in {"short", "bearish"}:
            distances["short"].append(distance / float(atr))
    return (
        min(distances["long"]) if distances["long"] else None,
        min(distances["short"]) if distances["short"] else None,
    )


def _directional_zone_value(context: ScoreContext, zone_kind: str | None = None) -> float | None:
    structural = context.structural_context or {}
    if zone_kind and isinstance(structural, Mapping):
        filtered = [z for z in structural.get("zones") or [] if str(z.get("type") or z.get("kind")) == zone_kind]
        structural = dict(structural)
        structural["zones"] = filtered
    scoped = ScoreContext(context.candidate, structural, context.market_bars, context.regime_decision, context.regime_mode, context.now)
    long_distance, short_distance = _zone_distances(scoped)
    direction = str(context.candidate.get("direction") or "").lower()
    same = long_distance if direction == "long" else short_distance if direction == "short" else None
    opposite = short_distance if direction == "long" else long_distance if direction == "short" else None
    if same is None and opposite is None:
        return None
    if same is None:
        return 0.0
    support = max(0.0, 1.0 - min(float(same), 3.0) / 3.0)
    if opposite is not None and opposite < same:
        return support * 0.25
    return support


def htf_bias(context: ScoreContext) -> ComponentObservation:
    value = _directional_zone_value(context)
    if value is None:
        return _observation("htf_bias", "quality", 0.5, "unavailable", "HTF directional context is unavailable")
    return _observation("htf_bias", "quality", value, "support" if value > 0.5 else "contradict" if value < 0.5 else "neutral", "HTF directional context evaluated")


def zone_context(context: ScoreContext) -> ComponentObservation:
    values = [_directional_zone_value(context, kind) for kind in ("fvg", "order_block")]
    available = [value for value in values if value is not None]
    if not available:
        return _observation("zone_context", "quality", 0.5, "unavailable", "FVG and order-block context is unavailable")
    value = sum(available) / len(available)
    return _observation("zone_context", "quality", value, "support" if value > 0.5 else "contradict" if value < 0.5 else "neutral", "zone context evaluated")


def alignment(context: ScoreContext) -> ComponentObservation:
    value = _directional_zone_value(context)
    if value is None:
        return _observation("alignment", "quality", 0.5, "unavailable", "multi-timeframe alignment is unavailable")
    return _observation("alignment", "quality", value, "support" if value > 0.5 else "contradict" if value < 0.5 else "neutral", "multi-timeframe alignment evaluated")


def freshness_quality(context: ScoreContext) -> ComponentObservation:
    freshness = context.candidate.get("data_freshness_seconds")
    maximum = float(getattr(config, "DATA_FRESHNESS_MAX_SECONDS", 600))
    if not _finite(freshness) or maximum <= 0 or freshness < 0:
        return _observation("freshness_quality", "quality", 0.5, "unavailable", "freshness quality is unavailable")
    value = max(0.0, min(1.0, 1.0 - float(freshness) / maximum))
    return _observation("freshness_quality", "quality", value, "support" if value > 0.5 else "contradict" if value < 0.5 else "neutral", "freshness quality evaluated")


def same_direction_agreement(context: ScoreContext) -> ComponentObservation:
    raw = context.candidate.get("_score_agreement")
    if not _finite(raw):
        return _observation("same_direction_agreement", "quality", 0.5, "unavailable", "same-direction agreement is unavailable")
    value = max(0.0, min(1.0, float(raw) / 10.0))
    return _observation("same_direction_agreement", "quality", value, "support" if value > 0.5 else "neutral", "same-direction agreement evaluated", raw_inputs={"agreement": raw})


def _frame_column(frame: Any, name: str) -> list[Any]:
    if frame is None:
        return []
    if hasattr(frame, "columns") and name in frame.columns:
        return frame.get_column(name).to_list()
    if isinstance(frame, Mapping):
        return list(frame.get(name) or [])
    return [row.get(name) for row in frame if isinstance(row, Mapping)]


def rvol(context: ScoreContext) -> ComponentObservation:
    volumes = [value for value in _frame_column(context.market_bars, "volume") if _finite(value) and float(value) >= 0]
    lookback = int(getattr(config, "TRADE_QUALITY_RVOL_LOOKBACK_BARS", 96))
    minimum = int(getattr(config, "TRADE_QUALITY_RVOL_MIN_BARS", 32))
    if len(volumes) < minimum + 1:
        return _observation("rvol", "quality", 0.5, "unavailable", "RVOL history is unavailable", raw_inputs={"bars": len(volumes), "minimum": minimum})
    reference = median(volumes[-(lookback + 1):-1])
    current = float(volumes[-1])
    if not _finite(reference) or reference <= 0:
        return _observation("rvol", "quality", 0.5, "unavailable", "RVOL reference volume is invalid")
    ratio = current / reference
    family = str(context.candidate.get("market_family") or "trend").lower()
    if family in {"mean_reversion", "reversal"}:
        value = max(0.5, min(1.0, 0.5 + max(0.0, ratio - 1.0) / 2.0))
    else:
        value = max(0.0, min(1.0, (ratio - 0.5) / 1.5))
    status = "support" if value > 0.55 else "contradict" if value < 0.45 else "neutral"
    return _observation("rvol", "quality", value, status, "5m relative volume evaluated", raw_inputs={"current_volume": current, "reference_volume": reference, "rvol": ratio, "lookback_bars": lookback})


def funding_overheating(context: ScoreContext) -> ComponentObservation:
    rates = _frame_column(context.market_bars, "funding_rate")
    available = _frame_column(context.market_bars, "funding_rate_available")
    if not available:
        available = [value is not None for value in rates]
    pairs = [(float(rate), bool(flag)) for rate, flag in zip(rates, available) if _finite(rate) and flag]
    lookback = int(getattr(config, "TRADE_QUALITY_FUNDING_LOOKBACK_BARS", 288))
    minimum = int(getattr(config, "TRADE_QUALITY_FUNDING_MIN_BARS", 32))
    if len(pairs) < minimum:
        return _observation("funding_overheating", "quality", 0.5, "unavailable", "funding history is unavailable", raw_inputs={"observations": len(pairs), "minimum": minimum})
    history = [rate for rate, _ in pairs[-lookback:]]
    current = history[-1]
    magnitudes = sorted(abs(rate) for rate in history[:-1] if _finite(rate))
    if len(magnitudes) < minimum:
        return _observation("funding_overheating", "quality", 0.5, "unavailable", "funding reference history is unavailable")
    rank = sum(value <= abs(current) for value in magnitudes) / len(magnitudes)
    heat = max(0.0, min(1.0, (rank - 0.75) / 0.25))
    direction = str(context.candidate.get("direction") or "").lower()
    family = str(context.candidate.get("market_family") or "trend").lower()
    same_crowded = (current > 0 and direction == "long") or (current < 0 and direction == "short")
    if heat <= 0:
        value = 0.5
    elif family in {"mean_reversion", "reversal"} and same_crowded:
        value = 0.5 + 0.5 * heat
    elif same_crowded:
        value = 0.5 - 0.5 * heat
    else:
        value = 0.5 + 0.5 * heat
    status = "support" if value > 0.55 else "contradict" if value < 0.45 else "neutral"
    return _observation("funding_overheating", "quality", value, status, "funding crowding evaluated", raw_inputs={"current_rate": current, "percentile_rank": rank, "heat": heat, "lookback_bars": lookback})


def contradiction(context: ScoreContext) -> ComponentObservation:
    long_distance, short_distance = _zone_distances(context)
    direction = str(context.candidate.get("direction") or "").lower()
    opposite = short_distance if direction == "long" else long_distance if direction == "short" else None
    value = 0.0 if opposite is not None and opposite <= 0.75 else 1.0
    status = "contradict" if value == 0.0 else "neutral"
    return _observation("contradiction", "quality", value, status, "opposing structural context evaluated", raw_inputs={"opposite_distance_atr": opposite})


COMPONENTS: tuple[Callable[[ScoreContext], ComponentObservation], ...] = (
    identity_validity,
    price_geometry,
    reward_risk,
    stop_distance,
    freshness_readiness,
    symbol_account_policy,
    structural_stop,
    htf_bias,
    zone_context,
    alignment,
    freshness_quality,
    same_direction_agreement,
    rvol,
    funding_overheating,
    contradiction,
)
