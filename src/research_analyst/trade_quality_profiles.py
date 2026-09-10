"""Versioned, regime-aware weight profiles for trade-quality scoring."""

from __future__ import annotations

from typing import Mapping


QUALITY_SCORE_POLICY_VERSION = "trade-quality-v2"
QUALITY_SCORE_PROFILE_VERSION = "trade-quality-profiles-v2"
QUALITY_COMPONENTS = (
    "identity_validity",
    "price_geometry",
    "reward_risk",
    "stop_distance",
    "symbol_account_policy",
    "structural_stop",
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
    "identity_validity": 0.05,
    "price_geometry": 0.05,
    "reward_risk": 0.05,
    "stop_distance": 0.05,
    "symbol_account_policy": 0.05,
    "structural_stop": 0.05,
    "htf_bias": 0.0875,
    "zone_context": 0.0875,
    "alignment": 0.0875,
    "freshness_quality": 0.0875,
    "same_direction_agreement": 0.0875,
    "rvol": 0.0875,
    "funding_overheating": 0.0875,
    "contradiction": 0.0875,
}

PROFILE_WEIGHTS: dict[str, dict[str, float]] = {
    "neutral": dict(NEUTRAL_WEIGHTS),
    "trend": {
        "identity_validity": 0.05,
        "price_geometry": 0.05,
        "reward_risk": 0.05,
        "stop_distance": 0.05,
        "symbol_account_policy": 0.05,
        "structural_stop": 0.05,
        "htf_bias": 0.126,
        "zone_context": 0.07,
        "alignment": 0.126,
        "freshness_quality": 0.07,
        "same_direction_agreement": 0.056,
        "rvol": 0.126,
        "funding_overheating": 0.056,
        "contradiction": 0.07,
    },
    "mean_reversion": {
        "identity_validity": 0.05,
        "price_geometry": 0.05,
        "reward_risk": 0.05,
        "stop_distance": 0.05,
        "symbol_account_policy": 0.05,
        "structural_stop": 0.05,
        "htf_bias": 0.07,
        "zone_context": 0.14,
        "alignment": 0.056,
        "freshness_quality": 0.07,
        "same_direction_agreement": 0.084,
        "rvol": 0.07,
        "funding_overheating": 0.126,
        "contradiction": 0.084,
    },
    "reversal": {
        "identity_validity": 0.05,
        "price_geometry": 0.05,
        "reward_risk": 0.05,
        "stop_distance": 0.05,
        "symbol_account_policy": 0.05,
        "structural_stop": 0.05,
        "htf_bias": 0.056,
        "zone_context": 0.126,
        "alignment": 0.056,
        "freshness_quality": 0.07,
        "same_direction_agreement": 0.084,
        "rvol": 0.112,
        "funding_overheating": 0.126,
        "contradiction": 0.07,
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
