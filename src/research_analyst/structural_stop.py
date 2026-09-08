"""Admission-owned HTF structural context and stop validation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import hashlib
from typing import Any

import config


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


STRUCTURAL_ATR_PERIOD = 14
STRUCTURAL_MIN_ATR_MULTIPLE = 0.5
STRUCTURAL_MAX_ATR_MULTIPLE = 3.0
STRUCTURAL_ADMISSION_CONTRACT_VERSION = "structural-sl-admission-v3"
STRUCTURAL_15M_ADMISSION_CONTRACT_VERSION = "structural-sl-admission-v4-15m"
STRUCTURAL_15M_SOURCE_MODE = "market_5m_resampled"
STRUCTURAL_15M_SOURCE_EXCHANGE = "bybit"
STRUCTURAL_15M_RESAMPLING_CONTRACT_VERSION = "execution-5m-to-15m-v1"
STRUCTURAL_ZONE_DETECTOR_VERSION = "structure-zones-v1"


def _timestamp(value: Any) -> datetime | None:
    try:
        return _utc(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _normalise_closed_bar_timestamp(value: Any) -> datetime:
    """Treat a boundary-minus-one-millisecond exchange timestamp as closed."""
    timestamp = _utc(value)
    if timestamp.microsecond == 999000:
        return (timestamp + timedelta(milliseconds=1)).replace(microsecond=0)
    return timestamp


def _canonical_asset(value: Any) -> str:
    text = str(value or "").upper().strip()
    if "/" in text:
        text = text.split("/", 1)[0]
    for suffix in ("_PERP.A", "_PERP", "-USDT-PERP", "USDT", "USD"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text


def _bar_window_is_covered(bars: Any, timeframe: str, required: int, cutoff: datetime) -> bool:
    """Require a contiguous, sufficiently warm direct-history window."""
    if bars is None or getattr(bars, "height", 0) < required:
        return False
    required_columns = {"timestamp", "open", "high", "low", "close"}
    if timeframe == "15m":
        required_columns.add("source_observation_ids")
    else:
        required_columns.add("bar_id")
    if not required_columns.issubset(set(getattr(bars, "columns", []))):
        return False
    if timeframe == "15m" and "source" not in bars.columns:
        return False
    validation_columns = set(required_columns)
    if timeframe == "15m":
        validation_columns.add("source")
        if "source_provenance" in bars.columns:
            validation_columns.add("source_provenance")
    for row in bars.select(sorted(validation_columns)).to_dicts():
        try:
            prices = [float(row[key]) for key in ("open", "high", "low", "close")]
        except (TypeError, ValueError):
            return False
        if not all(math.isfinite(price) and price > 0 for price in prices):
            return False
        if prices[2] > min(prices[0], prices[3]) or prices[1] < max(prices[0], prices[3]):
            return False
        if timeframe != "15m" and not str(row["bar_id"]).strip():
            return False
        if timeframe == "15m":
            source_ids = row["source_observation_ids"]
            if (
                not isinstance(source_ids, list)
                or not source_ids
                or not all(isinstance(value, str) and value.strip() for value in source_ids)
            ):
                return False
            sources = row.get("source_provenance") or [row.get("source")]
            if not all(
                source in {"bybit_rest", getattr(config, "BYBIT_WS_SOURCE", "bybit_ws")}
                for source in sources
            ):
                return False
    timestamps = [_timestamp(value) for value in bars["timestamp"].to_list()]
    seconds = {"15m": 900, "1h": 3600, "4h": 14400}.get(timeframe)
    if seconds is None:
        return False
    if not all(
        left is not None and right is not None
        and int((right - left).total_seconds()) == seconds
        for left, right in zip(timestamps, timestamps[1:])
    ):
        return False
    if timestamps[-1] is None:
        return False
    cutoff = _utc(cutoff)
    expected_end = cutoff.replace(second=0, microsecond=0)
    if timeframe == "15m":
        expected_end = expected_end.replace(minute=expected_end.minute - expected_end.minute % 15)
    else:
        expected_end = expected_end.replace(
            hour=cutoff.hour - cutoff.hour % (1 if timeframe == "1h" else 4),
            minute=0,
        )
    return timestamps[-1] == expected_end


def _zone_id(asset: str, timeframe: str, zone: dict[str, Any]) -> str:
    identity = "|".join(str(zone.get(key)) for key in (
        "type", "direction", "created_at", "low", "high",
    ))
    return "zone-" + hashlib.sha256(
        f"{asset.upper()}|{timeframe}|{identity}".encode("utf-8")
    ).hexdigest()[:32]


def _normalise_zone(zone: dict[str, Any], asset: str, timeframe: str) -> dict[str, Any] | None:
    if _canonical_asset(zone.get("asset")) != asset:
        return None
    zone_type = str(zone.get("type") or zone.get("kind") or "").lower()
    if zone_type not in {"fvg", "order_block"}:
        return None
    declared_timeframe = str(zone.get("timeframe") or "")
    if declared_timeframe.lower() != timeframe:
        return None
    try:
        low = float(zone["low"])
        high = float(zone["high"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) and value > 0 for value in (low, high)) or low > high:
        return None
    created_at = _timestamp(zone.get("created_at") or zone.get("end"))
    evidence = zone.get("source_evidence_ids")
    confirmed_at = _timestamp(zone.get("confirmed_at"))
    if (
        created_at is None
        or confirmed_at is None
        or not isinstance(evidence, list)
        or not evidence
        or not all(isinstance(item, str) and item.strip() for item in evidence)
    ):
        return None
    generated_id = _zone_id(asset, timeframe, zone)
    result = dict(zone)
    result.update({
        "zone_id": str(zone.get("zone_id") or zone.get("reference_id") or generated_id),
        "reference_id": str(zone.get("reference_id") or zone.get("zone_id") or generated_id),
        "type": zone_type,
        "asset": asset.upper(),
        "timeframe": timeframe,
        "low": low,
        "high": high,
        "created_at": created_at,
        "confirmed_at": confirmed_at,
        "coverage_status": zone.get("coverage_status"),
    })
    if any(zone.get(flag) for flag in ("stale", "is_stale", "superseded", "filled", "invalidated", "forming")):
        return None
    return result


def select_structural_zone(
    zones: list[dict[str, Any]],
    *,
    asset: str,
    direction: str,
    entry: float,
    cutoff: datetime,
) -> dict[str, Any] | None:
    """Choose the latest eligible directional zone, with configured priority."""
    wanted = "bullish" if direction == "long" else "bearish" if direction == "short" else None
    asset = _canonical_asset(asset)
    if wanted is None or not _finite_positive(entry):
        return None
    cutoff = _utc(cutoff)
    timeframes = ("4h", "1h")
    if getattr(config, "STRUCTURAL_15M_ZONES_ENABLED", False):
        timeframes += ("15m",)
    for timeframe in timeframes:
        eligible = []
        for raw in zones:
            zone = _normalise_zone(raw, asset, timeframe)
            if zone is None:
                continue
            if zone.get("timeframe") != timeframe or zone.get("state") not in ("active", "partial"):
                continue
            if zone.get("direction") != wanted or zone.get("coverage_status") != "covered":
                continue
            if zone["created_at"] > cutoff or zone["confirmed_at"] > cutoff:
                continue
            if zone.get("stale") or zone.get("is_stale"):
                continue
            if direction == "long" and zone["low"] > entry:
                continue
            if direction == "short" and zone["high"] < entry:
                continue
            eligible.append(zone)
        if eligible:
            return max(eligible, key=lambda zone: (zone["created_at"], zone["zone_id"]))
    return None


def build_structural_contexts(
    candidates: list[dict[str, Any]],
    cutoff: datetime,
    *,
    regime_db_path: str | None = None,
    market_db_path: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Build HTF zones and ATR only for assets that emitted candidates."""
    assets = sorted({_canonical_asset(candidate.get("asset")) for candidate in candidates if candidate.get("asset")})
    contexts = {
        asset: {
            "asset": asset,
            "cutoff": _utc(cutoff),
            "zones": [],
            "atr_by_timeframe": {},
            "atr_source_bar_ids": {},
            "frame_source_bar_ids": {},
            "coverage_status": {},
            "timeframe_provenance": {},
        }
        for asset in assets
    }
    if not assets:
        return contexts
    regime_conn = None
    market_conn = None
    try:
        from regime_history import load_regime_1h_bars, load_regime_4h_bars
        from strategy_v2_context import load_bars_for_interval, wilder_atr
        from structure_zones import detect_fvg, detect_order_blocks
        regime_conn = config.get_db_connection(
            read_only=True,
            db_path=regime_db_path or config.REGIME_DB_PATH,
        )
        if getattr(config, "STRUCTURAL_15M_ZONES_ENABLED", False):
            market_conn = config.get_db_connection(
                read_only=True,
                db_path=market_db_path or config.MARKET_DB_PATH,
            )
    except Exception:
        if regime_conn is not None:
            regime_conn.close()
        if market_conn is not None:
            market_conn.close()
        return contexts
    try:
        for asset in assets:
            for timeframe, loader in (("4h", load_regime_4h_bars), ("1h", load_regime_1h_bars)):
                contexts[asset]["timeframe_provenance"][timeframe] = {
                    "source_mode": "bybit_rest_direct",
                }
                bars = loader(regime_conn, asset, cutoff)
                required = int(getattr(
                    config,
                    f"REGIME_{'4H' if timeframe == '4h' else '1H'}_READINESS_BARS",
                    57,
                ))
                covered = _bar_window_is_covered(bars, timeframe, required, cutoff)
                contexts[asset]["coverage_status"][timeframe] = "covered" if covered else "incomplete"
                if not covered:
                    continue
                atr = wilder_atr(bars, STRUCTURAL_ATR_PERIOD)
                if atr is None or not math.isfinite(atr) or atr <= 0:
                    contexts[asset]["coverage_status"][timeframe] = "invalid_atr"
                    continue
                contexts[asset]["atr_by_timeframe"][timeframe] = atr
                contexts[asset]["atr_source_bar_ids"][timeframe] = [
                    str(value) for value in bars["bar_id"].to_list()
                ] if "bar_id" in bars.columns else []
                for zone in detect_fvg(bars, atr=atr, tf=timeframe) + detect_order_blocks(bars, atr=atr, tf=timeframe):
                    source_zone = dict(zone)
                    source_zone.setdefault("asset", asset)
                    source_zone.setdefault("coverage_status", "covered")
                    source_zone.setdefault("confirmed_at", source_zone.get("created_at"))
                    normalised = _normalise_zone(source_zone, asset, timeframe)
                    if normalised is not None:
                        contexts[asset]["zones"].append(normalised)
            if market_conn is not None:
                timeframe = "15m"
                contexts[asset]["timeframe_provenance"][timeframe] = {
                    "source_mode": STRUCTURAL_15M_SOURCE_MODE,
                    "source_exchange": STRUCTURAL_15M_SOURCE_EXCHANGE,
                    "resampling_contract_version": STRUCTURAL_15M_RESAMPLING_CONTRACT_VERSION,
                    "zone_detector_version": STRUCTURAL_ZONE_DETECTOR_VERSION,
                }
                bars = load_bars_for_interval(
                    market_conn,
                    asset,
                    timeframe,
                    cutoff,
                    int(getattr(config, "STRUCTURAL_15M_LOOKBACK_DAYS", 16)),
                )
                required = int(getattr(config, "STRUCTURAL_15M_READINESS_BARS", 57))
                covered = _bar_window_is_covered(bars, timeframe, required, cutoff)
                contexts[asset]["coverage_status"][timeframe] = "covered" if covered else "incomplete"
                if covered:
                    atr = wilder_atr(bars, STRUCTURAL_ATR_PERIOD)
                    if atr is None or not math.isfinite(atr) or atr <= 0:
                        contexts[asset]["coverage_status"][timeframe] = "invalid_atr"
                    else:
                        contexts[asset]["atr_by_timeframe"][timeframe] = atr
                        source_ids = sorted({
                            str(item_id)
                            for row in bars["source_observation_ids"].to_list()
                            for item_id in row
                            if item_id
                        })
                        contexts[asset]["atr_source_bar_ids"][timeframe] = source_ids
                        contexts[asset]["frame_source_bar_ids"][timeframe] = source_ids
                        for zone in detect_fvg(bars, atr=atr, tf=timeframe) + detect_order_blocks(bars, atr=atr, tf=timeframe):
                            source_zone = dict(zone)
                            source_zone.setdefault("asset", asset)
                            source_zone.setdefault("coverage_status", "covered")
                            source_zone.setdefault("source_mode", STRUCTURAL_15M_SOURCE_MODE)
                            source_zone.setdefault("source_exchange", STRUCTURAL_15M_SOURCE_EXCHANGE)
                            source_zone.setdefault(
                                "resampling_contract_version", STRUCTURAL_15M_RESAMPLING_CONTRACT_VERSION,
                            )
                            source_zone.setdefault("zone_detector_version", STRUCTURAL_ZONE_DETECTOR_VERSION)
                            source_zone.setdefault("confirmed_at", source_zone.get("created_at"))
                            normalised = _normalise_zone(source_zone, asset, timeframe)
                            if normalised is not None:
                                contexts[asset]["zones"].append(normalised)
    finally:
        if regime_conn is not None:
            regime_conn.close()
        if market_conn is not None:
            market_conn.close()
    return contexts


