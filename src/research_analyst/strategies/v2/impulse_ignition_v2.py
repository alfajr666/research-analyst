"""impulse-ignition-v2 — breakout of 1h base lid after compression (confluence ADR)."""

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
    load_btc_15m,
    prior_base_expansion_fail,
    prior_range_ratio,
    resample_ohlcv,
    resolve_bias,
    snapshot_zones_for_asset,
    structure_bias_4h,
    zone_bias_4h,
    zone_stack_and_ltf_scores,
)


STRATEGY_ID = "impulse-ignition-v2"
SETUP_CLASS = "impulse_ignition"
PHASE = "armed_base_breakout"
PLUGIN_VERSION = "v2"


@dataclass(frozen=True)
class IgnV2Config:
    n: int = 12
    k: float = 2.0
    p: int = 20
    c_ratio: float = 0.85
    g: float = 0.25
    e: float = 0.35
    r_max: float = 2.5
    s_min: float = 0.55
    n_top: int = 3
    target_r: float = 2.0
    horizon_hours: int = 4
    weights: dict[str, float] | None = None

    def __post_init__(self):
        if self.weights is None:
            object.__setattr__(
                self,
                "weights",
                {
                    "ltf_inside_htf": 0.12,
                    "zone_stack_tightness": 0.10,
                    "vp_proximity": 0.06,
                    "compression_quality": 0.14,
                    "edge_proximity": 0.14,
                    "volume_dryup": 0.08,
                    "oi_pressure": 0.10,
                    "funding_neutral": 0.06,
                    "relative_strength": 0.08,
                    "prior_impulse_quality": 0.08,
                    "candle_quality": 0.04,
                    "contradiction_penalty": 0.05,
                },
            )


def load_config() -> IgnV2Config:
    return IgnV2Config(
        n=int(getattr(config, "IGN_V2_N", 12)),
        k=float(getattr(config, "IGN_V2_K", 2.0)),
        p=int(getattr(config, "IGN_V2_P", 20)),
        c_ratio=float(getattr(config, "IGN_V2_C_RATIO", 0.85)),
        g=float(getattr(config, "IGN_V2_G", 0.25)),
        e=float(getattr(config, "IGN_V2_E", 0.35)),
        r_max=float(getattr(config, "IGN_V2_R_MAX", 2.5)),
        s_min=float(getattr(config, "IGN_V2_S_MIN", 0.55)),
        n_top=int(getattr(config, "IGN_V2_N_TOP", 3)),
    )


def _candle_quality(open_: float, close: float, high: float, low: float, direction: str) -> float:
    rng = high - low
    if rng <= 0:
        return 0.0
    body = abs(close - open_) / rng
    aligned = (direction == "long" and close >= open_) or (direction == "short" and close <= open_)
    return clamp01(body) * (1.0 if aligned else 0.4)


def _volume_dryup(bars_1h, n: int, p: int) -> float:
    if bars_1h.height < n + p:
        return 0.0
    base = bars_1h.tail(n)
    prior = bars_1h.tail(n + p).head(p)
    base_med = float(base["volume"].median())
    prior_med = float(prior["volume"].median())
    if prior_med <= 0:
        return 0.0
    dryup = 1.0 - base_med / prior_med
    return clamp01(dryup / 0.50)


def _oi_pressure(bars_15m, bars_1h, n: int) -> float:
    if "open_interest" not in bars_15m.columns or bars_1h.height < n:
        return 0.0
    base = bars_1h.tail(n)
    oi_start = float(base["open_interest"][0]) if "open_interest" in base.columns else 0.0
    oi_end = float(bars_15m["open_interest"][-1])
    if oi_start <= 0:
        return 0.0
    oi_chg = (oi_end - oi_start) / oi_start
    px_start = float(base["close"][0])
    px_end = float(bars_15m["close"][-1])
    if px_start <= 0:
        return 0.0
    px_chg = (px_end - px_start) / px_start
    pressure = oi_chg - max(px_chg, 0.0)
    return clamp01(pressure / 0.12)


def _funding_neutral(bars_15m) -> float:
    if "funding_rate" not in bars_15m.columns or bars_15m.height < 20:
        return 0.0
    funding = float(bars_15m["funding_rate"][-1])
    hist = bars_15m["funding_rate"].tail(min(bars_15m.height - 1, 96 * 14)).abs()
    if hist.len() < 10:
        return 0.0
    p90 = float(hist.quantile(0.90))
    if p90 <= 0:
        return 0.5
    return 1.0 if abs(funding) < p90 else 0.0


def _relative_strength(bars_15m, btc_15m, n_1h: int) -> float:
    if btc_15m is None or btc_15m.is_empty() or bars_15m.height < n_1h * 4:
        return 0.0
    window = n_1h * 4
    asset_start = float(bars_15m["close"][-window])
    asset_end = float(bars_15m["close"][-1])
    if asset_start <= 0:
        return 0.0
    asset_ret = (asset_end - asset_start) / asset_start
    btc = btc_15m.filter(btc_15m["timestamp"] <= bars_15m["timestamp"][-1]).tail(window)
    if btc.height < 2:
        return 0.0
    btc_start = float(btc["close"][0])
    btc_end = float(btc["close"][-1])
    if btc_start <= 0:
        return 0.0
    btc_ret = (btc_end - btc_start) / btc_start
    return clamp01((asset_ret - btc_ret + 0.02) / 0.10)


def _prior_impulse_quality(bars_1h, n: int, p: int) -> float:
    if bars_1h.height < n + p:
        return 0.0
    prior = bars_1h.tail(n + p).head(p)
    start = float(prior["close"][0])
    end = float(prior["close"][-1])
    if start <= 0:
        return 0.0
    impulse = (end - start) / start
    if impulse > 0.50:
        return 0.0
    return clamp01((impulse - 0.03) / 0.20)


