"""Reaction-oriented scorer and decayed anchored volume profile.

The module is deliberately pure at its public seam.  Database/network adapters
load cutoff-bound bars, funding, and OI; this module builds the profile and
returns auditable observations.  Rollout selection is kept here so callers do
not grow independent shadow/enforce branches.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from statistics import median
from typing import Any, Mapping, Sequence


PROFILE_VERSION = "reaction-profile-v1"
SCORER_VERSION = "reaction-scorer-v3"
WEIGHTS = {
    "value-reaction-v1": 0.50,
    "participation-v1": 0.20,
    "oi-participation-v1": 0.15,
    "crowding-v1": 0.15,
}
ANCHOR_HALF_LIFE_HOURS = 72.0
PROFILE_HALF_LIFE_HOURS = 72.0
MAX_ANCHOR_AGE_HOURS = 168.0
MIN_PROFILE_BARS = 72
NORMAL_PROFILE_BARS = 288


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _positive(value: Any) -> bool:
    return _finite(value) and float(value) > 0


def _rows(frame: Any) -> list[dict[str, Any]]:
    if isinstance(frame, Mapping):
        keys = list(frame)
        values = [frame.get(key) for key in keys]
        if values and all(isinstance(value, Sequence) and not isinstance(value, (str, bytes)) for value in values):
            return [{key: frame[key][index] for key in keys} for index in range(min(len(frame[key]) for key in keys))]
    return [dict(row) for row in (frame or []) if isinstance(row, Mapping)]


def _bar_start(row: Mapping[str, Any]) -> int | None:
    try:
        raw = row.get("source_start", row.get("timestamp"))
        if isinstance(raw, datetime):
            if raw.tzinfo is None:
                raw = raw.replace(tzinfo=timezone.utc)
            return int(raw.timestamp() * 1000)
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value


def _bar_end(row: Mapping[str, Any]) -> int | None:
    try:
        raw = row.get("source_end", row.get("timestamp"))
        if isinstance(raw, datetime):
            if raw.tzinfo is None:
                raw = raw.replace(tzinfo=timezone.utc)
            return int(raw.timestamp() * 1000)
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value


def _looks_like_15m(bars: Sequence[Mapping[str, Any]]) -> bool:
    """Distinguish canonical 5m bars from already-resampled 15m bars.

    Epoch alignment alone is insufficient: every third 5m bar is also aligned
    to a 15m boundary.  Consecutive spacing is the authoritative discriminator.
    """
    starts = [_bar_start(row) for row in bars]
    starts = [value for value in starts if value is not None]
    if len(starts) < 2:
        return False
    deltas = [right - left for left, right in zip(starts, starts[1:])]
    if any(delta == 300_000 for delta in deltas):
        return False
    return any(delta == 900_000 for delta in deltas) and all(value % 900_000 == 0 for value in starts)


def _timestamp_ms(value: Any) -> int | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        numeric = int(value)
        return numeric * 1000 if abs(numeric) < 100_000_000_000 else numeric
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
        except ValueError:
            return None
    return None


def _cutoff_funding(rows: Sequence[Mapping[str, Any]], cutoff: int | None) -> list[dict[str, Any]]:
    """Filter, deduplicate, and sort funding observations at a cutoff."""
    deduped: dict[int, dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []
    cutoff_ms = _timestamp_ms(cutoff) if cutoff is not None else None
    for raw in rows:
        row = dict(raw)
        if not _finite(row.get("rate")):
            continue
        stamp = _timestamp_ms(row.get("source_at", row.get("source_end")))
        if cutoff_ms is not None and stamp is not None and stamp > cutoff_ms:
            continue
        if stamp is None:
            unkeyed.append(row)
        else:
            deduped[stamp] = row
    return [*sorted(deduped.values(), key=lambda row: _timestamp_ms(row.get("source_at", row.get("source_end"))) or 0), *unkeyed]


def resample_5m_to_15m(bars: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate only exact, contiguous completed 5m groups."""
    ordered = sorted((dict(row) for row in bars), key=lambda row: _bar_start(row) or -1)
    out: list[dict[str, Any]] = []
    group: list[dict[str, Any]] = []
    for row in ordered:
        start = _bar_start(row)
        end = _bar_end(row)
        if start is None or end is None or not all(_finite(row.get(k)) for k in ("open", "high", "low", "close", "volume")):
            group = []
            continue
        if not group:
            if start % 900_000 != 0:
                continue
            group = [row]
            continue
        expected = (_bar_start(group[-1]) or 0) + 300_000
        if start != expected or start // 900_000 != (_bar_start(group[0]) or 0) // 900_000:
            group = [row] if start % 900_000 == 0 else []
            continue
        group.append(row)
        if len(group) == 3:
            first, last = group[0], group[-1]
            out.append({
                "source_start": _bar_start(first),
                "source_end": _bar_end(last),
                "open": float(first["open"]),
                "high": max(float(item["high"]) for item in group),
                "low": min(float(item["low"]) for item in group),
                "close": float(last["close"]),
                "volume": sum(max(0.0, float(item["volume"])) for item in group),
            })
            group = []
    return out


