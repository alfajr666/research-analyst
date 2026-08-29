"""Williams fractal plus EMA 20/50/100 scalp alpha plugin."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config
from strategy_v2_context import completed_cycle_for, has_active_event, last_completed_bar_fresh, list_candidate_symbols, load_bars_for_interval


STRATEGY_ID = "williams-fractal-scalp-v1"
SETUP_CLASS = "trend_pullback"
PHASE = "confirmed_fractal_pullback"
PLUGIN_VERSION = "v1"
EXECUTION_INTERVAL = "1m"
ALLOWED_ASSETS = frozenset({"BTC", "ETH", "PAXG", "QQQ"})


@dataclass(frozen=True)
class WilliamsFractalScalpConfig:
    min_bars: int = 130
    fractal_n: int = 2
    target_r: float = 2.0
    horizon_minutes: int = 60


def _ema(values: list[float], span: int) -> list[float]:
    alpha = 2.0 / (span + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1.0 - alpha) * result[-1])
    return result


def evaluate_symbol(bars, *, asset: str, symbol: str, cutoff: datetime, cfg: WilliamsFractalScalpConfig | None = None) -> dict | None:
    """Evaluate the last completed 1m bar without mutating research state."""
    cfg = cfg or WilliamsFractalScalpConfig()
    if asset not in ALLOWED_ASSETS or bars is None or bars.height < max(cfg.min_bars, 2 * cfg.fractal_n + 2):
        return None
    if not last_completed_bar_fresh(bars, cutoff):
        return None

    closes = [float(x) for x in bars["close"].to_list()]
    ema20, ema50, ema100 = (_ema(closes, n) for n in (20, 50, 100))
    center = bars.height - 1 - cfg.fractal_n
    window = bars.slice(center - cfg.fractal_n, 2 * cfg.fractal_n + 1)
    center_low = float(bars["low"][center])
    center_high = float(bars["high"][center])
    bull = center_low == min(float(x) for x in window["low"].to_list())
    bear = center_high == max(float(x) for x in window["high"].to_list())
    long_stack = ema20[-1] > ema50[-1] > ema100[-1] and ema20[-1] > ema20[-2] and ema50[-1] > ema50[-2]
    short_stack = ema20[-1] < ema50[-1] < ema100[-1] and ema20[-1] < ema20[-2] and ema50[-1] < ema50[-2]
    center_close = float(bars["close"][center])
    close = closes[-1]
    if long_stack and bull and center_close > ema100[center] and center_low < ema20[center]:
        stop = ema100[center] if center_low < ema50[center] else ema50[center]
        direction = "long"
    elif short_stack and bear and center_close < ema100[center] and float(bars["high"][center]) > ema20[center]:
        stop = ema100[center] if float(bars["high"][center]) > ema50[center] else ema50[center]
        direction = "short"
    else:
        return None
    risk = close - stop if direction == "long" else stop - close
    if risk <= 0:
        return None
    observed_at = bars["timestamp"][-1]
    if hasattr(observed_at, "to_pydatetime"):
        observed_at = observed_at.to_pydatetime()
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    target = close + cfg.target_r * risk if direction == "long" else close - cfg.target_r * risk
    return {
        "schema_version": 1, "strategy_id": STRATEGY_ID, "asset": asset, "direction": direction,
        "setup_class": SETUP_CLASS, "phase": PHASE, "observed_at": observed_at.isoformat(),
        "valid_until": (observed_at + timedelta(minutes=cfg.horizon_minutes)).isoformat(),
        "horizon_minutes": cfg.horizon_minutes, "confidence": 0.5, "confidence_status": "unscored",
        "entry_condition": {"type": "market", "price": round(close, 8)},
        "invalidation_price": round(stop, 8), "targets": [round(target, 8)], "plugin_version": PLUGIN_VERSION,
        "feature_snapshot": {"source_symbol": symbol, "execution_interval": EXECUTION_INTERVAL,
                              "fractal_n": cfg.fractal_n, "ema20": round(ema20[-1], 8),
                              "ema50": round(ema50[-1], 8), "ema100": round(ema100[-1], 8),
                              "risk": round(risk, 8), "target_r": cfg.target_r},
    }


def evaluate(conn, cutoff: datetime | None = None, *, cfg: WilliamsFractalScalpConfig | None = None,
             snapshot: dict | None = None, alpha_db_path=None, outbox_dir=None, eval_interval: str = EXECUTION_INTERVAL) -> list[dict]:
    cfg = cfg or WilliamsFractalScalpConfig()
    snapshot = snapshot or {}
    cutoff = cutoff or completed_cycle_for(snapshot.get("now"), EXECUTION_INTERVAL)
    events = []
    for symbol, asset in list_candidate_symbols(conn, cutoff):
        if asset not in ALLOWED_ASSETS:
            continue
        bars = load_bars_for_interval(conn, symbol, EXECUTION_INTERVAL, cutoff)
        event = evaluate_symbol(bars, asset=asset, symbol=symbol, cutoff=cutoff, cfg=cfg)
        if event and not has_active_event(STRATEGY_ID, asset, event["direction"], alpha_db_path=alpha_db_path, outbox_dir=outbox_dir, now=cutoff):
            events.append(event)
    return events


def run_plugin(cutoff_id: str, snapshot: dict) -> list[dict]:
    conn = config.get_db_connection(read_only=True, db_path=snapshot.get("market_db_path"))
    try:
        events = evaluate(conn, snapshot=snapshot)
        for event in events:
            event["input_snapshot_id"] = cutoff_id
        return events
    finally:
        conn.close()