def admit_selected_structural_stop(
    candidate: dict[str, Any],
    context: dict[str, Any] | None,
    *,
    cutoff: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply admission-owned zone selection and ATR buffer validation."""
    result: dict[str, Any] = {
        "structural_stop_gate": "fail",
        "structural_stop_reasons": [],
        "structural_admission_contract_version": STRUCTURAL_ADMISSION_CONTRACT_VERSION,
        "selected_zone_id": None,
        "selected_zone_kind": None,
        "selected_zone_asset": None,
        "selected_zone_timeframe": None,
        "selected_zone_state": None,
        "selected_zone_created_at": None,
        "selected_zone_confirmed_at": None,
        "selected_zone_coverage_status": None,
        "selected_zone_source_evidence_ids": [],
        "selected_zone_low": None,
        "selected_zone_high": None,
        "selected_zone_boundary": None,
        "entry_zone_location": None,
        "entry_zone_buffer": None,
        "entry_zone_buffer_atr": None,
        "structural_stop_buffer": None,
        "structural_stop_buffer_atr": None,
        "structural_atr": None,
        "structural_atr_period": STRUCTURAL_ATR_PERIOD,
        "structural_atr_method": "wilder",
        "structural_atr_source_bar_ids": [],
        "structural_context_cutoff": None,
    }
    if getattr(config, "STRUCTURAL_15M_ZONES_ENABLED", False):
        result["structural_admission_contract_version"] = STRUCTURAL_15M_ADMISSION_CONTRACT_VERSION
    if not isinstance(context, dict):
        result["structural_stop_gate"] = "unavailable"
        result["structural_stop_reasons"].append("structural context is unavailable")
        return result
    context_cutoff = _timestamp(context.get("cutoff") or cutoff or candidate.get("observed_at"))
    result["structural_context_cutoff"] = context_cutoff.isoformat() if context_cutoff else None
    candidate_asset = _canonical_asset(candidate.get("asset"))
    if _canonical_asset(context.get("asset")) != candidate_asset:
        result["structural_stop_reasons"].append("structural context asset does not match candidate")
    candidate_cutoff = _timestamp(
        candidate.get("cutoff_at")
        or (candidate.get("feature_snapshot") or {}).get("cutoff")
        or candidate.get("observed_at")
    )
    if candidate_cutoff is not None:
        candidate_cutoff = _normalise_closed_bar_timestamp(candidate_cutoff)
    if candidate_cutoff is not None and context_cutoff != candidate_cutoff:
        result["structural_stop_reasons"].append("structural context cutoff does not match candidate")
    if cutoff is not None and context_cutoff is not None and context_cutoff != _utc(cutoff):
        result["structural_stop_reasons"].append("structural context cutoff is not the requested cutoff")
    if cutoff is not None and context_cutoff is not None and context_cutoff > _utc(cutoff):
        result["structural_stop_reasons"].append("structural context cutoff is in the future")
    if now is not None and context_cutoff is not None and context_cutoff > _utc(now):
        result["structural_stop_reasons"].append("structural context cutoff is in the future")
    entry = candidate.get("entry_price")
    if entry is None:
        entry = (candidate.get("entry_condition") or {}).get("price")
    stop = candidate.get("invalidation_price", candidate.get("stop_loss"))
    direction = str(candidate.get("direction", "")).lower()
    if not _finite_positive(entry) or not _finite_positive(stop) or context_cutoff is None:
        result["structural_stop_reasons"].append("structural candidate prices or cutoff are invalid")
        return result
    zone = select_structural_zone(
        list(context.get("zones") or []),
        asset=str(candidate.get("asset", "")),
        direction=direction,
        entry=float(entry),
        cutoff=context_cutoff,
    )
    if zone is None:
        if getattr(config, "STRUCTURAL_15M_ZONES_ENABLED", False):
            fifteen_status = (context.get("coverage_status") or {}).get("15m")
            if fifteen_status is None:
                result["structural_stop_reasons"].append("15m structural context unavailable")
            elif fifteen_status != "covered":
                result["structural_stop_reasons"].append("15m structural frame incomplete or gapped")
            else:
                result["structural_stop_reasons"].append("no eligible 15m HTF structural zone")
        else:
            result["structural_stop_reasons"].append("no eligible HTF structural zone")
        return result
    timeframe = zone["timeframe"]
    atr = (context.get("atr_by_timeframe") or {}).get(timeframe)
    if not _finite_positive(atr):
        result["structural_stop_reasons"].append(f"{timeframe} structural ATR is unavailable")
        return result
    source_bar_ids = (context.get("atr_source_bar_ids") or {}).get(timeframe, [])
    if (
        not isinstance(source_bar_ids, list)
        or not source_bar_ids
        or not all(isinstance(value, str) and value.strip() for value in source_bar_ids)
    ):
        result["structural_stop_reasons"].append(f"{timeframe} structural ATR source bar IDs are unavailable")
        return result
    boundary = zone["low"] if direction == "long" else zone["high"]
    if direction == "long":
        entry_location = "inside" if float(entry) <= zone["high"] else "above"
        entry_buffer = 0.0 if entry_location == "inside" else float(entry) - zone["high"]
    else:
        entry_location = "inside" if float(entry) >= zone["low"] else "below"
        entry_buffer = 0.0 if entry_location == "inside" else zone["low"] - float(entry)
    buffer = boundary - float(stop) if direction == "long" else float(stop) - boundary
    entry_buffer_atr = entry_buffer / float(atr)
    buffer_atr = buffer / float(atr)
    result.update({
        "selected_zone_id": zone["zone_id"],
        "selected_zone_kind": zone.get("type") or zone.get("kind"),
        "selected_zone_asset": zone.get("asset"),
        "selected_zone_timeframe": timeframe,
        "selected_zone_state": zone.get("state"),
        "selected_zone_created_at": zone["created_at"].isoformat(),
        "selected_zone_confirmed_at": zone["confirmed_at"].isoformat(),
        "selected_zone_coverage_status": zone.get("coverage_status"),
        "selected_zone_source_evidence_ids": list(zone.get("source_evidence_ids") or []),
        "selected_zone_low": zone["low"],
        "selected_zone_high": zone["high"],
        "selected_zone_boundary": boundary,
        "entry_zone_location": entry_location,
        "entry_zone_buffer": entry_buffer,
        "entry_zone_buffer_atr": entry_buffer_atr,
        "structural_stop_buffer": buffer,
        "structural_stop_buffer_atr": buffer_atr,
        "structural_atr": float(atr),
        "structural_atr_source_bar_ids": source_bar_ids,
    })
    if timeframe == "15m":
        result["structural_admission_contract_version"] = STRUCTURAL_15M_ADMISSION_CONTRACT_VERSION
        result.update({
            "structural_15m_zones_enabled": True,
            "structural_source_mode": zone.get("source_mode"),
            "structural_source_exchange": zone.get("source_exchange"),
            "structural_resampling_contract_version": zone.get("resampling_contract_version"),
            "structural_zone_detector_version": zone.get("zone_detector_version"),
            "structural_frame_bar_ids": list(
                (context.get("frame_source_bar_ids") or {}).get("15m", source_bar_ids)
            ),
        })
    min_multiple = float(getattr(config, "STRUCTURAL_STOP_MIN_ATR_MULTIPLE", STRUCTURAL_MIN_ATR_MULTIPLE))
    max_multiple = float(getattr(config, "STRUCTURAL_STOP_MAX_ATR_MULTIPLE", STRUCTURAL_MAX_ATR_MULTIPLE))
    if entry_location != "inside":
        if entry_buffer_atr < min_multiple:
            result["structural_stop_reasons"].append(f"entry is too close to {timeframe} zone")
        if entry_buffer_atr > max_multiple:
            result["structural_stop_reasons"].append(f"entry is too far from {timeframe} zone")
    if buffer_atr < min_multiple:
        result["structural_stop_reasons"].append(
            f"{timeframe + ' ' if timeframe == '15m' else ''}structural stop buffer is below minimum ATR multiple"
        )
    if buffer_atr > max_multiple:
        result["structural_stop_reasons"].append(
            f"{timeframe + ' ' if timeframe == '15m' else ''}structural stop buffer is above maximum ATR multiple"
        )
    if not result["structural_stop_reasons"]:
        result["structural_stop_gate"] = "pass"
    return result