def _true_ranges(bars: Sequence[Mapping[str, Any]]) -> list[float]:
    ranges: list[float] = []
    previous_close: float | None = None
    for bar in bars:
        high, low, close = (float(bar[k]) for k in ("high", "low", "close"))
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)) if previous_close is not None else high - low)
        previous_close = close
    return ranges


def _atr14(bars: Sequence[Mapping[str, Any]]) -> float | None:
    if len(bars) < 14:
        return None
    values = _true_ranges(bars)
    return sum(values[-14:]) / 14.0


def _hour_buckets(bars: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    current: list[Mapping[str, Any]] = []
    for bar in bars:
        start = _bar_start(bar)
        if start is None:
            continue
        if not current:
            if start % 3_600_000 == 0:
                current = [bar]
            continue
        expected = (_bar_start(current[-1]) or 0) + 900_000
        if start != expected or start // 3_600_000 != (_bar_start(current[0]) or 0) // 3_600_000:
            current = [bar] if start % 3_600_000 == 0 else []
            continue
        current.append(bar)
        if len(current) == 4:
            groups.append({
                "hour_start": _bar_start(current[0]),
                "hour_end": _bar_end(current[-1]),
                "volume": sum(max(0.0, float(item.get("volume", 0.0))) for item in current),
            })
            current = []
    return groups


def _decay(value: float, age_hours: float, half_life: float) -> float:
    return float(value) * 2.0 ** (-max(0.0, age_hours) / half_life)


def _select_anchor(hourly: Sequence[Mapping[str, Any]], previous: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not hourly:
        return None
    latest_end = int(hourly[-1]["hour_end"])
    if previous and _finite(previous.get("original_volume")) and _finite(previous.get("hour_end")):
        age = max(0.0, (latest_end - int(previous["hour_end"])) / 3_600_000)
        if age <= MAX_ANCHOR_AGE_HOURS:
            anchor = dict(previous)
            anchor["current_strength"] = _decay(float(anchor["original_volume"]), age, ANCHOR_HALF_LIFE_HOURS)
            for item in hourly:
                if int(item["hour_end"]) <= int(anchor["hour_end"]):
                    continue
                strength = float(item["volume"])
                if strength > float(anchor["current_strength"]):
                    anchor = {
                        "hour_start": item["hour_start"], "hour_end": item["hour_end"],
                        "original_volume": strength, "current_strength": strength,
                        "replacement_reason": "new_hour_exceeded_decayed_anchor",
                    }
                else:
                    anchor["current_strength"] = _decay(
                        float(anchor["original_volume"]),
                        max(0.0, (int(item["hour_end"]) - int(anchor["hour_end"])) / 3_600_000),
                        ANCHOR_HALF_LIFE_HOURS,
                    )
            return anchor
    candidates = sorted(hourly[-72:], key=lambda item: (-float(item["volume"]), -int(item["hour_end"])))
    item = candidates[0]
    return {
        "hour_start": item["hour_start"], "hour_end": item["hour_end"],
        "original_volume": float(item["volume"]), "current_strength": float(item["volume"]),
        "replacement_reason": "initial_seed",
    }


def _allocate_profile(bars: Sequence[Mapping[str, Any]], width: float) -> tuple[dict[int, float], int | None]:
    volumes: dict[int, float] = {}
    for bar in bars:
        low, high, close = float(bar["low"]), float(bar["high"]), float(bar["close"])
        typical = (high + low + close) / 3.0
        first = math.floor(low / width)
        last = math.floor(high / width)
        indices = list(range(first, last + 1)) or [math.floor(typical / width)]
        weights = [1.0 / (1.0 + abs((index + 0.5) * width - typical) / width) for index in indices]
        scale = max(0.0, float(bar["volume"])) * float(bar.get("decay_weight", 1.0)) / sum(weights)
        for index, weight in zip(indices, weights):
            volumes[index] = volumes.get(index, 0.0) + scale * weight
    poc = max(volumes, key=volumes.get) if volumes else None
    return volumes, poc


def _value_area(volumes: Mapping[int, float], poc_index: int, fraction: float = 0.70) -> tuple[int, int]:
    total = sum(max(0.0, value) for value in volumes.values())
    indices = sorted(volumes)
    poc_position = indices.index(poc_index)
    low_position = high_position = poc_position
    covered = max(0.0, float(volumes.get(poc_index, 0.0)))
    while covered < fraction * total:
        left = indices[low_position - 1] if low_position > 0 else None
        right = indices[high_position + 1] if high_position + 1 < len(indices) else None
        left_value = float(volumes[left]) if left is not None else -1.0
        right_value = float(volumes[right]) if right is not None else -1.0
        if left_value < 0 and right_value < 0:
            break
        if left_value >= right_value:
            chosen = left
            low_position -= 1
        else:
            chosen = right
            high_position += 1
        covered += max(0.0, float(volumes.get(chosen, 0.0)))
    return indices[low_position], indices[high_position]


def _hvn_nodes(volumes: Mapping[int, float], width: float, poc_index: int) -> list[dict[str, Any]]:
    ordered = sorted(volumes.values())
    threshold = ordered[max(0, int(math.ceil(0.75 * len(ordered))) - 1)]
    qualifying = [index for index, value in volumes.items()
                  if value >= threshold and value > volumes.get(index - 1, -1.0)
                  and value > volumes.get(index + 1, -1.0)
                  and value >= 0.10 * volumes[poc_index]]
    groups: list[list[int]] = []
    for index in sorted(qualifying):
        if groups and index == groups[-1][-1] + 1:
            groups[-1].append(index)
        else:
            groups.append([index])
    nodes = []
    for group in groups:
        volume = sum(float(volumes[index]) for index in group)
        center = sum((index + 0.5) * width * float(volumes[index]) for index in group) / max(volume, 1e-12)
        nodes.append({"node_id": f"hvn:{group[0]}:{group[-1]}", "type": "hvn", "price": center, "volume": volume})
    return sorted(nodes, key=lambda node: (-node["volume"], node["price"]))[:8]


def build_value_profile(
    asset: str, evaluation_cutoff: int | float, completed_15m_bars: Sequence[Mapping[str, Any]],
    previous_anchor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one cutoff-bound, decayed three-day value profile."""
    try:
        cutoff = int(evaluation_cutoff)
    except (TypeError, ValueError):
        return {"status": "unavailable", "reason": "invalid cutoff", "profile_version": PROFILE_VERSION}
    bars = sorted(
        [dict(row) for row in completed_15m_bars if _bar_end(row) is not None and _bar_end(row) <= cutoff],
        key=lambda row: _bar_start(row) or -1,
    )[-NORMAL_PROFILE_BARS:]
    if len(bars) < MIN_PROFILE_BARS:
        return {"status": "unavailable", "reason": f"only {len(bars)} complete 15m bars", "profile_version": PROFILE_VERSION}
    if any((_bar_start(row) is None or _bar_end(row) is None) for row in bars):
        return {"status": "unavailable", "reason": "malformed bars", "profile_version": PROFILE_VERSION}
    hourly = _hour_buckets(bars)
    anchor = _select_anchor(hourly, previous_anchor)
    if anchor is None:
        return {"status": "unavailable", "reason": "no complete hourly anchor", "profile_version": PROFILE_VERSION}
    anchor_start = int(anchor["hour_start"])
    latest_end = int(bars[-1]["source_end"])
    profile_bars: list[dict[str, Any]] = []
    for bar in bars:
        if int(bar["source_start"]) < anchor_start:
            continue
        age_hours = max(0.0, (latest_end - int(bar["source_end"])) / 3_600_000)
        row = dict(bar)
        row["decay_weight"] = 2.0 ** (-age_hours / PROFILE_HALF_LIFE_HOURS)
        profile_bars.append(row)
    atr = _atr14(profile_bars)
    close = float(profile_bars[-1]["close"]) if profile_bars else 0.0
    if atr is None or not _positive(atr) or not _positive(close):
        return {"status": "unavailable", "reason": "ATR/profile history unavailable", "profile_version": PROFILE_VERSION}
    width = max(0.10 * atr, 0.0005 * close)
    volumes, poc_index = _allocate_profile(profile_bars, width)
    total = sum(volumes.values())
    if not volumes or total <= 0 or poc_index is None:
        return {"status": "unavailable", "reason": "zero decayed volume", "profile_version": PROFILE_VERSION}
    poc_price = (poc_index + 0.5) * width
    value_area_low, value_area_high = _value_area(volumes, poc_index)
    hvn_nodes = _hvn_nodes(volumes, width, poc_index)
    nodes = [{"node_id": f"poc:{poc_index}", "type": "poc", "price": poc_price, "volume": volumes[poc_index]}] + hvn_nodes
    return {
        "status": "ok", "asset": asset, "cutoff": cutoff, "bars": len(bars),
        "profile_bars": len(profile_bars), "profile_version": PROFILE_VERSION,
        "anchor_hour_start": anchor["hour_start"], "anchor_hour_end": anchor["hour_end"],
        "anchor": anchor, "atr14_15m": atr, "bin_width": width,
        "poc": poc_price, "value_area_low": (value_area_low + 0.5) * width,
        "value_area_high": (value_area_high + 0.5) * width,
        "hvn_nodes": hvn_nodes, "nodes": nodes,
        "total_volume": total, "volumes": volumes,
    }


def _reaction_bar(candidate: Mapping[str, Any], bars: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    supplied = candidate.get("reaction_bar")
    if isinstance(supplied, Mapping):
        return supplied
    return bars[-1] if bars else None


def score_value_reaction(candidate: Mapping[str, Any], profile: Mapping[str, Any], bars: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    if profile.get("status") != "ok":
        return {"value": 0.5, "status": "unavailable", "reason": "value profile unavailable"}
    entry = candidate.get("entry_price", candidate.get("entry"))
    direction = str(candidate.get("direction") or "").lower()
    atr = float(profile.get("atr14_15m") or 0.0)
    bar = _reaction_bar(candidate, bars)
    if not _positive(entry) or not _positive(atr) or not isinstance(bar, Mapping):
        return {"value": 0.5, "status": "unavailable", "reason": "reaction inputs unavailable"}
    nodes = profile.get("nodes") or []
    node = min(nodes, key=lambda item: (abs(float(entry) - float(item["price"])) / atr, 0 if item["type"] == "poc" else 1, -float(item.get("volume", 0.0)), float(item["price"])))
    distance_atr = abs(float(entry) - float(node["price"])) / atr
    proximity = math.exp(-0.5 * (distance_atr / 0.35) ** 2)
    low, high, opening, close = (float(bar[k]) for k in ("low", "high", "open", "close"))
    band = 0.15 * atr
    location = (close - low) / max(1e-12, high - low)
    support = ((direction == "long" and low <= float(node["price"]) + band and close > float(node["price"]) and location >= 0.60 and (close > opening or close >= float(bar.get("previous_close", opening))))
              or (direction == "short" and high >= float(node["price"]) - band and close < float(node["price"]) and (1.0 - location) >= 0.60 and (close < opening or close <= float(bar.get("previous_close", opening)))))
    opposing = ((direction == "long" and low <= float(node["price"]) + band and close < float(node["price"]) and (1.0 - location) >= 0.60)
                or (direction == "short" and high >= float(node["price"]) - band and close > float(node["price"]) and location >= 0.60))
    if support:
        value, status = 0.50 + 0.50 * proximity, "support"
    elif opposing:
        value, status = 0.50 - 0.50 * proximity, "contradict"
    elif distance_atr <= 0.75:
        value, status = 0.55, "neutral"
    else:
        value, status = 0.50, "neutral"
    return {"value": round(max(0.0, min(1.0, value)), 6), "status": status,
            "reason": "directional value reaction evaluated", "node_id": node["node_id"],
            "node_type": node["type"], "node_price": node["price"],
            "distance_atr": distance_atr, "proximity": proximity, "touch_band": band}


def _price_return_for_window(bars: Sequence[Mapping[str, Any]], horizon_ms: int) -> float | None:
    if not bars:
        return None
    latest = bars[-1]
    latest_end = _bar_end(latest)
    if latest_end is None or not _positive(latest.get("close")):
        return None
    eligible = [bar for bar in bars if _bar_end(bar) is not None and int(latest_end) - int(_bar_end(bar)) >= horizon_ms]
    if not eligible or not _positive(eligible[-1].get("close")):
        return None
    return float(latest["close"]) / float(eligible[-1]["close"]) - 1.0


def score_oi_confirmation(candidate: Mapping[str, Any], observations: Sequence[Mapping[str, Any]], bars: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    valid = sorted([row for row in observations if _finite(row.get("open_interest")) and float(row["open_interest"]) > 0 and (_bar_end(row) is None or True)], key=lambda row: str(row.get("source_at", row.get("source_end", ""))))
    if len(valid) < 32:
        return {"value": 0.5, "status": "unavailable", "reason": "OI history is unavailable", "observations": len(valid)}
    direction = str(candidate.get("direction") or "").lower()
    sign = 1.0 if direction == "long" else -1.0 if direction == "short" else 0.0
    if sign == 0:
        return {"value": 0.5, "status": "unavailable", "reason": "candidate direction is invalid", "observations": len(valid)}
    values = [float(row["open_interest"]) for row in valid]
    scores: list[float] = []
    states: list[str] = []
    for horizon_ms, horizon_name, horizon_weight in ((15 * 60_000, "15m", 0.60), (60 * 60_000, "60m", 0.40)):
        current = values[-1]
        target_index = max(0, len(values) - max(2, int(horizon_ms / (5 * 60_000))))
        base = values[target_index]
        oi_change = current / base - 1.0 if base > 0 else 0.0
        price_return = _price_return_for_window(bars, horizon_ms)
        aligned_price = sign * float(price_return or 0.0)
        if aligned_price == 0.0 or abs(oi_change) < 0.001:
            value, state = 0.5, "neutral"
        elif aligned_price > 0 and oi_change > 0:
            value, state = 0.80, "support"
        elif aligned_price > 0 and oi_change < 0:
            value, state = 0.55, "neutral"
        elif aligned_price < 0 and oi_change > 0:
            value, state = 0.35, "contradict"
        else:
            value, state = 0.35, "contradict"
        scores.append(value * horizon_weight)
        states.append(state)
    value = sum(scores)
    status = "contradict" if "contradict" in states and value < 0.5 else "support" if value > 0.55 else "neutral"
    return {"value": round(value, 6), "status": status, "reason": "OI confirmation evaluated",
            "observations": len(valid), "states": states}


def score_funding_crowding(direction: str, funding_history: Sequence[Mapping[str, Any]], reaction: Mapping[str, Any], oi: Mapping[str, Any], *, evaluation_cutoff: int | None = None) -> dict[str, Any]:
    rows = _cutoff_funding(funding_history, evaluation_cutoff)
    if len(rows) < 32:
        return {"value": 0.5, "status": "unavailable", "reason": "funding history is unavailable", "observations": len(rows)}
    current = float(rows[-1]["rate"])
    if current == 0.0:
        return {"value": 0.5, "status": "neutral", "reason": "funding is neutral", "heat": 0.0}
    magnitudes = [abs(float(row["rate"])) for row in rows[:-1]]
    percentile = sum(value <= abs(current) for value in magnitudes) / max(1, len(magnitudes))
    heat = max(0.0, min(1.0, (percentile - 0.75) / 0.25))
    if heat <= 0:
        return {"value": 0.5, "status": "neutral", "reason": "funding is not extreme", "percentile": percentile, "heat": heat}
    sign = 1.0 if str(direction).lower() == "long" else -1.0 if str(direction).lower() == "short" else 0.0
    signed = sign * current
    if sign == 0 or signed > 0:
        value, status, cap = 0.5 - 0.5 * heat, "contradict", "same_side_penalty"
    elif reaction.get("status") != "support":
        value, status, cap = 0.5, "neutral", "reaction_not_confirmed"
    elif oi.get("status") == "support":
        value, status, cap = 0.5 + 0.5 * heat, "support", "full_boost"
    elif oi.get("status") in {"neutral", "unavailable"}:
        value, status, cap = 0.5 + 0.25 * heat, "support", "capped_boost"
    else:
        value, status, cap = 0.5, "neutral", "oi_contradiction_block"
    return {"value": round(value, 6), "status": status, "reason": "funding crowding evaluated",
            "current_rate": current, "percentile": percentile, "heat": heat, "cap": cap,
            "observations": len(rows)}


def _participation(bars: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    volumes = [float(row["volume"]) for row in bars if _finite(row.get("volume")) and float(row["volume"]) >= 0]
    if len(volumes) < 33:
        return {"value": 0.5, "status": "unavailable", "reason": "volume history is unavailable"}
    reference = median(volumes[-97:-1])
    if reference <= 0:
        return {"value": 0.5, "status": "unavailable", "reason": "volume baseline is invalid"}
    ratio = volumes[-1] / reference
    value = max(0.0, min(1.0, (ratio - 0.5) / 1.5))
    return {"value": round(value, 6), "status": "support" if value > 0.55 else "contradict" if value < 0.45 else "neutral", "reason": "volume participation evaluated", "rvol": ratio}


def _weighted(observations: Mapping[str, Mapping[str, Any]]) -> float:
    return round(sum(WEIGHTS[key] * max(0.0, min(1.0, float(observations[key].get("value", 0.5)))) for key in WEIGHTS), 6)


def score_candidate_v3(
    candidate: Mapping[str, Any], market_bars: Sequence[Mapping[str, Any]],
    oi_observations: Sequence[Mapping[str, Any]] | None = None,
    funding_history: Sequence[Mapping[str, Any]] | None = None,
    *, mode: str = "shadow", evaluation_cutoff: int | None = None,
) -> dict[str, Any]:
    market_rows = _rows(market_bars)
    cutoff = int(evaluation_cutoff if evaluation_cutoff is not None else (_bar_end(market_rows[-1]) if market_rows else 0))
    bars15 = list(market_rows)
    if not _looks_like_15m(bars15):
        bars15 = resample_5m_to_15m(market_rows)
    profile_bars = bars15[:-1] if len(bars15) > 1 else bars15
    profile = build_value_profile(str(candidate.get("asset") or candidate.get("symbol") or ""), cutoff, profile_bars)
    reaction = score_value_reaction(candidate, profile, bars15)
    oi = score_oi_confirmation(candidate, oi_observations or [], market_rows)
    funding = score_funding_crowding(str(candidate.get("direction") or ""), funding_history or [], reaction, oi, evaluation_cutoff=cutoff)
    observations = {
        "value-reaction-v1": reaction,
        "participation-v1": _participation(market_rows),
        "oi-participation-v1": oi,
        "crowding-v1": funding,
    }
    score = _weighted(observations)
    return {
        "scorer_version": SCORER_VERSION, "reaction_score": score,
        "operational_score": score if str(mode).lower() == "enforce" else score,
        "verdict": "pass" if score >= 0.50 else "reject", "weights": dict(WEIGHTS),
        "observations": observations, "profile": profile,
    }


def select_operational_result(v2_result: Mapping[str, Any], v3_result: Mapping[str, Any], mode: str) -> dict[str, Any]:
    """Select one result; shadow still computes/persists v3 elsewhere."""
    if str(mode).lower() == "enforce":
        return {"quality_score": float(v3_result.get("reaction_score", 0.0)), "verdict": v3_result.get("verdict"), "operational_version": SCORER_VERSION}
    return {"quality_score": float(v2_result.get("quality_score", v2_result.get("score", 0.0))), "verdict": v2_result.get("verdict", v2_result.get("score_decision")), "operational_version": "trade-quality-v2"}
