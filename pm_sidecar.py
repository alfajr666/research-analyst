"""LLM position-management sidecar (phase 7, specs/llm-position-sidecar.md).

Emit-only: reads `positions_feed` (executor-owned) + the originating trade-intent +
HTF bias + swings + RR + 5m TA, and emits `hold|exit|reduce` with a one-liner to
`pm_advice`. It never holds credentials, sizes, selects a venue, or places orders.

Disabled by default (`PM_SIDECAR_ENABLED=false`). On any LLM failure/timeout/parse
error it emits `hold` (do no harm).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import config
from strategy_v2_context import (
    atr_last,
    completed_cycle_for,
    ema_last,
    load_bars_for_interval,
    structure_bias_4h,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load_open_positions(conn) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT position_id, symbol, asset, side, entry, size, opened_at,
               strategy_id, current_pnl, status
        FROM positions_feed
        WHERE status = 'open'
        ORDER BY asset, position_id
        """
    ).fetchall()
    cols = ["position_id", "symbol", "asset", "side", "entry", "size",
            "opened_at", "strategy_id", "current_pnl", "status"]
    return [dict(zip(cols, r)) for r in rows]


def _get_active_intent(conn, strategy_id: str, asset: str) -> Optional[Dict[str, Any]]:
    """Latest active trade-intent event for this strategy+asset (the plan to follow)."""
    try:
        row = conn.execute(
            """
            SELECT event_json FROM alpha_events
            WHERE strategy_id = ? AND asset = ?
              AND status IN ('active', 'armed', 'watching')
            ORDER BY observed_at DESC LIMIT 1
            """,
            (strategy_id, asset),
        ).fetchone()
        if not row:
            return None
        ev = json.loads(row[0]) if row[0] else {}
        return {
            "direction": ev.get("direction"),
            "invalidation_price": ev.get("invalidation_price"),
            "targets": ev.get("targets") or ev.get("structure", {}).get("targets"),
            "entry_condition": ev.get("entry_condition"),
        }
    except Exception:
        return None


def _bars_tail(bars, n: int = 60):
    if bars is None or bars.is_empty():
        return None
    return bars.sort("timestamp").tail(n)


def _htf_bias(conn, asset: str, cutoff: datetime) -> Tuple[str, Optional[float]]:
    bars_4h = _bars_tail(load_bars_for_interval(conn, asset, "4h", cutoff))
    if bars_4h is None:
        return "neutral", None
    try:
        return structure_bias_4h(bars_4h), atr_last(bars_4h, 14)
    except Exception:
        return "neutral", None


def _ta_5m(conn, asset: str, cutoff: datetime) -> Dict[str, Any]:
    bars = _bars_tail(load_bars_for_interval(conn, asset, "5m", cutoff))
    if bars is None:
        return {}
    closes = bars["close"].to_list()
    last = closes[-1] if closes else None
    summary = {"last_close": last}
    try:
        summary["ema20"] = ema_last(closes, 20)
    except Exception:
        pass
    try:
        summary["atr14"] = atr_last(bars, 14)
    except Exception:
        pass
    return summary


def _swings(conn, asset: str, cutoff: datetime) -> Dict[str, Any]:
    """HTF swing highs/lows from confirmed pivots on 4h bars."""
    try:
        from market_structure import (
            latest_confirmed_pivot_high,
            latest_confirmed_pivot_low,
        )
        bars = _bars_tail(load_bars_for_interval(conn, asset, "4h", cutoff))
        if bars is None:
            return {}
        idx = len(bars) - 1
        ph = latest_confirmed_pivot_high(bars, idx)
        pl = latest_confirmed_pivot_low(bars, idx)
        return {
            "swing_high": ph.get("price") if ph else None,
            "swing_low": pl.get("price") if pl else None,
        }
    except Exception:
        return {}


def _compute_rr(side: str, entry: float, current: Optional[float],
                invalidation: Optional[float]) -> Optional[float]:
    if not entry or current is None or invalidation is None:
        return None
    if side == "long":
        risk = entry - invalidation
        reward = current - entry
    else:
        risk = invalidation - entry
        reward = entry - current
    if risk <= 0:
        return None
    return round(reward / risk, 2)


