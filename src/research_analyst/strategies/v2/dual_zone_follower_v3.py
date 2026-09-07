"""Cutoff-bound dual-zone trend pullback strategy for the current engine."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import config
from strategy_features import build_feature_frame, cached_feature_frame
from strategy_v2_context import (
    cutoff_from_id,
    ema_last,
    evaluation_symbols,
    has_active_event,
    load_bars_for_interval,
    strategy_market_connection,
)
from strategies.v2.adx import dmi_adx_last


LONG_STRATEGY_ID = "dual-zone-follower-v3"
SHORT_STRATEGY_ID = "dual-zone-short-follower-v3"
PLUGIN_VERSION = "v3"


def _positive(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def evaluate_symbol(bars, *, asset: str, symbol: str, cutoff: datetime | None,
                    direction: str = "long", features=None) -> dict | None:
    """Build one candidate from a completed 5m frame and cached EMA features."""
    if direction not in {"long", "short"}:
        return None
    required = max(
        config.DUAL_ZONE_V3_EXIT_EMA_LENGTH,
        config.DUAL_ZONE_V3_ANCHOR_EMA_LENGTH,
        config.DUAL_ZONE_V3_TREND_EMA_LENGTH,
    )
    if bars.is_empty() or bars.height < required:
        return None

    feature_frame = features if features is not None else bars
    if feature_frame.is_empty():
        return None
    row = feature_frame.row(-1, named=True)
    close = _positive(row.get("close"))
    if close is None:
        return None
    if features is None:
        closes = [float(value) for value in bars["close"].to_list()]
        ema_values = [
            ema_last(closes, length)
            for length in (
                config.DUAL_ZONE_V3_EXIT_EMA_LENGTH,
                config.DUAL_ZONE_V3_ANCHOR_EMA_LENGTH,
                config.DUAL_ZONE_V3_TREND_EMA_LENGTH,
            )
        ]
    else:
        ema_values = [
            row.get(f"ema_{length}")
            for length in (
                config.DUAL_ZONE_V3_EXIT_EMA_LENGTH,
                config.DUAL_ZONE_V3_ANCHOR_EMA_LENGTH,
                config.DUAL_ZONE_V3_TREND_EMA_LENGTH,
            )
        ]
    e7, e26, e99 = (_positive(value) for value in ema_values)
    if None in (e7, e26, e99):
        return None

    long = direction == "long"
    regime = (
        e26 > e99 and close > e26 and close > e99
        if long else e26 < e99 and close < e26 and close < e99
    )
    if not regime:
        return None

    distance_to_anchor = abs(close - e26) / e26 * 100.0
    distance_to_trend = abs(close - e99) / e99 * 100.0
    if distance_to_anchor <= config.DUAL_ZONE_V3_A_ENTRY_DISTANCE_PCT:
        zone = "A"
        anchor = e26
        target_pct = config.DUAL_ZONE_V3_A_TARGET_DISTANCE_PCT
        entry_distance_pct = distance_to_anchor
    elif distance_to_trend <= config.DUAL_ZONE_V3_B_ENTRY_DISTANCE_PCT:
        zone = "B"
        anchor = e99
        target_pct = config.DUAL_ZONE_V3_B_TARGET_DISTANCE_PCT
        entry_distance_pct = distance_to_trend
    else:
        return None

    stop_pct = (
        config.DUAL_ZONE_V3_A_STOP_DISTANCE_PCT
        if zone == "A" else config.DUAL_ZONE_V3_B_STOP_DISTANCE_PCT
    ) / 100.0
    stop = anchor * (1.0 - stop_pct if long else 1.0 + stop_pct)
    target = e7 * (1.0 + target_pct / 100.0 if long else 1.0 - target_pct / 100.0)
    observed_at = row.get("timestamp")
    if not isinstance(observed_at, datetime):
        return None
    observed_at = (
        observed_at.replace(tzinfo=timezone.utc)
        if observed_at.tzinfo is None else observed_at.astimezone(timezone.utc)
    )
    strategy_id = LONG_STRATEGY_ID if long else SHORT_STRATEGY_ID
    return {
        "schema_version": 1,
        "plugin_version": PLUGIN_VERSION,
        "strategy_id": strategy_id,
        "asset": asset.upper(),
        "direction": direction,
        "setup_class": "dual_zone_follower" if long else "dual_zone_short_follower",
        "phase": f"channel_{zone.lower()}",
        "observed_at": observed_at.isoformat(),
        "valid_until": (observed_at + timedelta(minutes=5)).isoformat(),
        "horizon_minutes": 5,
        "confidence": 0.5,
        "confidence_status": "uncalibrated",
        "entry_condition": {"type": "limit_at_ema_context", "price": close},
        "entry_price": close,
        "invalidation_price": stop,
        "targets": [target],
        "feature_snapshot": {
            "source_symbol": symbol,
            "execution_timeframe": "5m",
            "ema7": e7,
            "ema26": e26,
            "ema99": e99,
            "channel": zone,
            "entry_distance_pct": entry_distance_pct,
            "cutoff": cutoff.isoformat() if cutoff else None,
        },
    }


def _run(cutoff_id: str, snapshot: dict, direction: str) -> list[dict]:
    cutoff = cutoff_from_id(str(snapshot.get("cutoff_at") or cutoff_id), snapshot.get("now"))
    conn, owns_conn = strategy_market_connection(snapshot.get("market_db_path"))
    try:
        events = []
        feature_spec = {
            "ema": {
                f"ema_{config.DUAL_ZONE_V3_EXIT_EMA_LENGTH}": config.DUAL_ZONE_V3_EXIT_EMA_LENGTH,
                f"ema_{config.DUAL_ZONE_V3_ANCHOR_EMA_LENGTH}": config.DUAL_ZONE_V3_ANCHOR_EMA_LENGTH,
                f"ema_{config.DUAL_ZONE_V3_TREND_EMA_LENGTH}": config.DUAL_ZONE_V3_TREND_EMA_LENGTH,
            },
        }
        for symbol, asset in evaluation_symbols(conn, cutoff, snapshot):
            bars = load_bars_for_interval(conn, symbol, "5m", cutoff)
            features = cached_feature_frame(
                snapshot,
                f"dual-zone-v3-5m:{asset}:{cutoff.isoformat()}",
                bars,
                lambda frame: build_feature_frame(frame, **feature_spec),
                asset=asset,
                interval="5m",
                cutoff=cutoff,
                feature_spec=feature_spec,
            )
            adx_bars = load_bars_for_interval(
                conn, symbol, config.DUAL_ZONE_V3_ADX_TIMEFRAME, cutoff
            )
            dmi = dmi_adx_last(
                adx_bars,
                config.DUAL_ZONE_V3_ADX_LENGTH,
                config.DUAL_ZONE_V3_ADX_SMOOTHING,
                symbol=symbol,
                interval=config.DUAL_ZONE_V3_ADX_TIMEFRAME,
            )
            direction_ok = (
                dmi is not None
                and dmi[0] >= config.DUAL_ZONE_V3_MIN_ADX
                and (
                    not config.DUAL_ZONE_V3_USE_DI_DIRECTION
                    or (direction == "long" and dmi[1] > dmi[2])
                    or (direction == "short" and dmi[2] > dmi[1])
                )
            )
            event = (
                evaluate_symbol(
                    bars,
                    asset=asset,
                    symbol=symbol,
                    cutoff=cutoff,
                    direction=direction,
                    features=features,
                )
                if direction_ok else None
            )
            if event is None or has_active_event(event["strategy_id"], asset, direction, now=cutoff):
                continue
            event["input_snapshot_id"] = cutoff_id
            event["feature_snapshot"].update({
                "adx_1h": dmi[0],
                "+di_1h": dmi[1],
                "-di_1h": dmi[2],
                "cutoff": cutoff.isoformat(),
            })
            events.append(event)
        return events
    finally:
        if owns_conn:
            conn.close()


def run_plugin(cutoff_id: str, snapshot: dict) -> list[dict]:
    return _run(cutoff_id, snapshot, "long")


def run_short_plugin(cutoff_id: str, snapshot: dict) -> list[dict]:
    return _run(cutoff_id, snapshot, "short")
