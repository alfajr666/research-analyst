"""accumulation-base-v2 — limit at 1h EMA99 inside 1h compression (confluence ADR)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import config
from alpha_outbox import write_event
from confluence_scoring import clamp01, confidence_from_confluence, proximity_score, weighted_confluence
from strategy_v2_context import (
    atr_last,
    completed_cycle,
    completed_cycle_for,
    compression_ok,
    compute_htf_zones,
    ema_last,
    has_active_event,
    last_completed_bar_fresh,
    list_candidate_symbols,
    load_15m_bars,
    load_bars_for_interval,
    prior_base_expansion_fail,
    resample_ohlcv,
    resolve_bias,
    snapshot_zones_for_asset,
    structure_bias_4h,
    zone_bias_4h,
    zone_stack_and_ltf_scores,
)


STRATEGY_ID = "accumulation-base-v2"
SETUP_CLASS = "accumulation_base"
PHASE = "armed_compression_pullback"
PLUGIN_VERSION = "v2"


@dataclass(frozen=True)
class AccV2Config:
    n: int = 12
    k: float = 2.0
    g: float = 0.25
    d_max: float = 0.50
    r_max: float = 2.5
    s_min: float = 0.55
    n_top: int = 3
    ema_inv_pct: float = 0.015
    target_r: float = 2.0
    horizon_hours: int = 4
    weights: dict[str, float] | None = None

    def __post_init__(self):
        if self.weights is None:
            object.__setattr__(
                self,
                "weights",
                {
                    "ltf_inside_htf": 0.18,
                    "zone_stack_tightness": 0.15,
                    "vp_proximity": 0.08,
                    "compression_quality": 0.20,
                    "volume_character": 0.10,
                    "ema_proximity": 0.18,
                    "candle_quality": 0.08,
                    "contradiction_penalty": 0.08,
                },
            )


def load_config() -> AccV2Config:
    return AccV2Config(
        n=int(getattr(config, "ACC_V2_N", 12)),
        k=float(getattr(config, "ACC_V2_K", 2.0)),
        g=float(getattr(config, "ACC_V2_G", 0.25)),
        d_max=float(getattr(config, "ACC_V2_D_MAX", 0.50)),
        r_max=float(getattr(config, "ACC_V2_R_MAX", 2.5)),
        s_min=float(getattr(config, "ACC_V2_S_MIN", 0.55)),
        n_top=int(getattr(config, "ACC_V2_N_TOP", 3)),
    )


def _volume_character(bars_1h, n: int) -> float:
    if bars_1h.height < n + 5:
        return 0.0
    base = bars_1h.tail(n)
    prior = bars_1h.tail(n + n).head(n)
    base_med = float(base["volume"].median())
    prior_med = float(prior["volume"].median())
    if prior_med <= 0:
        return 0.0
    ratio = base_med / prior_med
    # dry-up preferred; mild spike still ok soft
    if ratio <= 0.7:
        return clamp01((0.7 - ratio) / 0.5 + 0.5)
    if ratio <= 1.2:
        return 0.45
    return clamp01(1.0 - (ratio - 1.2) / 1.5)


def _candle_quality(open_: float, close: float, high: float, low: float, direction: str) -> float:
    rng = high - low
    if rng <= 0:
        return 0.0
    body = abs(close - open_)
    body_frac = body / rng
    aligned = (direction == "long" and close >= open_) or (direction == "short" and close <= open_)
    return clamp01(body_frac) * (1.0 if aligned else 0.4)


def evaluate_symbol(
    bars_15m,
    *,
    asset: str,
    symbol: str,
    cutoff: datetime,
    zones: list[dict] | None = None,
    cfg: AccV2Config | None = None,
    feature_extras: dict | None = None,
) -> dict | None:
    """Pure evaluate: return candidate event dict or None if hard gates fail."""
    cfg = cfg or AccV2Config()
    if bars_15m.is_empty() or not last_completed_bar_fresh(bars_15m, cutoff):
        return None

    bars_1h = resample_ohlcv(bars_15m, "1h")
    bars_4h = resample_ohlcv(bars_15m, "4h")
    if bars_1h.height < max(cfg.n, 99) or bars_4h.height < 48:
        return None

    atr_1h = atr_last(bars_1h, 14)
    if atr_1h is None or atr_1h <= 0:
        return None

    ok_comp, base_high, base_low, base_range = compression_ok(bars_1h, cfg.n, cfg.k, atr_1h)
    if not ok_comp:
        return None

    ref_close = float(bars_15m["close"][-1])
    ref_open = float(bars_15m["open"][-1])
    ref_high = float(bars_15m["high"][-1])
    ref_low = float(bars_15m["low"][-1])
    observed_at = bars_15m["timestamp"][-1]
    if hasattr(observed_at, "to_pydatetime"):
        observed_at = observed_at.to_pydatetime()
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)

    local_zones = list(zones or [])
    if not local_zones:
        local_zones = compute_htf_zones(bars_1h, bars_4h)

    atr_4h = atr_last(bars_4h, 14)
    s_bias = structure_bias_4h(bars_4h)
    z_bias, nearest_zone = zone_bias_4h(local_zones, ref_close, atr_4h)
    direction = resolve_bias(s_bias, z_bias)
    if direction is None:
        return None

    if prior_base_expansion_fail(bars_1h, cfg.n, cfg.g, atr_1h, direction):
        return None

    ema99 = ema_last(bars_1h["close"].to_list(), 99)
    if ema99 is None or ema99 <= 0:
        return None

    ema_dist_atr = abs(ref_close - ema99) / atr_1h
    if ema_dist_atr > cfg.d_max:
        return None

    entry = max(ema99, ref_close) if direction == "long" else min(ema99, ref_close)
    band = ema99 * (1.0 - cfg.ema_inv_pct) if direction == "long" else ema99 * (1.0 + cfg.ema_inv_pct)
    base_inv = base_low if direction == "long" else base_high
    if direction == "long":
        invalidation = min(band, base_inv)
    else:
        invalidation = max(band, base_inv)

    risk = abs(entry - invalidation)
    if risk <= 0 or risk > cfg.r_max * atr_1h:
        return None

    target = entry + risk * cfg.target_r if direction == "long" else entry - risk * cfg.target_r

    ltf, stack = zone_stack_and_ltf_scores(local_zones, entry, atr_1h, direction)
    compression_quality = clamp01(1.0 - (base_range / (cfg.k * atr_1h)))
    vol_char = _volume_character(bars_1h, cfg.n)
    ema_prox = proximity_score(ema_dist_atr, same=cfg.d_max * 0.33, near=cfg.d_max)
    candle_q = _candle_quality(ref_open, ref_close, ref_high, ref_low, direction)
    contradiction = 0.0
    if s_bias in ("long", "short") and z_bias in ("long", "short") and s_bias != z_bias:
        contradiction = 1.0  # should already hard-fail; keep soft 0
    elif s_bias == "missing" or z_bias == "missing":
        contradiction = 0.15

    vp_prox = 0.0
    if feature_extras and feature_extras.get("vp_proximity") is not None:
        vp_prox = clamp01(float(feature_extras["vp_proximity"]))

    components = {
        "ltf_inside_htf": ltf,
        "zone_stack_tightness": stack,
        "vp_proximity": vp_prox,
        "compression_quality": compression_quality,
        "volume_character": vol_char,
        "ema_proximity": ema_prox,
        "candle_quality": candle_q,
        "contradiction_penalty": contradiction,
    }
    score, weighted = weighted_confluence(components, cfg.weights or {})
    confidence, conf_status = confidence_from_confluence(score)

    ema48_4h = ema_last(bars_4h["close"].to_list(), 48)
    return {
        "schema_version": 1,
        "strategy_id": STRATEGY_ID,
        "asset": asset,
        "direction": direction,
        "setup_class": SETUP_CLASS,
        "phase": PHASE,
        "observed_at": observed_at.isoformat(),
        "valid_until": (observed_at + timedelta(hours=cfg.horizon_hours)).isoformat(),
        "horizon_minutes": cfg.horizon_hours * 60,
        "confidence": confidence,
        "confidence_status": conf_status,
        "entry_condition": {"type": "limit_at_ema_context", "price": round(entry, 8)},
        "invalidation_price": round(invalidation, 8),
        "targets": [round(target, 8)],
        "plugin_version": PLUGIN_VERSION,
        "feature_snapshot": {
            "source_symbol": symbol,
            "confluence_score": score,
            "confidence_components": weighted,
            "component_raw": {k: round(float(v), 6) for k, v in components.items()},
            "close_15m": round(ref_close, 8),
            "close_1h": round(float(bars_1h["close"][-1]), 8),
            "ema99_1h": round(ema99, 8),
            "ema48_4h": round(ema48_4h, 8) if ema48_4h else None,
            "atr_1h": round(atr_1h, 8),
            "base_high": round(base_high, 8),
            "base_low": round(base_low, 8),
            "base_range": round(base_range, 8),
            "structure_bias": s_bias,
            "zone_bias": z_bias,
            "nearest_zone": (
                {
                    "type": nearest_zone.get("type"),
                    "direction": nearest_zone.get("direction"),
                    "low": nearest_zone.get("low"),
                    "high": nearest_zone.get("high"),
                    "state": nearest_zone.get("state"),
                }
                if nearest_zone
                else None
            ),
            "ema_distance_atr": round(ema_dist_atr, 6),
            "risk": round(risk, 8),
            "completed_bar_at": observed_at.isoformat(),
        },
        "_confluence_score": score,
    }


def evaluate(
    conn,
    cutoff: datetime | None = None,
    *,
    cfg: AccV2Config | None = None,
    snapshot: dict | None = None,
    alpha_db_path: str | Path | None = None,
    outbox_dir: Path | None = None,
    eval_interval: str = "15m",
) -> list[dict]:
    """Score all candidates; apply S_min, top-N, then re-arm filter."""
    cfg = cfg or load_config()
    snapshot = snapshot or {}
    now = snapshot.get("now")
    cutoff = cutoff or completed_cycle_for(now, eval_interval)
    symbols = list_candidate_symbols(conn, cutoff)
    gated: list[dict] = []
    for symbol, asset in symbols:
        bars = load_bars_for_interval(conn, symbol, eval_interval, cutoff)
        zones = snapshot_zones_for_asset(snapshot, asset)
        extras = (snapshot.get("feature_snapshots") or {}).get(asset) or {}
        vp = extras.get("vp")
        feature_extras = {}
        if isinstance(vp, dict) and vp.get("proximity") is not None:
            feature_extras["vp_proximity"] = vp["proximity"]
        cand = evaluate_symbol(
            bars,
            asset=asset,
            symbol=symbol,
            cutoff=cutoff,
            zones=zones,
            cfg=cfg,
            feature_extras=feature_extras or None,
        )
        if cand is not None:
            gated.append(cand)

    eligible = [c for c in gated if float(c.get("_confluence_score", 0)) >= cfg.s_min]
    eligible.sort(key=lambda c: float(c.get("_confluence_score", 0)), reverse=True)
    top = eligible[: cfg.n_top]

    emitted: list[dict] = []
    for cand in top:
        if has_active_event(
            STRATEGY_ID,
            cand["asset"],
            cand["direction"],
            alpha_db_path=alpha_db_path,
            outbox_dir=outbox_dir,
            now=cutoff,
        ):
            continue
        clean = {k: v for k, v in cand.items() if not k.startswith("_")}
        emitted.append(clean)
    return emitted


def run_plugin(cutoff_id: str, snapshot: dict) -> list[dict]:
    conn = config.get_db_connection(read_only=True, db_path=snapshot.get("market_db_path"))
    try:
        now = snapshot.get("now")
        eval_interval = snapshot.get("eval_interval", "15m")
        cutoff = completed_cycle_for(now, eval_interval) if now else completed_cycle_for(None, eval_interval)
        events = evaluate(conn, cutoff, snapshot=snapshot, eval_interval=eval_interval)
        written = []
        for ev in events:
            ev["input_snapshot_id"] = cutoff_id
            ev["plugin_version"] = PLUGIN_VERSION
            created, _ = write_event(ev)
            if created:
                written.append(ev)
        return written
    finally:
        conn.close()
