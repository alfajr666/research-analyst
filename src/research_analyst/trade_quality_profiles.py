"""Versioned, regime-aware weight profiles for trade-quality scoring."""

from __future__ import annotations

from typing import Mapping


QUALITY_SCORE_POLICY_VERSION = "trade-quality-v1"
QUALITY_SCORE_PROFILE_VERSION = "trade-quality-profiles-v1"
QUALITY_COMPONENTS = (
    "htf_bias",
    "zone_context",
    "alignment",
    "freshness_quality",
    "same_direction_agreement",
    "rvol",
    "funding_overheating",
    "contradiction",
)


NEUTRAL_WEIGHTS = {
    "htf_bias": 0.125,
    "zone_context": 0.125,
    "alignment": 0.125,
    "freshness_quality": 0.125,
    "same_direction_agreement": 0.125,
    "rvol": 0.125,
    "funding_overheating": 0.125,
    "contradiction": 0.125,
}

PROFILE_WEIGHTS: dict[str, dict[str, float]] = {
    "neutral": dict(NEUTRAL_WEIGHTS),
    "trend": {
        "htf_bias": 0.18,
        "zone_context": 0.10,
        "alignment": 0.18,
        "freshness_quality": 0.10,
        "same_direction_agreement": 0.08,
        "rvol": 0.18,
        "funding_overheating": 0.08,
        "contradiction": 0.10,
    },
    "mean_reversion": {
        "htf_bias": 0.10,
        "zone_context": 0.20,
        "alignment": 0.08,
        "freshness_quality": 0.10,
        "same_direction_agreement": 0.12,
        "rvol": 0.10,
        "funding_overheating": 0.18,
        "contradiction": 0.12,
    },
    "reversal": {
        "htf_bias": 0.08,
        "zone_context": 0.18,
        "alignment": 0.08,
        "freshness_quality": 0.10,
        "same_direction_agreement": 0.12,
        "rvol": 0.16,
        "funding_overheating": 0.18,
        "contradiction": 0.10,
    },
}


def _normalise_weights(weights: Mapping[str, float]) -> dict[str, float]:
    values = {key: max(0.0, float(weights.get(key, 0.0))) for key in QUALITY_COMPONENTS}
    total = sum(values.values())
    if total <= 0:
        return dict(NEUTRAL_WEIGHTS)
    return {key: value / total for key, value in values.items()}


def profile_weights(profile: str) -> dict[str, float]:
    """Return a defensive, normalized copy of a named profile."""
    return _normalise_weights(PROFILE_WEIGHTS.get(str(profile), NEUTRAL_WEIGHTS))


def effective_profile_weights(family: str, family_weight: float) -> tuple[str, dict[str, float]]:
    """Blend a family profile with neutral weights using regime intensity."""
    normalized_family = str(family or "neutral").strip().lower()
    if normalized_family not in PROFILE_WEIGHTS:
        normalized_family = "neutral"
    intensity = max(0.0, min(float(family_weight), 1.0))
    family_values = profile_weights(normalized_family)
    weights = {
        key: (intensity * family_values[key]) + ((1.0 - intensity) * NEUTRAL_WEIGHTS[key])
        for key in QUALITY_COMPONENTS
    }
    return f"{normalized_family}-v1", _normalise_weights(weights)
