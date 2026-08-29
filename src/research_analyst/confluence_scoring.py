"""Shared confluence scoring helpers (ADR: strategy confluence scoring)."""

from __future__ import annotations

from typing import Mapping


def clamp01(value: float) -> float:
    return max(0.0, min(float(value), 1.0))


def zone_proximity_bin(distance_atr: float, same: float = 0.25, near: float = 0.75) -> str:
    """ATR-normalized distance → same_zone | near_zone | far."""
    d = float(distance_atr)
    if d <= same:
        return "same_zone"
    if d <= near:
        return "near_zone"
    return "far"


def proximity_score(distance_atr: float, same: float = 0.25, near: float = 0.75) -> float:
    """Map ATR distance into [0, 1] using the ADR bins."""
    bin_name = zone_proximity_bin(distance_atr, same=same, near=near)
    if bin_name == "same_zone":
        return 1.0 - (distance_atr / same) * 0.15 if same > 0 else 1.0
    if bin_name == "near_zone":
        span = near - same
        return 0.85 - ((distance_atr - same) / span) * 0.55 if span > 0 else 0.5
    return 0.0


def weighted_confluence(
    components: Mapping[str, float],
    weights: Mapping[str, float],
) -> tuple[float, dict[str, float]]:
    """Return (raw score in roughly [0,1], weighted component contributions)."""
    weighted: dict[str, float] = {}
    total_w = 0.0
    total = 0.0
    for name, weight in weights.items():
        w = float(weight)
        if w == 0:
            continue
        raw = float(components.get(name, 0.0))
        if name.startswith("-") or name.endswith("_penalty") or name == "contradiction_penalty":
            contrib = -abs(w) * clamp01(raw)
        else:
            contrib = w * clamp01(raw)
        weighted[name] = round(contrib, 6)
        total += contrib
        total_w += abs(w)
    if total_w <= 0:
        return 0.0, weighted
    # Normalize by sum of absolute weights so score sits near [0, 1]
    score = clamp01(total / total_w if total >= 0 else 0.0)
    return round(score, 6), weighted


def confidence_from_confluence(confluence_score: float) -> tuple[float, str]:
    """Map confluence → uncalibrated confidence in [0, 1]."""
    return round(clamp01(confluence_score), 4), "uncalibrated"