def evaluate_symbol(
    bars_15m,
    *,
    asset: str,
    symbol: str,
    cutoff: datetime,
    zones: list[dict] | None = None,
    btc_15m=None,
    cfg: IgnV2Config | None = None,
    feature_extras: dict | None = None,
) -> dict | None:
    cfg = cfg or IgnV2Config()
    if bars_15m.is_empty() or not last_completed_bar_fresh(bars_15m, cutoff):
        return None

    bars_1h = resample_ohlcv(bars_15m, "1h")
    bars_4h = resample_ohlcv(bars_15m, "4h")
    need = cfg.n + cfg.p
    if bars_1h.height < max(need, 20) or bars_4h.height < 48:
        return None

    atr_1h = atr_last(bars_1h, 14)
    if atr_1h is None or atr_1h <= 0:
        return None

    ok_comp, base_high, base_low, base_range = compression_ok(bars_1h, cfg.n, cfg.k, atr_1h)
    if not ok_comp:
        return None

    ratio = prior_range_ratio(bars_1h, cfg.n, cfg.p)
    if ratio is None or ratio > cfg.c_ratio:
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

    # Inside base, not breached
    if not (base_low <= ref_close <= base_high):
        return None

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

    # No breach of lid (hard)
    if direction == "long" and ref_close > base_high:
        return None
    if direction == "short" and ref_close < base_low:
        return None

    # Near edge
    if direction == "long":
        edge_dist = base_high - ref_close
    else:
        edge_dist = ref_close - base_low
    if edge_dist > cfg.e * atr_1h:
        return None

    entry = base_high if direction == "long" else base_low
    invalidation = base_low if direction == "long" else base_high
    risk = abs(entry - invalidation)
    if risk <= 0 or risk > cfg.r_max * atr_1h:
        return None

    target = entry + risk * cfg.target_r if direction == "long" else entry - risk * cfg.target_r
    entry_type = "breakout_above" if direction == "long" else "breakout_below"

    ltf, stack = zone_stack_and_ltf_scores(local_zones, entry, atr_1h, direction)
    compression_quality = clamp01(1.0 - (base_range / (cfg.k * atr_1h))) * clamp01(
        (cfg.c_ratio - ratio) / max(cfg.c_ratio, 1e-9)
    )
    edge_prox = proximity_score(edge_dist / atr_1h, same=cfg.e * 0.33, near=cfg.e)
    vol_dry = _volume_dryup(bars_1h, cfg.n, cfg.p)
    oi = _oi_pressure(bars_15m, bars_1h, cfg.n)
    funding = _funding_neutral(bars_15m)
    rs = _relative_strength(bars_15m, btc_15m, cfg.n)
    prior_imp = _prior_impulse_quality(bars_1h, cfg.n, cfg.p)
    candle_q = _candle_quality(ref_open, ref_close, ref_high, ref_low, direction)
    contradiction = 0.15 if s_bias == "missing" or z_bias == "missing" else 0.0

    vp_prox = 0.0
    if feature_extras and feature_extras.get("vp_proximity") is not None:
        vp_prox = clamp01(float(feature_extras["vp_proximity"]))

    components = {
        "ltf_inside_htf": ltf,
        "zone_stack_tightness": stack,
        "vp_proximity": vp_prox,
        "compression_quality": compression_quality,
        "edge_proximity": edge_prox,
        "volume_dryup": vol_dry,
        "oi_pressure": oi,
        "funding_neutral": funding,
        "relative_strength": rs,
        "prior_impulse_quality": prior_imp,
        "candle_quality": candle_q,
        "contradiction_penalty": contradiction,
    }
    score, weighted = weighted_confluence(components, cfg.weights or {})
    confidence, conf_status = confidence_from_confluence(score)

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
        "entry_condition": {"type": entry_type, "price": round(entry, 8)},
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
            "atr_1h": round(atr_1h, 8),
            "base_high": round(base_high, 8),
            "base_low": round(base_low, 8),
            "base_range": round(base_range, 8),
            "compression_ratio": round(ratio, 6),
            "edge_distance_atr": round(edge_dist / atr_1h, 6),
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
            "risk": round(risk, 8),
            "completed_bar_at": observed_at.isoformat(),
        },
        "_confluence_score": score,
    }


def evaluate(
    conn,
    cutoff: datetime | None = None,
    *,
    cfg: IgnV2Config | None = None,
    snapshot: dict | None = None,
    alpha_db_path: str | Path | None = None,
    outbox_dir: Path | None = None,
    eval_interval: str = "15m",
) -> list[dict]:
    cfg = cfg or load_config()
    snapshot = snapshot or {}
    now = snapshot.get("now")
    cutoff = cutoff or completed_cycle_for(now, eval_interval)
    btc = load_bars_for_interval(conn, "BTC", eval_interval, cutoff).select(["timestamp", "close"])
    symbols = list_candidate_symbols(conn, cutoff)
    gated: list[dict] = []
    for symbol, asset in symbols:
        bars = load_bars_for_interval(conn, symbol, eval_interval, cutoff)
        zones = snapshot_zones_for_asset(snapshot, asset)
        extras = (snapshot.get("feature_snapshots") or {}).get(asset) or {}
        vp = extras.get("vp") or extras.get("openmarket_vp")
        feature_extras = {}
        if isinstance(vp, dict) and vp.get("proximity") is not None:
            feature_extras["vp_proximity"] = vp["proximity"]
        cand = evaluate_symbol(
            bars,
            asset=asset,
            symbol=symbol,
            cutoff=cutoff,
            zones=zones,
            btc_15m=btc,
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
    conn = config.get_db_connection(read_only=True, db_path=snapshot.get("db_path"))
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
