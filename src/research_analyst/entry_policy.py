"""Session and market-context metadata for shadow entry-policy research."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import config


POLICY_VERSION = "entry-policy-v1"
_SESSION_BOUNDARIES = ((0, "asia"), (8, "europe"), (13, "us"), (21, "rollover"))
_NAMED_SESSIONS = frozenset({"asia", "europe", "us"})
_FAMILY_BY_STRATEGY = {
    "accumulation_base_v2": "mean_reversion",
    "bb_rsi_meanrev_v1": "mean_reversion",
    "failed_break_v3": "reversal",
    "liquidity_sweep_reversal_v1": "reversal",
    "mtf_exhaustion_reversal_v1": "reversal",
    "rsi_reclaim_v1": "reversal",
    "ema7_26_cross_hammer_shooting_star_1h_adx_v1": "reversal",
}
_TREND_STRATEGY_MARKERS = (
    "continuation", "breakout", "follower", "pullback", "trend", "wall",
    "impulse", "stack", "double_touch", "ema9_adx",
)
_VALID_FAMILIES = frozenset({"trend", "mean_reversion", "reversal", "unknown"})
_ENVIRONMENT_ALIASES = {
    "trending": "trend",
    "trending_up": "trend",
    "trending_down": "trend",
    "trend": "trend",
    "ranging": "range",
    "range": "range",
    "mean_reverting": "range",
    "high_vol": "shock",
    "high_volatility": "shock",
    "shock": "shock",
    "reversal": "reversal",
}


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except (TypeError, ValueError, OverflowError):
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _normalise(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def session_context(observed_at: Any) -> dict[str, Any]:
    """Return the UTC session and the configured opening-cooldown phase."""
    timestamp = _utc(observed_at)
    if timestamp is None:
        return {
            "session_name": "unknown",
            "session_phase": "unknown",
            "session_elapsed_minutes": None,
        }

    hour = timestamp.hour
    boundary_hour, session_name = max(
        boundary for boundary in _SESSION_BOUNDARIES if boundary[0] <= hour
    )
    session_start = timestamp.replace(hour=boundary_hour, minute=0, second=0, microsecond=0)
    elapsed = int((timestamp - session_start).total_seconds() // 60)
    cooldown = int(getattr(config, "REGIME_SESSION_COOLDOWN_MINUTES", 30))
    if session_name in _NAMED_SESSIONS and elapsed < cooldown:
        phase = "cooldown"
    elif session_name in _NAMED_SESSIONS:
        phase = "active"
    else:
        phase = "off_hours"
    return {
        "session_name": session_name,
        "session_phase": phase,
        "session_elapsed_minutes": elapsed,
    }


def strategy_family(candidate: dict[str, Any]) -> str:
    """Classify a strategy for analysis, without changing its execution rules."""
    explicit = _normalise(candidate.get("market_family"))
    if explicit in _VALID_FAMILIES:
        return explicit

    strategy_id = _normalise(candidate.get("strategy_id"))
    if strategy_id in _FAMILY_BY_STRATEGY:
        return _FAMILY_BY_STRATEGY[strategy_id]
    if any(marker in strategy_id for marker in _TREND_STRATEGY_MARKERS):
        return "trend"

    setup_class = _normalise(candidate.get("setup_class"))
    if "reversal" in setup_class or "failed_break" in setup_class or "exhaustion" in setup_class:
        return "reversal"
    if "meanrev" in setup_class or "mean_reversion" in setup_class:
        return "mean_reversion"
    return "unknown"


def environment_state(candidate: dict[str, Any]) -> str:
    """Read an explicit regime label; missing labels remain unknown."""
    snapshot = candidate.get("feature_snapshot") or {}
    for value in (
        candidate.get("environment_state"),
        candidate.get("market_environment"),
        candidate.get("market_regime"),
        snapshot.get("environment_state") if isinstance(snapshot, dict) else None,
        snapshot.get("market_environment") if isinstance(snapshot, dict) else None,
        snapshot.get("market_regime") if isinstance(snapshot, dict) else None,
        snapshot.get("regime") if isinstance(snapshot, dict) else None,
    ):
        normalized = _normalise(value)
        if normalized in _ENVIRONMENT_ALIASES:
            return _ENVIRONMENT_ALIASES[normalized]
    return "unknown"


def evaluate_entry_policy(candidate: dict[str, Any], mode: str | None = None) -> dict[str, Any]:
    """Record candidate context; regime/session enforcement happens upstream."""
    configured_mode = mode or getattr(config, "ENTRY_POLICY_MODE", "shadow")
    configured_mode = _normalise(configured_mode)
    if configured_mode not in {"off", "shadow", "enforce"}:
        raise ValueError("entry policy mode must be off, shadow, or enforce")

    session = session_context(candidate.get("observed_at"))
    reasons = []
    if session["session_phase"] == "cooldown":
        reasons.append("session_open_cooldown")

    if configured_mode == "off":
        decision = "disabled"
    elif reasons:
        decision = "would_block"
    else:
        decision = "allow"
    return {
        "policy_version": POLICY_VERSION,
        "mode": configured_mode,
        "decision": decision,
        "enforced_block": False,
        "session_name": session["session_name"],
        "session_phase": session["session_phase"],
        "session_elapsed_minutes": session["session_elapsed_minutes"],
        "market_family": strategy_family(candidate),
        "environment_state": environment_state(candidate),
        "reasons": reasons,
    }


def annotate_candidate(candidate: dict[str, Any], mode: str | None = None) -> dict[str, Any]:
    """Return a candidate copy with durable policy context attached."""
    annotated = dict(candidate)
    policy = evaluate_entry_policy(annotated, mode=mode)
    annotated["market_family"] = policy["market_family"]
    annotated["entry_policy"] = policy
    return annotated


def persist_observation(conn, event: dict[str, Any]) -> None:
    """Persist one policy observation without affecting delivery or admission."""
    policy = event.get("entry_policy")
    candidate_id = event.get("candidate_id")
    if not isinstance(policy, dict) or not candidate_id:
        return
    conn.execute(
        """
        INSERT OR REPLACE INTO entry_policy_observations (
            candidate_id, observed_at, policy_version, mode, decision,
            session_name, session_phase, session_elapsed_minutes,
            market_family, environment_state, reasons_json, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            candidate_id,
            event.get("observed_at"),
            policy.get("policy_version", POLICY_VERSION),
            policy.get("mode", "shadow"),
            policy.get("decision", "allow"),
            policy.get("session_name", "unknown"),
            policy.get("session_phase", "unknown"),
            policy.get("session_elapsed_minutes"),
            policy.get("market_family", "unknown"),
            policy.get("environment_state", "unknown"),
            json.dumps(policy.get("reasons", []), sort_keys=True),
        ),
    )