def _build_prompt(pos: Dict[str, Any], intent, htf_bias, ta, swings, rr) -> str:
    ctx = {
        "position": {
            "symbol": pos["symbol"], "side": pos["side"], "entry": pos["entry"],
            "current_pnl": pos.get("current_pnl"),
        },
        "strategy_intent": intent or {},
        "htf_bias": htf_bias,
        "rr": rr,
        "ta_5m": ta,
        "swings_4h": swings,
    }
    return (
        "You manage an already-open position and must follow the strategy's plan. "
        "Decide one action: hold, exit, or reduce. exit = close the whole position; "
        "reduce = cut part of it. Prefer hold unless structure invalidates the plan "
        "or RR has badly degraded. "
        "Respond with strict JSON only: "
        '{"action": "hold|exit|reduce", "reason": "<=120 chars"}\n\n'
        f"CONTEXT:\n{json.dumps(ctx, default=str)}"
    )


def call_pm_llm(prompt: str) -> Optional[Dict[str, str]]:
    """Call the configured LLM. Returns {action, reason} or None on any failure."""
    api_key = getattr(config, "LLM_API_KEY", "") or os.getenv("LLM_API_KEY", "")
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except Exception:
        return None
    model = getattr(config, "LLM_MODEL", "") or os.getenv("LLM_MODEL", "") or "gpt-4o-mini"
    retries = max(0, getattr(config, "PM_LLM_RETRIES", 1))
    timeout = getattr(config, "PM_LLM_TIMEOUT_S", 20)
    for _ in range(retries + 1):
        try:
            client = OpenAI(api_key=api_key)
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a disciplined position manager. Reply with JSON only."},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                timeout=timeout,
            )
            data = json.loads(resp.choices[0].message.content)
            action = str(data.get("action", "hold")).lower()
            if action not in ("hold", "exit", "reduce"):
                action = "hold"
            reason = str(data.get("reason", ""))[: getattr(config, "PM_REASON_MAX_CHARS", 120)]
            return {"action": action, "reason": reason}
        except Exception:
            continue
    return None


def _emit_advice(conn, pos, action, reason, htf_bias, rr, cutoff, observed_at) -> bool:
    advice_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{pos['position_id']}|{cutoff.isoformat()}"))
    try:
        if conn.execute("SELECT 1 FROM pm_advice WHERE advice_id = ?", (advice_id,)).fetchone():
            return False  # already advised this position at this cutoff
        conn.execute(
            """
            INSERT INTO pm_advice
                (advice_id, position_id, strategy_id, asset, action, reason,
                 htf_bias, rr, cutoff_at, observed_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (advice_id, pos["position_id"], pos["strategy_id"], pos["asset"], action,
             reason, htf_bias, rr, cutoff, observed_at, observed_at),
        )
        conn.commit()
        return True
    except Exception:
        return False


def run_once(db_path: str | None = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """One PM sidecar pass. No-op when disabled or no open positions.

    Cadence is enforced by the 5m cutoff + deterministic advice_id (one advice per
    position per cutoff). Safe to call every tick.
    """
    if not getattr(config, "PM_SIDECAR_ENABLED", False):
        return {"enabled": False, "advices": 0}
    now = now or _utcnow()
    cutoff = completed_cycle_for(now, f"{getattr(config, 'PM_CADENCE_MINUTES', 5)}m")
    conn = config.get_db_connection(read_only=False, db_path=db_path)
    try:
        positions = _load_open_positions(conn)
        advices = 0
        for pos in positions:
            asset = pos["asset"]
            intent = _get_active_intent(conn, pos["strategy_id"], asset)
            htf_bias, _ = _htf_bias(conn, asset, cutoff)
            ta = _ta_5m(conn, asset, cutoff)
            swings = _swings(conn, asset, cutoff)
            rr = _compute_rr(
                pos["side"], pos["entry"], ta.get("last_close"),
                (intent or {}).get("invalidation_price"),
            )
            prompt = _build_prompt(pos, intent, htf_bias, ta, swings, rr)
            decision = call_pm_llm(prompt)
            if decision is None:
                action, reason = "hold", "llm unavailable/error; defaulting to hold"
            else:
                action, reason = decision["action"], decision["reason"]
            if _emit_advice(conn, pos, action, reason, htf_bias, rr, cutoff, now):
                advices += 1
        return {"enabled": True, "positions": len(positions), "advices": advices}
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else None
    print(json.dumps(run_once(db), default=str))
