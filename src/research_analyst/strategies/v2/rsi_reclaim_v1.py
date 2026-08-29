"""rsi-reclaim-v1 — 15m RSI pullback + EMA reclaim under 4h bias + 1h EMA200 sep."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import polars as pl

import config
from alpha_outbox import write_event
from confluence_scoring import clamp01, confidence_from_confluence, proximity_score, weighted_confluence
from strategy_v2_context import (
    atr_last,
    completed_cycle,
    completed_cycle_for,
    compute_htf_zones,
    ema_last,
    has_active_event,
    last_completed_bar_fresh,
    list_candidate_symbols,
    load_15m_bars,
    load_bars_for_interval,
    resample_ohlcv,
    resolve_bias,
    snapshot_zones_for_asset,
    structure_bias_4h,
    zone_bias_4h,
    zone_stack_and_ltf_scores,
)


STRATEGY_ID = "rsi-reclaim-v1"
SETUP_CLASS = "continuation_pullback"
PHASE = "confirmed_rsi_reclaim"
PLUGIN_VERSION = "v1"


@dataclass(frozen=True)
class RsiReclaimConfig:
    ema_fast: int = 20
    ema_mid: int = 50
    rsi_len: int = 14
    rsi_max: float = 45.0
    rsi_min: float = 55.0
    pullback_tol: float = 0.0008
    body_atr_min: float = 0.20
    sep_min: float = 0.003
    sep_max: float = 0.04
    r_max: float = 2.5
    s_min: float = 0.55
    n_top: int = 3
    inv_band: float = 0.015
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
                    "rsi_quality": 0.16,
                    "reclaim_body": 0.14,
                    "ema_touch_quality": 0.12,
                    "extension_quality": 0.12,
                    "stack_spread": 0.08,
                    "candle_quality": 0.06,
                    "contradiction_penalty": 0.04,
                },
            )


def load_config() -> RsiReclaimConfig:
    return RsiReclaimConfig(
        ema_fast=int(getattr(config, "RSI_RECLAIM_EMA_FAST", 20)),
        ema_mid=int(getattr(config, "RSI_RECLAIM_EMA_MID", 50)),
        rsi_len=int(getattr(config, "RSI_RECLAIM_RSI_LEN", 14)),
        rsi_max=float(getattr(config, "RSI_RECLAIM_RSI_MAX", 45.0)),
        rsi_min=float(getattr(config, "RSI_RECLAIM_RSI_MIN", 55.0)),
        pullback_tol=float(getattr(config, "RSI_RECLAIM_PULLBACK_TOL", 0.0008)),
        body_atr_min=float(getattr(config, "RSI_RECLAIM_BODY_ATR_MIN", 0.20)),
        sep_min=float(getattr(config, "RSI_RECLAIM_SEP_MIN", 0.003)),
        sep_max=float(getattr(config, "RSI_RECLAIM_SEP_MAX", 0.04)),
        r_max=float(getattr(config, "RSI_RECLAIM_R_MAX", 2.5)),
        s_min=float(getattr(config, "RSI_RECLAIM_S_MIN", 0.55)),
        n_top=int(getattr(config, "RSI_RECLAIM_N_TOP", 3)),
    )


def rsi_series(closes: Sequence[float], length: int = 14) -> list[float | None]:
    """Wilder RSI; returns list aligned to closes (None until warm)."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < length + 1 or length < 1:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, length + 1):
        delta = float(closes[i]) - float(closes[i - 1])
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / length
    avg_loss = losses / length
    if avg_loss == 0:
        out[length] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[length] = 100.0 - (100.0 / (1.0 + rs))
    for i in range(length + 1, n):
        delta = float(closes[i]) - float(closes[i - 1])
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (length - 1) + gain) / length
        avg_loss = (avg_loss * (length - 1) + loss) / length
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def _candle_quality(open_: float, close: float, high: float, low: float, direction: str) -> float:
    rng = high - low
    if rng <= 0:
        return 0.0
    body = abs(close - open_)
    body_frac = body / rng
    aligned = (direction == "long" and close >= open_) or (direction == "short" and close <= open_)
    return clamp01(body_frac) * (1.0 if aligned else 0.4)


def _rsi_quality(rsi: float, rsi_prev: float, direction: str, cfg: RsiReclaimConfig) -> float:
    turn = abs(rsi - rsi_prev)
    turn_score = clamp01(turn / 5.0)
    if direction == "long":
        # Prefer RSI near floor of pullback band (deeper pullback, still ≤ max)
        depth = clamp01((cfg.rsi_max - rsi) / max(cfg.rsi_max, 1.0))
    else:
        depth = clamp01((rsi - cfg.rsi_min) / max(100.0 - cfg.rsi_min, 1.0))
    return clamp01(0.55 * depth + 0.45 * turn_score)


def _extension_quality(sep: float, cfg: RsiReclaimConfig) -> float:
    span = cfg.sep_max - cfg.sep_min
    if span <= 0:
        return 0.0
    mid = (cfg.sep_min + cfg.sep_max) / 2.0
    # Peak at mid-band; 0 at edges
    return clamp01(1.0 - abs(sep - mid) / (span / 2.0))


