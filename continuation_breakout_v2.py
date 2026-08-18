"""continuation-breakout-v2 — breakout of 1h flag after established 4h trend."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from alpha_outbox import write_event
from confluence_scoring import clamp01, confidence_from_confluence, proximity_score, weighted_confluence
from strategy_v2_context import (
    atr_last,
    completed_cycle,
    compression_ok,
    compute_htf_zones,
    has_active_event,
    last_completed_bar_fresh,
    list_candidate_symbols,
    load_15m_bars,
    load_btc_15m,
    resample_ohlcv,
    resolve_bias,
    snapshot_zones_for_asset,
    structure_bias_4h,
    zone_bias_4h,
    zone_stack_and_ltf_scores,
)


STRATEGY_ID = "continuation-breakout-v2"
SETUP_CLASS = "continuation_breakout"
PHASE = "armed_flag_breakout"
PLUGIN_VERSION = "v2"


@dataclass(frozen=True)
class ContV2Config:
    # 4h trend
    p: int = 12  # completed 4h bars for min trend print
    t_min: float = 0.75  # min signed return / ATR_4h
    # 1h flag
    n: int = 16
    k: float = 3.0
    retr_max: float = 0.50  # max pullback vs prior impulse
    g: float = 0.35
    e: float = 0.50
    # extension (15m window in bars; default ~1d)
    x_bars: int = 96
    x_max: float = 4.0  # max signed move / ATR_1h
    r_max: float = 3.0
    s_min: float = 0.40
    n_top: int = 5
    target_r: float = 1.5
    horizon_hours: int = 4
    weight_profile: str = "balanced"
    weights: dict[str, float] | None = None

    def __post_init__(self):
        if self.weights is None:
            object.__setattr__(
                self,
                "weights",
                {
                    "ltf_inside_htf": 0.10,
                    "zone_stack_tightness": 0.10,
                    "vp_proximity": 0.05,
                    "flag_compression_quality": 0.12,
                    "edge_proximity": 0.12,
                    "trend_quality": 0.14,
                    "retrace_quality": 0.10,
                    "acceptance": 0.06,
                    "participation": 0.08,
                    "relative_strength": 0.07,
                    "funding_neutral": 0.04,
                    "extension_penalty": 0.08,
                    "candle_quality": 0.04,
                    "contradiction_penalty": 0.05,
                },
            )


def load_config() -> ContV2Config:
    return ContV2Config(
        p=int(getattr(config, "CONT_V2_P", 12)),
        t_min=float(getattr(config, "CONT_V2_T_MIN", 0.75)),
        n=int(getattr(config, "CONT_V2_N", 16)),
        k=float(getattr(config, "CONT_V2_K", 3.0)),
        retr_max=float(getattr(config, "CONT_V2_RETR_MAX", 0.50)),
        g=float(getattr(config, "CONT_V2_G", 0.35)),
        e=float(getattr(config, "CONT_V2_E", 0.50)),
        x_bars=int(getattr(config, "CONT_V2_X_BARS", 96)),
        x_max=float(getattr(config, "CONT_V2_X_MAX", 4.0)),
        r_max=float(getattr(config, "CONT_V2_R_MAX", 3.0)),
        s_min=float(getattr(config, "CONT_V2_S_MIN", 0.40)),
        n_top=int(getattr(config, "CONT_V2_N_TOP", 5)),
        weight_profile=str(getattr(config, "CONT_V2_WEIGHT_PROFILE", "balanced")),
    )


def _candle_quality(open_: float, close: float, high: float, low: float, direction: str) -> float:
    rng = high - low
    if rng <= 0:
        return 0.0
    body = abs(close - open_) / rng
    aligned = (direction == "long" and close >= open_) or (direction == "short" and close <= open_)
    return clamp01(body) * (1.0 if aligned else 0.4)


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


def _relative_strength(bars_15m, btc_15m, window: int) -> float:
    if btc_15m is None or btc_15m.is_empty() or bars_15m.height < window:
        return 0.0
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


def _participation(bars_15m, bars_1h, n: int) -> float:
    if bars_15m.height < 20 or bars_1h.height < n:
        return 0.0
    vol_med = float(bars_15m["volume"].tail(96).head(95).median()) if bars_15m.height >= 96 else float(
        bars_15m["volume"].head(max(bars_15m.height - 1, 1)).median()
    )
    last_vol = float(bars_15m["volume"][-1])
    vol_part = clamp01((last_vol / vol_med - 1.0) / 2.0) if vol_med > 0 else 0.0
    oi_part = 0.0
    if "open_interest" in bars_15m.columns and bars_15m.height >= 5:
        oi_now = float(bars_15m["open_interest"][-1])
        oi_prev = float(bars_15m["open_interest"][-5])
        if oi_prev > 0:
            oi_part = clamp01(((oi_now - oi_prev) / oi_prev) / 0.05)
    return clamp01(0.7 * vol_part + 0.3 * oi_part)


def _trend_print_4h(
    bars_4h,
    p: int,
    direction: str,
    atr_4h: float,
    flag_4h_bars: int = 3,
) -> tuple[bool, float]:
    """ATR-normalized signed return of the impulse *into* the flag (not through it)."""
    # End the trend window just before the recent pause so a healthy flag does not
    # erase an established 4h move.
    tail = max(int(flag_4h_bars), 1)
    need = p + tail + 1
    if bars_4h.height < need or atr_4h <= 0:
        return False, 0.0
    end = float(bars_4h["close"][-(tail + 1)])
    start = float(bars_4h["close"][-(tail + 1 + p)])
    if start <= 0:
        return False, 0.0
    norm = (end - start) / atr_4h
    signed_norm = norm if direction == "long" else -norm
    return signed_norm >= 0, signed_norm


def _impulse_and_retrace(
    bars_1h,
    n: int,
    direction: str,
    flag_high: float,
    flag_low: float,
    impulse_mult: int = 8,
) -> tuple[float, float] | None:
    """
    Prior impulse = directional leg *before* the flag window (not the flag itself).
    Retrace = pullback from that impulse extreme into the flag, as fraction of impulse.
    Returns (impulse_size, retrace_fraction) or None if undefined.
    """
    # Prefer multi-day lookback so the impulse is the trend leg, not the pause.
    impulse_len = min(max(n * impulse_mult, 72), max(bars_1h.height - n, 0))
    if impulse_len < 4:
        return None
    impulse = bars_1h.tail(n + impulse_len).head(impulse_len)
    if direction == "long":
        impulse_extreme = float(impulse["high"].max())
        impulse_start = float(impulse["low"].min())
        impulse_size = impulse_extreme - impulse_start
        if impulse_size <= 0:
            return None
        retrace = (impulse_extreme - flag_low) / impulse_size
    else:
        impulse_extreme = float(impulse["low"].min())
        impulse_start = float(impulse["high"].max())
        impulse_size = impulse_start - impulse_extreme
        if impulse_size <= 0:
            return None
        retrace = (flag_high - impulse_extreme) / impulse_size
    return impulse_size, max(0.0, retrace)


def _extension_atr(bars_15m, x_bars: int, atr_1h: float, direction: str) -> float:
    if bars_15m.height < x_bars + 1 or atr_1h <= 0:
        return 0.0
    start = float(bars_15m["close"][-(x_bars + 1)])
    end = float(bars_15m["close"][-1])
    move = (end - start) / atr_1h
    return move if direction == "long" else -move


def _acceptance_soft(bars_15m, flag_high: float, flag_low: float, direction: str) -> float:
    """Soft: recent wicks probing beyond flag edge without close breach (already gated)."""
    if bars_15m.height < 4:
        return 0.0
    recent = bars_15m.tail(4)
    if direction == "long":
        probes = sum(1 for h in recent["high"].to_list() if float(h) >= flag_high * 0.999)
    else:
        probes = sum(1 for lo in recent["low"].to_list() if float(lo) <= flag_low * 1.001)
    return clamp01(probes / 4.0)


def evaluate_symbol(
    bars_15m,
    *,
    asset: str,
    symbol: str,
    cutoff: datetime,
    zones: list[dict] | None = None,
    btc_15m=None,
    cfg: ContV2Config | None = None,
    feature_extras: dict | None = None,
) -> dict | None:
    cfg = cfg or ContV2Config()
    if bars_15m.is_empty() or not last_completed_bar_fresh(bars_15m, cutoff):
        return None

    bars_1h = resample_ohlcv(bars_15m, "1h")
    bars_4h = resample_ohlcv(bars_15m, "4h")
    if bars_1h.height < max(cfg.n + 8, 20) or bars_4h.height < max(cfg.p + 1, 48):
        return None

    atr_1h = atr_last(bars_1h, 14)
    atr_4h = atr_last(bars_4h, 14)
    if atr_1h is None or atr_1h <= 0 or atr_4h is None or atr_4h <= 0:
        return None

    # Flag from the last N *fully closed* 1h bars before the hour that contains
    # the arming 15m bar, so the trigger print cannot expand the lid.
    ref_ts = bars_15m["timestamp"][-1]
    if hasattr(ref_ts, "to_pydatetime"):
        ref_ts = ref_ts.to_pydatetime()
    if ref_ts.tzinfo is None:
        ref_ts = ref_ts.replace(tzinfo=timezone.utc)
    hour_start = ref_ts.replace(minute=0, second=0, microsecond=0)
    closed_1h = bars_1h.filter(bars_1h["timestamp"] < hour_start)
    if closed_1h.height < cfg.n:
        closed_1h = bars_1h  # fallback when history is short
    ok_flag, flag_high, flag_low, flag_range = compression_ok(closed_1h, cfg.n, cfg.k, atr_1h)
    if not ok_flag:
        return None

    ref_close = float(bars_15m["close"][-1])
    ref_open = float(bars_15m["open"][-1])
    ref_high = float(bars_15m["high"][-1])
    ref_low = float(bars_15m["low"][-1])
    observed_at = ref_ts

    if not (flag_low <= ref_close <= flag_high):
        return None

    local_zones = list(zones or [])
    if not local_zones:
        local_zones = compute_htf_zones(bars_1h, bars_4h)

    s_bias = structure_bias_4h(bars_4h)
    z_bias, nearest_zone = zone_bias_4h(local_zones, ref_close, atr_4h)
    direction = resolve_bias(s_bias, z_bias)
    if direction is None:
        return None

    # 4h min trend print (ATR-normalized), ending before the recent flag pause
    flag_4h = max(1, (cfg.n + 3) // 4)
    _ok_sign, trend_norm = _trend_print_4h(bars_4h, cfg.p, direction, atr_4h, flag_4h_bars=flag_4h)
    if trend_norm < cfg.t_min:
        return None

    # Retrace cap vs prior impulse (measured on closed 1h series used for the flag)
    impulse_info = _impulse_and_retrace(closed_1h, cfg.n, direction, flag_high, flag_low)
    if impulse_info is None:
        return None
    impulse_size, retrace_frac = impulse_info
    if retrace_frac > cfg.retr_max:
        return None

    # Counter-trend expansion of prior (N-1) range negates the flag (spec gate).
    prior = closed_1h.tail(cfg.n).head(cfg.n - 1)
    if prior.height >= 1:
        ph, pl = float(prior["high"].max()), float(prior["low"].min())
        last_c = float(closed_1h["close"][-1])
        grace = cfg.g * atr_1h
        if direction == "long" and last_c < pl - grace:
            return None
        if direction == "short" and last_c > ph + grace:
            return None
    # No breach of flag lid
    if direction == "long" and ref_close > flag_high:
        return None
    if direction == "short" and ref_close < flag_low:
        return None

    if direction == "long":
        edge_dist = flag_high - ref_close
    else:
        edge_dist = ref_close - flag_low
    if edge_dist > cfg.e * atr_1h:
        return None

    # Extension hard cap
    ext = _extension_atr(bars_15m, cfg.x_bars, atr_1h, direction)
    if ext > cfg.x_max:
        return None

    entry = flag_high if direction == "long" else flag_low
    invalidation = flag_low if direction == "long" else flag_high
    risk = abs(entry - invalidation)
    if risk <= 0 or risk > cfg.r_max * atr_1h:
        return None

    target = entry + risk * cfg.target_r if direction == "long" else entry - risk * cfg.target_r
    entry_type = "breakout_above" if direction == "long" else "breakout_below"

    ltf, stack = zone_stack_and_ltf_scores(local_zones, entry, atr_1h, direction)
    flag_comp = clamp01(1.0 - (flag_range / (cfg.k * atr_1h)))
    edge_prox = proximity_score(edge_dist / atr_1h, same=cfg.e * 0.33, near=cfg.e)
    # trend quality: how far above t_min
    trend_q = clamp01((trend_norm - cfg.t_min) / max(cfg.t_min * 2.0, 1e-9) + 0.35)
    retrace_q = clamp01(1.0 - (retrace_frac / max(cfg.retr_max, 1e-9)))
    acceptance = _acceptance_soft(bars_15m, flag_high, flag_low, direction)
    participation = _participation(bars_15m, bars_1h, cfg.n)
    rs = _relative_strength(bars_15m, btc_15m, max(cfg.n * 4, 32))
    funding = _funding_neutral(bars_15m)
    ext_pen = clamp01(ext / max(cfg.x_max, 1e-9)) if ext > 0 else 0.0
    candle_q = _candle_quality(ref_open, ref_close, ref_high, ref_low, direction)
    contradiction = 0.15 if s_bias == "missing" or z_bias == "missing" else 0.0

    vp_prox = 0.0
    if feature_extras and feature_extras.get("vp_proximity") is not None:
        vp_prox = clamp01(float(feature_extras["vp_proximity"]))

    components = {
        "ltf_inside_htf": ltf,
        "zone_stack_tightness": stack,
        "vp_proximity": vp_prox,
        "flag_compression_quality": flag_comp,
        "edge_proximity": edge_prox,
        "trend_quality": trend_q,
        "retrace_quality": retrace_q,
        "acceptance": acceptance,
        "participation": participation,
        "relative_strength": rs,
        "funding_neutral": funding,
        "extension_penalty": ext_pen,
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
            "weight_profile": cfg.weight_profile,
            "confluence_score": score,
            "confidence_components": weighted,
            "component_raw": {k: round(float(v), 6) for k, v in components.items()},
            "close_15m": round(ref_close, 8),
            "close_1h": round(float(bars_1h["close"][-1]), 8),
            "close_4h": round(float(bars_4h["close"][-1]), 8),
            "atr_1h": round(atr_1h, 8),
            "atr_4h": round(atr_4h, 8),
            "flag_high": round(flag_high, 8),
            "flag_low": round(flag_low, 8),
            "flag_range": round(flag_range, 8),
            "trend_norm_4h": round(trend_norm, 6),
            "retrace_fraction": round(retrace_frac, 6),
            "impulse_size": round(impulse_size, 8),
            "extension_atr": round(ext, 6),
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
    cfg: ContV2Config | None = None,
    snapshot: dict | None = None,
    alpha_db_path: str | Path | None = None,
    outbox_dir: Path | None = None,
) -> list[dict]:
    cfg = cfg or load_config()
    cutoff = cutoff or completed_cycle()
    snapshot = snapshot or {}
    btc = load_btc_15m(conn, cutoff)
    symbols = list_candidate_symbols(conn, cutoff)
    gated: list[dict] = []
    for symbol, asset in symbols:
        bars = load_15m_bars(conn, symbol, cutoff)
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
        cutoff = completed_cycle(now) if now else completed_cycle()
        events = evaluate(conn, cutoff, snapshot=snapshot)
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
