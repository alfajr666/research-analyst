"""Upstream approved-universe performance rotation and durable feed contract."""
from __future__ import annotations

import json
import hashlib
import math
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping

import httpx

import config


ALGORITHM_VERSION = "performance-24h-v1"
SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
PERMANENT_ASSETS = ("BTC", "ETH", "PAXG", "QQQ")
PERMANENT_SYMBOLS = ("BTC", "ETH", "PAXG", "QQQUSDT")


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def rotation_boundary(value: datetime, refresh_hours: int | None = None) -> datetime:
    """Return the fixed UTC boundary containing ``value``."""
    hours = int(refresh_hours or getattr(config, "SYMBOL_ROTATION_REFRESH_HOURS", 4))
    seconds = hours * 60 * 60
    epoch = int(_utc(value).timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=timezone.utc)


def approved_assets() -> list[str]:
    """Load the only pool allowed to participate in performance ranking."""
    seen: set[str] = set()
    result = []
    for asset in config.load_static_symbols():
        canonical = str(asset).strip().upper()
        if canonical and canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return sorted(result)


def _target_sides() -> int:
    count = int(getattr(config, "SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT", 30))
    return max(1, count // 2)


def _source_priority(source: str) -> int:
    if getattr(config, "COINANALYZE_EVAL_ENABLED", False) and source == "coinalyze":
        return 0
    if source.endswith("_ws"):
        return 1
    if source == getattr(config, "FAILOVER_SOURCE_NAME", "venue_agg_v1"):
        return 2
    return 3


def _finite_positive(value: object) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _canonical_asset(value: object) -> str:
    return str(value or "").strip().upper().removesuffix("USDT")


def _watchlist_ttl() -> timedelta:
    return timedelta(hours=float(getattr(config, "SYMBOL_ROTATION_WATCHLIST_TTL_HOURS", 72)))


def _watchlist_cap() -> int:
    return int(getattr(config, "SYMBOL_ROTATION_WATCHLIST_MAX_SYMBOLS", 80))


def _cap_assets(assets: Iterable[str]) -> list[str]:
    """Return a deterministic capped base-symbol universe."""
    available = {_canonical_asset(asset) for asset in assets if _canonical_asset(asset)}
    available.update(PERMANENT_ASSETS)
    slots = max(0, _watchlist_cap() - len(PERMANENT_SYMBOLS))
    return [*PERMANENT_ASSETS, *sorted(available - set(PERMANENT_ASSETS))[:slots]]


def _effective_universe_version(symbols: Iterable[str], entries: Iterable[Mapping[str, object]]) -> str:
    payload = {
        "symbols": sorted({_canonical_asset(symbol) for symbol in symbols}),
        "entries": sorted(
            (
                {
                    "asset": _canonical_asset(entry.get("asset")),
                }
                for entry in entries
            ),
            key=lambda item: item["asset"],
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"universe-{hashlib.sha256(encoded).hexdigest()[:16]}"


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_entry_time(entry: Mapping[str, object], field: str) -> datetime | None:
    value = entry.get(field)
    if value in (None, ""):
        return None
    try:
        return _utc(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _rank_records(records: Iterable[Mapping[str, object]], pool: Iterable[str], boundary: datetime,
                  lookback_hours: int, *, require_fresh: bool = False) -> tuple[dict[str, float], str | None, list[str]]:
    """Validate and rank point-in-time performance records.

    Records may contain ``return`` or positive ``reference_price`` and
    ``current_price``. Invalid, duplicated, unknown, and future observations are
    excluded and reported rather than being turned into fabricated rankings.
    """
    boundary = _utc(boundary)
    pool_set = {str(asset).upper() for asset in pool}
    performance: dict[str, float] = {}
    source_as_of: list[datetime] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for raw in records:
        asset = str(raw.get("asset", "")).strip().upper()
        if asset not in pool_set:
            rejected.append(f"unknown asset: {asset or '<empty>'}")
            continue
        if asset in PERMANENT_ASSETS:
            continue
        if asset in seen:
            rejected.append(f"duplicate asset: {asset}")
            continue
        seen.add(asset)
        source = str(raw.get("source", "")).strip()
        interval = str(raw.get("interval", "")).strip().lower()
        if not source or not interval:
            rejected.append(f"source contract incomplete: {asset}")
            continue
        try:
            _utc(raw.get("retrieved_at"))
        except (TypeError, ValueError):
            rejected.append(f"invalid retrieval timestamp: {asset}")
            continue
        try:
            as_of = _utc(raw.get("as_of") or raw.get("end") or raw.get("source_as_of"))
        except (TypeError, ValueError):
            rejected.append(f"invalid timestamp: {asset}")
            continue
        if as_of > boundary:
            rejected.append(f"future data: {asset}")
            continue
        try:
            start_value = raw.get("start") or raw.get("start_at") or raw.get("reference_at")
            if start_value is not None and as_of - _utc(start_value) < timedelta(hours=lookback_hours):
                rejected.append(f"incomplete coverage: {asset}")
                continue
            if start_value is None and interval not in {"24h", "24hr", "24-hour"}:
                rejected.append(f"incomplete coverage: {asset}")
                continue
        except (TypeError, ValueError):
            rejected.append(f"invalid start timestamp: {asset}")
            continue
        try:
            if "return" in raw:
                result = float(raw["return"])
                if not math.isfinite(result):
                    raise ValueError
            else:
                reference = raw.get("reference_price")
                current = raw.get("current_price")
                if not (_finite_positive(reference) and _finite_positive(current)):
                    raise ValueError
                result = float(current) / float(reference) - 1.0
            if not math.isfinite(result):
                raise ValueError
        except (TypeError, ValueError):
            rejected.append(f"invalid performance: {asset}")
            continue
        source_as_of.append(as_of)
        performance[asset] = result

    latest = max(source_as_of) if source_as_of else None
    if require_fresh and latest is not None:
        max_age = timedelta(hours=float(getattr(config, "SYMBOL_ROTATION_SOURCE_MAX_AGE_HOURS", 6)))
        if boundary - latest > max_age:
            return {}, latest.isoformat(), rejected + ["performance snapshot is stale"]
    return performance, latest.isoformat() if latest else None, rejected


def rank_performance(records: Iterable[Mapping[str, object]], boundary: datetime,
                     pool: Iterable[str] | None = None, *, require_fresh: bool = False,
                     source_cutoff: datetime | None = None) -> dict:
    """Return deterministic equal-sided selections and validation metadata."""
    boundary = _utc(source_cutoff) if source_cutoff is not None else rotation_boundary(boundary)
    assets = approved_assets() if pool is None else sorted({str(x).upper().removesuffix("USDT") for x in pool})
    permanent = [asset for asset in PERMANENT_ASSETS if asset in assets or pool is None]
    ranking_pool = [asset for asset in assets if asset not in PERMANENT_ASSETS]
    lookback = int(getattr(config, "SYMBOL_ROTATION_LOOKBACK_HOURS", 24))
    performance, source_as_of, rejected = _rank_records(
        records, ranking_pool, boundary, lookback, require_fresh=require_fresh
    )
    losers_ordered = sorted(performance.items(), key=lambda item: (item[1], item[0]))
    gainers_ordered = sorted(performance.items(), key=lambda item: (-item[1], item[0]))
    side_count = _target_sides()
    losers = [
        {"asset": asset, "return": value, "rank_side": "loser", "rank": rank}
        for rank, (asset, value) in enumerate(losers_ordered[:side_count], start=1)
    ]
    gainers = [
        {"asset": asset, "return": value, "rank_side": "gainer", "rank": rank}
        for rank, (asset, value) in enumerate(gainers_ordered[:side_count], start=1)
    ]
    symbols = list(permanent)
    for item in gainers + losers:
        if item["asset"] not in symbols:
            symbols.append(item["asset"])
    return {
        "gainers": gainers,
        "losers": losers,
        "symbols": symbols,
        "source_as_of": source_as_of,
        "qualified_count": len(performance),
        "rejected": rejected,
        "rotating_symbol_count": side_count * 2,
        "target_count": side_count * 2,
    }


def _feed_valid(feed: object, at: datetime | None = None) -> bool:
    if not isinstance(feed, dict) or not validate_feed(feed, raise_error=False):
        return False
    try:
        valid_from = _utc(feed["valid_from"])
        valid_until = _utc(feed["valid_until"])
        when = _utc(at or datetime.now(timezone.utc))
    except (KeyError, TypeError, ValueError):
        return False
    return valid_from <= when < valid_until and isinstance(feed.get("symbols"), list) and bool(feed["symbols"])


def _validate_feed_shape(feed: dict, *, sticky: bool) -> list[str]:
    errors: list[str] = []
    required = {"schema_version", "feed_id", "algorithm_version", "generated_at",
                "valid_from", "valid_until", "permanent_symbols", "rotating_symbol_count",
                "symbol_count", "gainers", "losers", "symbols", "status"}
    errors.extend(f"missing field: {key}" for key in sorted(required - feed.keys()))
    symbols = feed.get("symbols")
    try:
        symbols_are_unique = isinstance(symbols, list) and len(set(symbols)) == len(symbols)
    except TypeError:
        symbols_are_unique = False
    if not isinstance(symbols, list) or not symbols or not symbols_are_unique or not all(
        isinstance(symbol, str) and symbol.strip() for symbol in symbols
    ):
        errors.append("symbols must be a non-empty unique list")
    elif feed.get("symbol_count") != len(symbols):
        errors.append("symbol_count does not match symbols")
    permanent = feed.get("permanent_symbols")
    if permanent != list(PERMANENT_SYMBOLS):
        errors.append("permanent_symbols does not match the fixed permanent set")
    elif not isinstance(symbols, list) or not all(symbol in symbols for symbol in permanent):
        errors.append("symbols must include every permanent asset")
    if (not isinstance(feed.get("rotating_symbol_count"), int)
            or isinstance(feed.get("rotating_symbol_count"), bool)
            or feed.get("rotating_symbol_count") <= 0
            or feed.get("rotating_symbol_count") % 2):
        errors.append("rotating_symbol_count must be a positive even integer")
    try:
        if _utc(feed["valid_until"]) <= _utc(feed["valid_from"]):
            errors.append("validity interval is invalid")
    except (KeyError, TypeError, ValueError):
        errors.append("validity timestamps are invalid")
    for side in ("gainers", "losers"):
        if not isinstance(feed.get(side), list):
            errors.append(f"{side} must be a list")

    if not sticky:
        return errors

    ttl = feed.get("watchlist_ttl_hours")
    if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0:
        errors.append("watchlist_ttl_hours must be positive")
    cap = feed.get("watchlist_max_symbols")
    if (not isinstance(cap, int) or isinstance(cap, bool) or cap < len(PERMANENT_SYMBOLS)):
        errors.append("watchlist_max_symbols must include permanent symbols")
    if isinstance(cap, int) and len(symbols) > cap:
        errors.append("symbol_count exceeds watchlist_max_symbols")
    entries = feed.get("watchlist_entries")
    if not isinstance(entries, list):
        errors.append("watchlist_entries must be a list")
        entries = []
    entry_assets: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("watchlist entry must be an object")
            continue
        asset = _canonical_asset(entry.get("asset"))
        if not asset or asset in PERMANENT_ASSETS:
            errors.append("watchlist entry must be a non-permanent asset")
        entry_assets.append(asset)
        for field in (
            "first_selected_at", "last_selected_at", "expires_at", "last_feed_id", "last_rank_side"
        ):
            if not entry.get(field):
                errors.append(f"watchlist entry missing {field}")
        for field in ("first_selected_at", "last_selected_at", "expires_at"):
            if entry.get(field):
                try:
                    _utc(entry[field])  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    errors.append(f"watchlist entry has invalid {field}")
        rank = entry.get("last_rank")
        if not isinstance(rank, int) or isinstance(rank, bool) or rank <= 0:
            errors.append("watchlist entry last_rank must be a positive integer")
        if entry.get("last_rank_side") not in {"gainer", "loser"}:
            errors.append("watchlist entry last_rank_side is invalid")
        first = _parse_entry_time(entry, "first_selected_at")
        last = _parse_entry_time(entry, "last_selected_at")
        expires = _parse_entry_time(entry, "expires_at")
        if first is not None and last is not None and first > last:
            errors.append("watchlist entry selection timestamps are out of order")
        if last is not None and expires is not None and last >= expires:
            errors.append("watchlist entry expires_at must follow last_selected_at")
    if len(set(entry_assets)) != len(entry_assets):
        errors.append("watchlist entries must be unique")
    symbol_bases = {_canonical_asset(symbol) for symbol in symbols}
    if any(asset not in symbol_bases for asset in entry_assets):
        errors.append("watchlist entries must be present in symbols")
    if symbol_bases - set(PERMANENT_ASSETS) - set(entry_assets):
        errors.append("every non-permanent symbol must have a watchlist entry")
    if feed.get("effective_symbol_count") != len(symbols):
        errors.append("effective_symbol_count does not match symbols")
    if feed.get("watchlist_entry_count") != len(entries):
        errors.append("watchlist_entry_count does not match watchlist_entries")
    if not isinstance(feed.get("watchlist_history", []), list):
        errors.append("watchlist_history must be a list")
    if not isinstance(feed.get("effective_universe_version"), str) or not feed.get("effective_universe_version"):
        errors.append("effective_universe_version must be non-empty")
    if not isinstance(feed.get("freshness_state"), str) or not feed.get("freshness_state"):
        errors.append("freshness_state must be non-empty")
    return errors


def validate_feed(feed: object, *, raise_error: bool = True) -> bool:
    """Validate the immutable JSON contract at the serialization boundary."""
    errors: list[str] = []
    if not isinstance(feed, dict):
        errors.append("feed must be an object")
    elif feed.get("schema_version") == LEGACY_SCHEMA_VERSION:
        errors.extend(_validate_feed_shape(feed, sticky=False))
    elif feed.get("schema_version") == SCHEMA_VERSION:
        errors.extend(_validate_feed_shape(feed, sticky=True))
    else:
        errors.append("unsupported schema_version")
    if errors and raise_error:
        raise ValueError("invalid symbol rotation feed: " + "; ".join(errors))
    return not errors


def read_feed(path: str | Path | None = None, at: datetime | None = None,
              *, allow_expired: bool = False) -> dict | None:
    target = Path(path or getattr(config, "SYMBOL_ROTATION_FEED_PATH", ""))
    if not target or not target.exists():
        return None
    try:
        feed = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(feed, dict) or feed.get("schema_version") not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}:
        return None
    if allow_expired or _feed_valid(feed, at):
        return feed
    return None


def write_feed(feed: dict, path: str | Path | None = None) -> Path:
    """Atomically publish one complete immutable feed snapshot."""
    validate_feed(feed)
    target = Path(path or getattr(config, "SYMBOL_ROTATION_FEED_PATH", ""))
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(feed, sort_keys=True, separators=(",", ":")) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".symbol-rotation-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return target


def _previous_watchlist_entries(previous_feed: Mapping[str, object] | None) -> dict[str, dict]:
    if not isinstance(previous_feed, Mapping) or previous_feed.get("schema_version") != SCHEMA_VERSION:
        return {}
    entries = previous_feed.get("watchlist_entries")
    if not isinstance(entries, list):
        return {}
    result = {}
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        asset = _canonical_asset(raw.get("asset"))
        expires = _parse_entry_time(raw, "expires_at")
        rank = raw.get("last_rank")
        if (asset and asset not in PERMANENT_ASSETS and expires is not None
                and isinstance(rank, int) and not isinstance(rank, bool) and rank > 0):
            result[asset] = dict(raw)
    return result


def _watchlist_has_active_entries(feed: Mapping[str, object] | None, at: datetime) -> bool:
    cutoff = _utc(at)
    return any(
        expires is not None and expires > cutoff
        for entry in _previous_watchlist_entries(feed).values()
        for expires in [_parse_entry_time(entry, "expires_at")]
    )


def _apply_watchlist(
    selected_items: list[dict],
    previous_feed: Mapping[str, object] | None,
    boundary: datetime,
    feed_id: str,
) -> tuple[list[dict], list[str], list[dict], list[dict]]:
    """Merge current rotation selections into the durable capped watchlist."""
    boundary = _utc(boundary)
    previous = _previous_watchlist_entries(previous_feed)
    current: dict[str, dict] = {}
    events = list(previous_feed.get("watchlist_events", [])) if isinstance(previous_feed, Mapping) else []
    history = list(previous_feed.get("watchlist_history", [])) if isinstance(previous_feed, Mapping) else []
    if not isinstance(events, list):
        events = []
    if not isinstance(history, list):
        history = []
    events = [event for event in events if isinstance(event, dict)][-500:]
    history = [entry for entry in history if isinstance(entry, dict)]

    for item in selected_items:
        asset = _canonical_asset(item.get("asset"))
        if not asset or asset in PERMANENT_ASSETS:
            continue
        if asset in current:
            continue
        old = previous.get(asset, {})
        is_reselection = bool(old)
        entry = {
            "asset": asset,
            "permanent": False,
            "first_selected_at": old.get("first_selected_at", _iso(boundary)),
            "last_selected_at": _iso(boundary),
            "expires_at": _iso(boundary + _watchlist_ttl()),
            "last_feed_id": feed_id,
            "last_rank_side": item.get("rank_side"),
            "last_rank": item.get("rank"),
        }
        current[asset] = {**entry, "_current": True}
        events.append({
            "event": "reselected" if is_reselection else "added",
            "asset": asset,
            "at": _iso(boundary),
            "feed_id": feed_id,
        })

    for asset, old in previous.items():
        if asset in current:
            continue
        expires = _parse_entry_time(old, "expires_at")
        if expires is not None and expires > boundary:
            current[asset] = {**old, "_current": False}
        else:
            events.append({"event": "expired", "asset": asset, "at": _iso(boundary), "feed_id": feed_id})
            history.append({**old, "state": "expired", "state_at": _iso(boundary)})

    slots = max(0, _watchlist_cap() - len(PERMANENT_SYMBOLS))
    selected = sorted(
        (entry for entry in current.values() if entry.get("_current")),
        key=lambda entry: (
            str(entry.get("last_rank_side") or ""),
            int(entry.get("last_rank") or 0),
            str(entry.get("asset")),
        ),
    )
    older = sorted(
        (entry for entry in current.values() if not entry.get("_current")),
        key=lambda entry: (
            _parse_entry_time(entry, "expires_at") or datetime.max.replace(tzinfo=timezone.utc),
            _parse_entry_time(entry, "last_selected_at") or datetime.max.replace(tzinfo=timezone.utc),
            int(entry.get("last_rank") or 0),
            str(entry.get("asset")),
        ),
    )
    kept = selected[:slots]
    kept.extend(older[:max(0, slots - len(kept))])
    kept_assets = {entry["asset"] for entry in kept}
    for entry in current.values():
        if entry["asset"] not in kept_assets:
            events.append({
                "event": "evicted",
                "asset": entry["asset"],
                "at": _iso(boundary),
                "feed_id": feed_id,
                "reason": "watchlist_cap",
            })
            history.append({
                key: value for key, value in {
                    **entry,
                    "state": "evicted",
                    "state_at": _iso(boundary),
                    "eviction_reason": "watchlist_cap",
                }.items() if key != "_current"
            })
    clean_entries = [
        {key: value for key, value in entry.items() if key != "_current"}
        for entry in kept
    ]
    return clean_entries, sorted(kept_assets), events[-500:], history


def effective_state_at(feed: Mapping[str, object] | None, at: datetime | None = None) -> tuple[list[str], dict]:
    """Return the cutoff-bound effective universe and its provenance."""
    cutoff = _utc(at or datetime.now(timezone.utc))
    if not isinstance(feed, Mapping) or not validate_feed(feed, raise_error=False):
        fallback = _fallback_feed(rotation_boundary(cutoff), cutoff, "missing or invalid feed")
        metadata = dict(fallback)
        metadata["status"] = "invalid"
        metadata["freshness_state"] = "invalid"
        metadata["effective_universe_version"] = _effective_universe_version(PERMANENT_SYMBOLS, [])
        return list(PERMANENT_ASSETS), metadata

    if feed.get("schema_version") == LEGACY_SCHEMA_VERSION:
        if _feed_valid(feed, cutoff):
            symbols = sorted({_canonical_asset(symbol) for symbol in feed.get("symbols", [])})
            metadata = dict(feed)
            metadata["effective_universe_version"] = _effective_universe_version(symbols, [])
            metadata["freshness_state"] = feed.get("status", "ready")
            return symbols, metadata
        fallback = _fallback_feed(rotation_boundary(cutoff), cutoff, "legacy feed expired")
        metadata = dict(fallback)
        metadata["status"] = "permanent_fallback"
        metadata["freshness_state"] = "permanent_fallback"
        return list(PERMANENT_ASSETS), metadata

    entries = []
    for entry in feed.get("watchlist_entries", []):
        if not isinstance(entry, dict):
            continue
        expires = _parse_entry_time(entry, "expires_at")
        if expires is not None and expires > cutoff:
            entries.append(entry)
    bases = sorted({_canonical_asset(entry.get("asset")) for entry in entries})
    symbols = list(PERMANENT_ASSETS) + [asset for asset in bases if asset not in PERMANENT_ASSETS]
    metadata = dict(feed)
    metadata["symbols"] = list(PERMANENT_SYMBOLS) + bases
    metadata["symbol_count"] = len(symbols)
    metadata["effective_symbol_count"] = len(symbols)
    metadata["watchlist_entry_count"] = len(entries)
    metadata["effective_universe_version"] = _effective_universe_version(symbols, entries)
    last_valid_at = _parse_entry_time(
        {"value": feed.get("last_valid_feed_at") or feed.get("generated_at")}, "value"
    )
    metadata["feed_age_seconds"] = (
        max(0.0, (cutoff - last_valid_at).total_seconds()) if last_valid_at is not None else None
    )
    if feed.get("status") == "fallback" or not entries:
        state = "permanent_fallback"
    elif _feed_valid(feed, cutoff):
        state = "ready"
    elif entries:
        state = "degraded"
    else:
        state = "permanent_fallback"
    metadata["status"] = state
    metadata["freshness_state"] = state
    return [*PERMANENT_ASSETS, *bases], metadata


def _fallback_feed(
    boundary: datetime,
    generated_at: datetime,
    reason: str,
    previous_feed: Mapping[str, object] | None = None,
) -> dict:
    symbols = list(PERMANENT_SYMBOLS)
    feed = {
        "schema_version": SCHEMA_VERSION,
        "feed_id": f"performance-{boundary.strftime('%Y-%m-%dT%H:%M:%SZ')}-fallback",
        "algorithm_version": ALGORITHM_VERSION,
        "generated_at": _utc(generated_at).isoformat().replace("+00:00", "Z"),
        "valid_from": boundary.isoformat().replace("+00:00", "Z"),
        "valid_until": (boundary + timedelta(hours=int(getattr(config, "SYMBOL_ROTATION_REFRESH_HOURS", 4)))).isoformat().replace("+00:00", "Z"),
        "source_as_of": None,
        "permanent_symbols": list(PERMANENT_SYMBOLS),
        "rotating_symbol_count": int(getattr(config, "SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT", 30)),
        "symbol_count": len(symbols),
        "approved_count": 0,
        "qualified_count": 0,
        "target_count": int(getattr(config, "SYMBOL_ROTATION_ROTATING_SYMBOL_COUNT", 30)),
        "gainers": [],
        "losers": [],
        "symbols": symbols,
        "status": "fallback",
        "fallback_reason": reason,
        "rejection_count": 0,
        "rejection_reasons": [reason],
        "watchlist_ttl_hours": float(getattr(config, "SYMBOL_ROTATION_WATCHLIST_TTL_HOURS", 72)),
        "watchlist_max_symbols": _watchlist_cap(),
        "watchlist_entries": [],
        "watchlist_events": [],
        "watchlist_history": list(previous_feed.get("watchlist_history", []))
        if isinstance(previous_feed, Mapping) and isinstance(previous_feed.get("watchlist_history"), list)
        else [],
        "effective_symbol_count": len(symbols),
        "watchlist_entry_count": 0,
        "effective_universe_version": _effective_universe_version(symbols, []),
        "last_valid_feed_id": (
            previous_feed.get("last_valid_feed_id") or previous_feed.get("feed_id")
            if isinstance(previous_feed, Mapping) else None
        ),
        "last_valid_feed_at": (
            previous_feed.get("last_valid_feed_at") or previous_feed.get("generated_at")
            if isinstance(previous_feed, Mapping) else None
        ),
        "freshness_state": "permanent_fallback",
    }
    return feed


def _migrate_legacy_feed(legacy: Mapping[str, object], boundary: datetime,
                         generated_at: datetime) -> dict:
    """Upgrade a valid legacy snapshot without discarding its selections."""
    selected_items: list[dict] = []
    seen: set[str] = set()
    for side, field in (("gainer", "gainers"), ("loser", "losers")):
        raw_items = legacy.get(field, [])
        if not isinstance(raw_items, list):
            raw_items = []
        for position, raw_item in enumerate(raw_items, start=1):
            if not isinstance(raw_item, Mapping):
                continue
            asset = _canonical_asset(raw_item.get("asset"))
            if not asset or asset in PERMANENT_ASSETS or asset in seen:
                continue
            seen.add(asset)
            rank = raw_item.get("rank", position)
            if isinstance(rank, bool):
                rank = position
            else:
                try:
                    rank = int(rank)
                except (TypeError, ValueError):
                    rank = position
            if rank <= 0:
                rank = position
            selected_items.append({
                "asset": asset,
                "rank_side": side,
                "rank": rank,
                "return": raw_item.get("return"),
            })

    # Older feeds did not always persist ranked side arrays. Preserve their
    # remaining symbols as deterministic legacy selections during migration.
    raw_symbols = legacy.get("symbols", [])
    if isinstance(raw_symbols, list):
        for position, raw_symbol in enumerate(raw_symbols, start=1):
            asset = _canonical_asset(raw_symbol)
            if not asset or asset in PERMANENT_ASSETS or asset in seen:
                continue
            seen.add(asset)
            selected_items.append({"asset": asset, "rank_side": "gainer", "rank": position})

    feed_id = f"{legacy.get('feed_id') or 'legacy-feed'}-sticky"
    entries, kept_assets, events, history = _apply_watchlist(
        selected_items, None, boundary, feed_id
    )
    symbols = list(PERMANENT_SYMBOLS) + kept_assets
    feed = dict(legacy)
    feed.update({
        "schema_version": SCHEMA_VERSION,
        "feed_id": feed_id,
        "algorithm_version": ALGORITHM_VERSION,
        "generated_at": _iso(generated_at),
        "valid_from": _iso(boundary),
        "valid_until": _iso(boundary + timedelta(hours=int(getattr(config, "SYMBOL_ROTATION_REFRESH_HOURS", 4)))),
        "permanent_symbols": list(PERMANENT_SYMBOLS),
        "symbols": symbols,
        "symbol_count": len(symbols),
        "status": "ready",
        "fallback_reason": None,
        "watchlist_ttl_hours": float(getattr(config, "SYMBOL_ROTATION_WATCHLIST_TTL_HOURS", 72)),
        "watchlist_max_symbols": _watchlist_cap(),
        "watchlist_entries": entries,
        "watchlist_events": events,
        "watchlist_history": history,
        "effective_symbol_count": len(symbols),
        "watchlist_entry_count": len(entries),
        "effective_universe_version": _effective_universe_version(symbols, entries),
        "last_valid_feed_id": legacy.get("feed_id"),
        "last_valid_feed_at": legacy.get("generated_at"),
        "freshness_state": "ready",
    })
    return feed


def build_feed(records: Iterable[Mapping[str, object]], boundary: datetime,
               *, generated_at: datetime | None = None, previous_feed: dict | None = None,
               source_cutoff: datetime | None = None) -> dict:
    """Build a feed from a validated complete performance snapshot."""
    boundary = rotation_boundary(boundary)
    generated_at = _utc(generated_at or datetime.now(timezone.utc))
    records = list(records)
    pool = {
        str(record.get("asset", "")).upper().removesuffix("USDT")
        for record in records
        if str(record.get("asset", "")).strip()
    }
    ranking = rank_performance(
        records, boundary, pool=pool, require_fresh=True, source_cutoff=source_cutoff
    )
    if not ranking["gainers"] and not ranking["losers"]:
        if previous_feed and (
            _feed_valid(previous_feed, boundary)
            or _watchlist_has_active_entries(previous_feed, boundary)
        ):
            return previous_feed
        reason = "; ".join(ranking["rejected"][:3]) or "no valid performance snapshot"
        return _fallback_feed(boundary, generated_at, reason, previous_feed)
    target = ranking["target_count"]
    rotating_assets = {item["asset"] for item in ranking["gainers"] + ranking["losers"]}
    feed_id = f"performance-{boundary.strftime('%Y-%m-%dT%H:%M:%SZ')}-v{int(generated_at.timestamp())}"
    selected_items = ranking["gainers"] + ranking["losers"]
    entries, kept_assets, events, history = _apply_watchlist(
        selected_items, previous_feed, boundary, feed_id
    )
    feed_symbols = list(PERMANENT_SYMBOLS) + kept_assets
    feed = {
        "schema_version": SCHEMA_VERSION,
        "feed_id": feed_id,
        "algorithm_version": ALGORITHM_VERSION,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "valid_from": boundary.isoformat().replace("+00:00", "Z"),
        "valid_until": (boundary + timedelta(hours=int(getattr(config, "SYMBOL_ROTATION_REFRESH_HOURS", 4)))).isoformat().replace("+00:00", "Z"),
        "source_as_of": ranking["source_as_of"],
        "permanent_symbols": list(PERMANENT_SYMBOLS),
        "rotating_symbol_count": ranking["rotating_symbol_count"],
        "symbol_count": len(feed_symbols),
        "approved_count": len(pool),
        "qualified_count": ranking["qualified_count"],
        "target_count": target,
        "gainers": ranking["gainers"],
        "losers": ranking["losers"],
        "symbols": feed_symbols,
        "status": "ready",
        "fallback_reason": None if len(rotating_assets) >= target else f"qualified shortfall: {len(rotating_assets)}/{target}",
        "rejection_count": len(ranking["rejected"]),
        "rejection_reasons": ranking["rejected"][:20],
        "watchlist_ttl_hours": float(getattr(config, "SYMBOL_ROTATION_WATCHLIST_TTL_HOURS", 72)),
        "watchlist_max_symbols": _watchlist_cap(),
        "watchlist_entries": entries,
        "watchlist_events": events,
        "watchlist_history": history,
        "effective_symbol_count": len(feed_symbols),
        "watchlist_entry_count": len(entries),
        "effective_universe_version": _effective_universe_version(feed_symbols, entries),
        "last_valid_feed_id": feed_id,
        "last_valid_feed_at": _iso(generated_at),
        "freshness_state": "ready",
    }
    return feed


def _db_performance_records(conn, boundary: datetime, pool: Iterable[str] | None = None) -> list[dict]:
    """Adapt retained completed bars into a point-in-time performance snapshot."""
    boundary = rotation_boundary(boundary)
    start = boundary - timedelta(hours=int(getattr(config, "SYMBOL_ROTATION_LOOKBACK_HOURS", 24)))
    pool = sorted({str(asset).upper() for asset in (pool if pool is not None else approved_assets())})
    if not pool:
        return []
    placeholders = ",".join("?" for _ in pool)
    try:
        rows = conn.execute(
            f"""SELECT asset, source_end, source,
                      CAST(json_extract(payload_json, '$.close') AS REAL), retrieved_at
                 FROM source_observations
                WHERE interval = ? AND asset IN ({placeholders})
                  AND source_end >= ? AND source_end <= ?
                  AND CAST(json_extract(payload_json, '$.close') AS REAL) > 0
                ORDER BY asset, source_end, source""",
            [getattr(config, "SYMBOL_ROTATION_BAR_INTERVAL", "5m"), *pool, start, boundary],
        ).fetchall()
    except Exception:
        return []
    by_asset: dict[str, dict[datetime, tuple[int, float]]] = {}
    for asset, source_end, source, close, retrieved_at in rows:
        try:
            timestamp = _utc(source_end)
            value = float(close)
            if not _finite_positive(value):
                continue
        except (TypeError, ValueError):
            continue
        prices = by_asset.setdefault(str(asset).upper(), {})
        priority = _source_priority(str(source))
        if timestamp not in prices or priority < prices[timestamp][0]:
            prices[timestamp] = (priority, value, str(source), retrieved_at)
    records = []
    for asset, prices in by_asset.items():
        if len(prices) < 2:
            continue
        first_at, (_, first, _, _) = min(prices.items())
        last_at, (_, last, source, retrieved_at) = max(prices.items())
        if last_at - first_at >= timedelta(hours=int(getattr(config, "SYMBOL_ROTATION_LOOKBACK_HOURS", 24))):
            records.append({
                "asset": asset, "as_of": last_at, "start": first_at,
                "reference_price": first, "current_price": last,
                "source": source, "interval": getattr(config, "SYMBOL_ROTATION_BAR_INTERVAL", "5m"),
                "retrieved_at": retrieved_at,
            })
    return records


def fetch_bybit_ticker_snapshot(boundary: datetime, *, client: object | None = None) -> list[dict]:
    """Fetch the lightweight all-symbol 24h source used by the rotation worker."""
    boundary = rotation_boundary(boundary)
    own_client = client is None
    http_client = client or httpx.Client(timeout=20)
    try:
        response = http_client.get(
            f"{getattr(config, 'BYBIT_LINEAR_BASE_URL', 'https://api.bybit.com')}/v5/market/tickers",
            params={"category": "linear"},
        )
        response.raise_for_status()
        payload = response.json()
        captured_at = datetime.now(timezone.utc)
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        response_time = payload.get("time") if isinstance(payload, dict) else None
        server_as_of = (
            _utc(datetime.fromtimestamp(float(response_time) / 1000, tz=timezone.utc))
            if response_time else captured_at
        )
        # A small server/local clock skew must not make a completed response
        # unusable as a live snapshot.
        as_of = min(server_as_of, captured_at)
        records = []
        for ticker in result.get("list", []) if isinstance(result, dict) else []:
            symbol = str(ticker.get("symbol", "")).upper()
            if not symbol.endswith("USDT"):
                continue
            asset = symbol.removesuffix("USDT")
            current = ticker.get("lastPrice")
            reference = ticker.get("prevPrice24h")
            record = {"asset": asset, "as_of": as_of, "source": "bybit_ticker",
                      "interval": "24h", "retrieved_at": captured_at}
            try:
                if _finite_positive(reference) and _finite_positive(current):
                    record.update(reference_price=float(reference), current_price=float(current))
                elif ticker.get("price24hPcnt") is not None:
                    record["return"] = float(ticker["price24hPcnt"])
                else:
                    continue
            except (TypeError, ValueError, OverflowError):
                continue
            records.append(record)
        return records
    except Exception as exc:
        print(f"[symbol_rotation] ticker fetch failed: {type(exc).__name__}: {exc}")
        return []
    finally:
        if own_client:
            http_client.close()


def refresh_feed(conn, boundary: datetime, *, records: Iterable[Mapping[str, object]] | None = None,
                 now: datetime | None = None, path: str | Path | None = None,
                 source_cutoff: datetime | None = None) -> dict:
    """Publish at most one feed per UTC boundary, retaining or falling back safely."""
    boundary = rotation_boundary(boundary)
    current = read_feed(path, boundary)
    if current and current.get("valid_from") == boundary.isoformat().replace("+00:00", "Z"):
        if current.get("schema_version") == LEGACY_SCHEMA_VERSION:
            migrated = _migrate_legacy_feed(current, boundary, _utc(now or datetime.now(timezone.utc)))
            write_feed(migrated, path)
            return migrated
        if current.get("status") == "fallback" and records is not None:
            upgraded = build_feed(
                records, boundary, generated_at=now, previous_feed=current,
                source_cutoff=source_cutoff,
            )
            if upgraded.get("status") == "ready":
                write_feed(upgraded, path)
                return upgraded
        if current.get("status") == "fallback" and set(current.get("symbols", [])) != set(PERMANENT_SYMBOLS):
            current = _fallback_feed(
                boundary, now or datetime.now(timezone.utc),
                current.get("fallback_reason") or "no valid performance snapshot",
            )
            write_feed(current, path)
        return current
    previous = read_feed(path, boundary, allow_expired=True)
    source_records = list(records) if records is not None else _db_performance_records(conn, boundary)
    feed = build_feed(
        source_records, boundary, generated_at=now, previous_feed=previous,
        source_cutoff=source_cutoff,
    )
    write_feed(feed, path)
    return feed


def subscription_assets(at: datetime | None = None) -> tuple[list[str], dict]:
    """Return the rotation selections plus permanent assets for subscriptions."""
    assets = approved_assets()
    if not getattr(config, "SYMBOL_ROTATION_ENABLED", True):
        assets = _cap_assets(assets)
        metadata = {
            "feed_id": "disabled",
            "status": "disabled",
            "freshness_state": "disabled",
            "fallback_reason": None,
            "effective_universe_version": _effective_universe_version(assets, []),
        }
        return assets, metadata
    feed = read_feed(at=at, allow_expired=True)
    selected, metadata = effective_state_at(feed, at)
    if feed is None:
        metadata["fallback_reason"] = "missing or invalid feed"
    elif metadata.get("status") in {"degraded", "permanent_fallback"}:
        metadata["fallback_reason"] = metadata.get("fallback_reason") or "feed stale or watchlist expired"
    return sorted(set(selected)), metadata


def select_symbols(conn, candidates: Iterable[tuple[str, str]], cutoff: datetime) -> list[tuple[str, str]]:
    """Compatibility selector for callers with an explicit candidate list."""
    candidates = list(candidates)
    if not getattr(config, "SYMBOL_ROTATION_ENABLED", True) or not candidates:
        return candidates
    records = _db_performance_records(conn, cutoff, pool={asset for _, asset in candidates})
    ranking = rank_performance(records, cutoff, pool={asset for _, asset in candidates})
    selected = {item["asset"] for item in ranking["gainers"] + ranking["losers"]}
    return [
        (symbol, asset) for symbol, asset in candidates
        if str(asset).upper().removesuffix("USDT") in selected
    ] if selected else candidates


def run_worker() -> None:
    """Run the rotation plugin at fixed UTC boundaries as a managed process."""
    last_boundary = None
    cached_records: list[dict] = []
    last_snapshot_at = datetime.min.replace(tzinfo=timezone.utc)
    while True:
        now = datetime.now(timezone.utc)
        boundary = rotation_boundary(now)
        if now - last_snapshot_at >= timedelta(minutes=1):
            records = fetch_bybit_ticker_snapshot(boundary)
            if records:
                cached_records = records
                last_snapshot_at = now
        if boundary != last_boundary:
            connection = config.get_db_connection(read_only=True, db_path=config.MARKET_DB_PATH)
            try:
                # A snapshot captured before the boundary is the only valid
                # source for this decision. The cache avoids post-boundary
                # ticker data being mislabeled as point-in-time history.
                source_cutoff = datetime.now(timezone.utc) if last_boundary is None else boundary
                records = cached_records if cached_records and all(
                    _utc(record["as_of"]) <= source_cutoff for record in cached_records
                ) else None
                feed = refresh_feed(
                    connection, boundary, records=records, now=now,
                    source_cutoff=source_cutoff if source_cutoff != boundary else None,
                )
            finally:
                connection.close()
            print(
                f"[symbol_rotation] feed={feed['feed_id']} status={feed['status']} "
                f"symbols={feed['symbol_count']} fallback={feed.get('fallback_reason')}"
            )
            last_boundary = boundary
        next_boundary = boundary + timedelta(hours=int(getattr(config, "SYMBOL_ROTATION_REFRESH_HOURS", 4)))
        time.sleep(min(60.0, max(1.0, (next_boundary - datetime.now(timezone.utc)).total_seconds())))


if __name__ == "__main__":
    run_worker()