def evaluate_symbol(
    bars_15m,
    *,
    asset: str,
    symbol: str,
    cutoff: datetime,
    zones: list[dict] | None = None,
    cfg: RsiReclaimConfig | None = None,
    feature_extras: dict | None = None,
) -> dict | None:
    """Pure evaluate: return candidate event dict or None if hard gates fail."""
    cfg = cfg or RsiReclaimConfig()
    if bars_15m.is_empty() or not last_completed_bar_fresh(bars_15m, cutoff):
        return None

    bars_1h = resample_ohlcv(bars_15m, "1h")
    bars_4h = resample_ohlcv(bars_15m, "4h")
    # EMA200_1h needs ~200 hours; 4h EMA48 needs 48 bars
    if bars_1h.height < 200 or bars_4h.height < 48:
        return None
    if bars_15m.height < max(cfg.ema_mid, cfg.rsi_len) + 5:
        return None

    atr_15m = atr_last(bars_15m, 14)
    atr_1h = atr_last(bars_1h, 14)
    if atr_15m is None or atr_15m <= 0:
        return None

    closes_15m = bars_15m["close"].to_list()
    ema_fast = ema_last(closes_15m, cfg.ema_fast)
    ema_mid = ema_last(closes_15m, cfg.ema_mid)
    if ema_fast is None or ema_mid is None or ema_fast <= 0 or ema_mid <= 0:
        return None

    rsi_vals = rsi_series(closes_15m, cfg.rsi_len)
    rsi = rsi_vals[-1]
    rsi_prev = rsi_vals[-2] if len(rsi_vals) >= 2 else None
    if rsi is None or rsi_prev is None:
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

    close_1h = float(bars_1h["close"][-1])
    ema200 = ema_last(bars_1h["close"].to_list(), 200)
    if ema200 is None or ema200 <= 0:
        return None
    if direction == "long":
        sep = (close_1h - ema200) / ema200
    else:
        sep = (ema200 - close_1h) / ema200
    if not (cfg.sep_min <= sep <= cfg.sep_max):
        return None

    if direction == "long":
        if not (ema_fast > ema_mid):
            return None
        if not (rsi <= cfg.rsi_max and rsi >= rsi_prev):
            return None
        touch = ref_low <= ema_fast * (1.0 + cfg.pullback_tol) or ref_close <= ema_fast
        reclaim = ref_close > ema_fast and ref_close > ref_open
    else:
        if not (ema_fast < ema_mid):
            return None
        if not (rsi >= cfg.rsi_min and rsi <= rsi_prev):
            return None
        touch = ref_high >= ema_fast * (1.0 - cfg.pullback_tol) or ref_close >= ema_fast
        reclaim = ref_close < ema_fast and ref_close < ref_open
    if not (touch and reclaim):
        return None

    body_atr = abs(ref_close - ref_open) / atr_15m
    if body_atr < cfg.body_atr_min:
        return None

    entry = ref_close
    bar_extreme = ref_low if direction == "long" else ref_high
    band = ema_mid * (1.0 - cfg.inv_band) if direction == "long" else ema_mid * (1.0 + cfg.inv_band)
    if direction == "long":
        invalidation = min(bar_extreme, band)
    else:
        invalidation = max(bar_extreme, band)

    risk = abs(entry - invalidation)
    if risk <= 0 or risk > cfg.r_max * atr_15m:
        return None

    target = entry + risk * cfg.target_r if direction == "long" else entry - risk * cfg.target_r
    entry_type = "breakout_above" if direction == "long" else "breakout_below"

    ltf, stack = zone_stack_and_ltf_scores(local_zones, entry, atr_15m, direction)
    touch_dist = abs(min(ref_low, ref_close) - ema_fast) if direction == "long" else abs(max(ref_high, ref_close) - ema_fast)
    # Prefer wick that actually tagged near EMA
    ema_touch_q = proximity_score(touch_dist / atr_15m, same=0.15, near=0.75)
    stack_spread = abs(ema_fast - ema_mid) / atr_15m
    stack_score = clamp01(stack_spread / 1.5)
    reclaim_body_score = clamp01((body_atr - cfg.body_atr_min) / max(1.0 - cfg.body_atr_min, 0.1) * 0.5 + 0.5)
    candle_q = _candle_quality(ref_open, ref_close, ref_high, ref_low, direction)
    rsi_q = _rsi_quality(float(rsi), float(rsi_prev), direction, cfg)
    ext_q = _extension_quality(sep, cfg)

    contradiction = 0.0
    if s_bias in ("long", "short") and z_bias in ("long", "short") and s_bias != z_bias:
        contradiction = 1.0
    elif s_bias == "missing" or z_bias == "missing":
        contradiction = 0.15

    vp_prox = 0.0
    if feature_extras and feature_extras.get("vp_proximity") is not None:
        vp_prox = clamp01(float(feature_extras["vp_proximity"]))

    components = {
        "ltf_inside_htf": ltf,
        "zone_stack_tightness": stack,
        "vp_proximity": vp_prox,
        "rsi_quality": rsi_q,
        "reclaim_body": reclaim_body_score,
        "ema_touch_quality": ema_touch_q,
        "extension_quality": ext_q,
        "stack_spread": stack_score,
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
            "close_1h": round(close_1h, 8),
            "ema_fast_15m": round(ema_fast, 8),
            "ema_mid_15m": round(ema_mid, 8),
            "ema200_1h": round(ema200, 8),
            "ema48_4h": round(ema48_4h, 8) if ema48_4h else None,
            "atr_15m": round(atr_15m, 8),
            "atr_1h": round(atr_1h, 8) if atr_1h else None,
            "rsi_15m": round(float(rsi), 4),
            "rsi_prev_15m": round(float(rsi_prev), 4),
            "sep_1h": round(sep, 6),
            "body_atr": round(body_atr, 6),
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
    cfg: RsiReclaimConfig | None = None,
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
