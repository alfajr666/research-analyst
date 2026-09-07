"""Evaluator-owned strategy scope routing.

Strategies receive an already-resolved symbol scope and remain unaware of
rotation, watchlist, regime, and account policy implementation details.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


SCOPE_CONTRACT_VERSION = "strategy-scope-v1"
_FAMILIES = {"trend", "mean_reversion", "reversal"}


def _assets(values: Iterable[object]) -> list[str]:
    return sorted({str(value).strip().upper() for value in (values or []) if str(value).strip()})


def build_strategy_scope(
    assets: Iterable[object],
    *,
    plugin_id: str,
    market_family: str,
    cutoff: datetime,
    feed_metadata: Mapping[str, Any] | None = None,
    regime_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve one deterministic, cutoff-bound scope for a plugin.

    Account-symbol policy is deliberately not consulted here. It remains a
    downstream admission safety gate until separately specified.
    """
    universe = _assets(assets)
    metadata = dict(feed_metadata or {})
    regime = dict(regime_scope or {})
    mode = str(regime.get("mode", "off")).strip().lower()
    allowed = list(universe)
    excluded: list[dict[str, str]] = []

    if mode == "enforce":
        ready = set(_assets(regime.get("allowed_assets", [])))
        for asset in universe:
            if asset not in ready:
                reason = "regime_missing" if asset in set(_assets(regime.get("missing_assets", []))) else "regime_blocked"
                excluded.append({"asset": asset, "reason": reason})
        allowed = [asset for asset in allowed if asset in ready]
        if market_family not in _FAMILIES:
            excluded.extend({"asset": asset, "reason": "unknown_strategy_family"} for asset in allowed)
            allowed = []
        else:
            family_map = regime.get("family_assets") or {}
            family_assets = set(_assets(family_map.get(market_family, [])))
            for asset in allowed:
                if asset not in family_assets:
                    excluded.append({"asset": asset, "reason": f"regime_family:{market_family}"})
            allowed = [asset for asset in allowed if asset in family_assets]
    elif mode not in {"off", "shadow"}:
        raise ValueError(f"unsupported regime scope mode: {mode}")

    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    cutoff_text = cutoff.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    feed_id = str(metadata.get("feed_id") or "unknown")
    effective_version = str(
        metadata.get("effective_universe_version")
        or metadata.get("feed_id")
        or "unknown"
    )
    regime_cutoff = regime.get("cutoff_at") or regime.get("regime_cutoff_at")
    return {
        "evaluation_cutoff": cutoff_text,
        "effective_universe_version": effective_version,
        "feed_id": feed_id,
        "regime_scope_id": f"{feed_id}:{regime_cutoff or 'none'}:{mode}",
        "regime_cutoff_at": regime_cutoff,
        "strategy_id": str(plugin_id),
        "market_family": str(market_family),
        "allowed_assets": allowed,
        "excluded_assets": excluded,
        "scope_contract_version": SCOPE_CONTRACT_VERSION,
    }
