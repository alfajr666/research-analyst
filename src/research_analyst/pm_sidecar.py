"""LLM position-management sidecar (specs/llm-position-sidecar.md).

Emit-only: reads open positions (bybit-executor 1m snapshots via
EXECUTOR_SNAPSHOT_DIR, or the local positions_feed table as legacy fallback) + the
originating trade-intent + HTF bias + swings + RR + 5m TA, and emits
`hold|exit|reduce|near_tp` with a one-liner to `pm_advice`. When
EXECUTOR_DECISION_DIR is set, each advice is also exported as a PMDecision JSON
file the executor consumes (HOLD/REDUCE/EXIT/NEAR_TP). It never holds credentials,
sizes, selects a venue, or places orders.

Enabled by default (`PM_SIDECAR_ENABLED=true`). On any LLM failure/timeout/parse
error it emits `hold` (do no harm).
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from strategy_v2_context import (
    atr_last,
    completed_cycle_for,
    ema_last,
    load_bars_for_interval,
    structure_bias_4h,
)


class InvalidPMResponseError(ValueError):
    """The model response was not a usable PM decision object."""


UNMANAGED_STRATEGY_ID = "unmanaged"


def _log_event(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def _classify_llm_error(exc: Exception) -> Tuple[str, Optional[int]]:
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__.lower()
    if status == 429 or "ratelimit" in name:
        return "rate_limit", status
    if status is not None and status >= 500:
        return "server_error", status
    if "timeout" in name or isinstance(exc, TimeoutError):
        return "timeout", status
    if isinstance(exc, (InvalidPMResponseError, json.JSONDecodeError, UnicodeDecodeError)):
        return "invalid_response", status
    if "connection" in name or "connect" in str(exc).lower():
        return "connection_error", status
    if status is not None:
        return "client_error", status
    return "unknown", status


def _parse_pm_response(content: Any) -> Dict[str, Any]:
    if not isinstance(content, str):
        raise InvalidPMResponseError("response content is not text")
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            start = text.index("{")
            data, _ = json.JSONDecoder().raw_decode(text[start:])
        except (ValueError, json.JSONDecodeError) as exc:
            raise InvalidPMResponseError("response is not valid JSON") from exc
    if not isinstance(data, dict):
        raise InvalidPMResponseError("response is not a JSON object")
    proposed_action = str(data.get("action", "hold")).strip().lower()
    action = proposed_action
    proposed_confidence = data.get("confidence")
    confidence = None
    normalization_reason = None
    try:
        if proposed_confidence is not None:
            confidence = float(proposed_confidence)
    except (TypeError, ValueError):
        normalization_reason = "confidence is not numeric"
    if action not in ("hold", "exit", "reduce", "near_tp"):
        normalization_reason = "unknown action"
        action = "hold"
    elif action != "hold" and (
        confidence is None
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
        or confidence < float(getattr(config, "PM_ACTION_CONFIDENCE", 0.70))
    ):
        normalization_reason = normalization_reason or "action confidence below threshold"
        action = "hold"
    reason = str(data.get("reason", ""))[: getattr(config, "PM_REASON_MAX_CHARS", 120)]
    return {
        "action": action,
        "reason": reason,
        "confidence": confidence,
        "proposed_action": proposed_action,
        "proposed_confidence": confidence,
        "normalization_reason": normalization_reason,
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load_open_positions(conn) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT position_id, symbol, asset, side, entry, size, opened_at,
               strategy_id, current_pnl, stop_loss, status
        FROM positions_feed
        WHERE status = 'open'
        ORDER BY asset, position_id
        """
    ).fetchall()
    cols = ["position_id", "symbol", "asset", "side", "entry", "size",
            "opened_at", "strategy_id", "current_pnl", "stop_loss", "status"]
    out = [dict(zip(cols, r)) for r in rows]
    # Carry the default profile so the decision writer knows where to deliver.
    for p in out:
        p.setdefault("exchange_id", getattr(config, "INTENT_EXCHANGE_ID", "bybit"))
        p.setdefault("account_id", getattr(config, "INTENT_ACCOUNT_ID", "hyro"))
    return out


def _load_open_positions_from_snapshots(snapshot_dir, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Read open positions from bybit-executor 1m snapshots.

    Expects ``<snapshot_dir>/<exchange_id>/<account_id>/latest.json`` whose
    ``positions`` array carries the executor's position rows (keys: symbol, side,
    status, position_id, quantity, entry_price, original_json). Only fresh OPEN
    rows are eligible. The originating
    trade-intent lives in ``original_json`` (which holds strategy_id + asset).
    """
    base = Path(snapshot_dir or "")
    if not base.exists():
        return []
    out: List[Dict[str, Any]] = []
    for latest in base.glob("*/*/latest.json"):
        exchange_id = latest.parent.parent.name
        account_id = latest.parent.name
        try:
            data = json.loads(latest.read_text(encoding="utf-8"))
        except Exception:
            continue
        if now is not None:
            try:
                snapshot_at = datetime.fromisoformat(
                    str(data["timestamp"]).replace("Z", "+00:00")
                )
                if snapshot_at.tzinfo is None:
                    snapshot_at = snapshot_at.replace(tzinfo=timezone.utc)
                age = (now.astimezone(timezone.utc) - snapshot_at.astimezone(timezone.utc)).total_seconds()
                if age < 0 or age > float(getattr(config, "DATA_FRESHNESS_MAX_SECONDS", 600)):
                    continue
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        for p in data.get("positions", []):
            if str(p.get("status")) not in ("OPEN", "open"):
                continue
            original: Dict[str, Any] = {}
            try:
                original = json.loads(p.get("original_json") or "{}")
            except Exception:
                original = {}
            asset = original.get("asset")
            if not asset and p.get("symbol"):
                asset = str(p["symbol"]).split("/")[0]
            out.append({
                "position_id": p.get("position_id"),
                "symbol": p.get("symbol"),
                "asset": asset,
                "side": p.get("side"),
                "entry": p.get("entry_price"),
                "stop_loss": p.get("stop_loss"),
                "size": p.get("quantity"),
                "opened_at": p.get("updated_at"),
                "strategy_id": original.get("strategy_id") or (original.get("metadata") or {}).get("strategy_id") or UNMANAGED_STRATEGY_ID,
                "current_pnl": None,
                "status": p.get("status"),
                "exchange_id": exchange_id,
                "account_id": account_id,
            })
    return out


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
            "candidate_id": ev.get("candidate_id"),
            "direction": ev.get("direction"),
            "entry_price": ev.get("entry_price") or (ev.get("entry_condition") or {}).get("price"),
            "invalidation_price": ev.get("invalidation_price"),
            "targets": ev.get("targets") or (ev.get("structure") or {}).get("targets"),
            "entry_condition": ev.get("entry_condition"),
            "atr14_4h": ev.get("atr14_4h"),
            "structural_reference": ev.get("structural_reference"),
            "metadata": ev.get("metadata") or {},
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
    try:
        rsi_len, stoch_len = 14, 14
        rsi = []
        for i in range(len(closes)):
            if i < rsi_len:
                rsi.append(None)
                continue
            gains = [max(closes[j] - closes[j - 1], 0.0) for j in range(i - rsi_len + 1, i + 1)]
            losses = [max(closes[j - 1] - closes[j], 0.0) for j in range(i - rsi_len + 1, i + 1)]
            avg_loss = sum(losses) / rsi_len
            rsi.append(100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + sum(gains) / rsi_len / avg_loss))
        raw = []
        for i, value in enumerate(rsi):
            window = [x for x in rsi[max(0, i - stoch_len + 1):i + 1] if x is not None]
            if value is None or len(window) < stoch_len:
                raw.append(None)
            else:
                lo, hi = min(window), max(window)
                raw.append(0.0 if hi == lo else 100.0 * (value - lo) / (hi - lo))
        k_values = [sum(raw[i - 2:i + 1]) / 3 for i in range(len(raw)) if i >= 2 and all(x is not None for x in raw[i - 2:i + 1])]
        summary["rsi5"] = rsi[-1]
        summary["stoch_k"] = k_values[-1] if k_values else None
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
        "Use only the supplied position, strategy, market-structure, TA, and RR evidence. "
        "Do not invent missing data or use a generic preference for hold. "
        "Decide one action: hold, exit, reduce, or near_tp. exit = close the whole "
        "position; reduce = cut part of it; near_tp = preserve the runner and let "
        "the executor reduce once near the original target. Hold only when the evidence "
        "supports keeping the plan; exit or reduce when the plan is invalidated or RR "
        "has materially degraded. Include confidence from 0 to 1. "
        "Respond with strict JSON only: "
        '{"action": "hold|exit|reduce|near_tp", "confidence": 0.0, '
        '"reason": "<=120 chars"}\n\n'
        f"CONTEXT:\n{json.dumps(ctx, default=str)}"
    )


def call_pm_llm(prompt: str, *, request_id: Optional[str] = None,
                cycle_id: Optional[str] = None, symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Call the configured LLM. Returns a normalized decision or None on failure."""
    request_id = request_id or str(uuid.uuid4())
    api_key = getattr(config, "LLM_API_KEY", "") or os.getenv("LLM_API_KEY", "")
    if not api_key:
        _log_event("llm_request_skipped", request_id=request_id, cycle_id=cycle_id,
                   symbol=symbol, error_class="missing_api_key")
        return None
    try:
        from openai import OpenAI
    except Exception:
        return None
    model = getattr(config, "LLM_MODEL", "") or os.getenv("LLM_MODEL", "") or "gpt-4o-mini"
    retries = max(0, getattr(config, "PM_LLM_RETRIES", 1))
    timeout = getattr(config, "PM_LLM_TIMEOUT_S", 20)
    base_url = getattr(config, "LLM_BASE_URL", "") or None
    for attempt in range(1, retries + 2):
        started = time.monotonic()
        try:
            client = OpenAI(api_key=api_key, base_url=base_url)
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a disciplined position manager. Reply with JSON only."},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                timeout=timeout,
            )
            decision = _parse_pm_response(resp.choices[0].message.content)
            _log_event("llm_request_succeeded", request_id=request_id, cycle_id=cycle_id,
                       symbol=symbol, model=model, attempt=attempt,
                       duration_ms=round((time.monotonic() - started) * 1000),
                       action=decision["action"])
            return decision
        except Exception as exc:
            error = _classify_llm_error(exc)
            _log_event("llm_request_failed", request_id=request_id, cycle_id=cycle_id,
                       symbol=symbol, model=model, attempt=attempt,
                       duration_ms=round((time.monotonic() - started) * 1000),
                       error_class=error[0], http_status=error[1],
                       retryable=attempt <= retries)
            continue
    return None


def _emit_advice(conn, pos, decision, htf_bias, rr, cutoff, observed_at) -> bool:
    action = decision["action"]
    advice_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{pos['position_id']}|{cutoff.isoformat()}"))
    cutoff_text = cutoff.isoformat()
    observed_text = observed_at.isoformat()
    try:
        if conn.execute("SELECT 1 FROM pm_advice WHERE advice_id = ?", (advice_id,)).fetchone():
            return False  # already advised this position at this cutoff
        conn.execute(
            """
            INSERT INTO pm_advice
                (advice_id, position_id, strategy_id, asset, action, reason,
                 htf_bias, rr, confidence, proposed_action, proposed_confidence,
                 normalization_reason, cutoff_at, observed_at, created_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (advice_id, pos["position_id"], pos["strategy_id"], pos["asset"], action,
             decision.get("reason", ""), htf_bias, rr, decision.get("confidence"),
             decision.get("proposed_action"), decision.get("proposed_confidence"),
             decision.get("normalization_reason"), cutoff_text, observed_text, observed_text),
        )
        conn.commit()
        return True
    except Exception:
        return False


def _write_decision_file(pos: Dict[str, Any], decision: Dict[str, Any],
                         cutoff: datetime, observed_at: datetime) -> bool:
    """Export an advice as a PMDecision file for bybit-executor to consume.

    Writes ``<EXECUTOR_DECISION_DIR>/<decision_id>.json`` matching the executor's
    PMDecision contract (HOLD/REDUCE/EXIT/NEAR_TP). No-op when EXECUTOR_DECISION_DIR is
    unset, so legacy (DB-only) operation is unaffected.
    """
    decision_dir = getattr(config, "EXECUTOR_DECISION_DIR", "") or ""
    if not decision_dir:
        return False
    decision_dir = Path(decision_dir)
    decision_dir.mkdir(parents=True, exist_ok=True)
    action_up = str(decision.get("action", "hold")).upper()
    if action_up not in ("HOLD", "EXIT", "REDUCE", "NEAR_TP", "UPDATE_STOP"):
        action_up = "HOLD"
    fraction = None
    if action_up == "REDUCE":
        fraction = float(getattr(config, "PM_REDUCE_FRACTION", 0.5))
    elif action_up == "NEAR_TP":
        fraction = float(getattr(config, "PM_NEAR_TP_REDUCE_FRACTION", 0.75))
    validity = int(getattr(config, "PM_DECISION_VALIDITY_MINUTES", 5))
    valid_until = observed_at + timedelta(minutes=validity)
    decision_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"{pos.get('position_id')}|{cutoff.isoformat()}"))
    payload = {
        "schema_version": 1,
        "decision_id": decision_id,
        "exchange_id": pos.get("exchange_id") or getattr(config, "INTENT_EXCHANGE_ID", "bybit"),
        "account_id": pos.get("account_id") or getattr(config, "INTENT_ACCOUNT_ID", "hyro"),
        "position_id": pos.get("position_id") or "",
        "symbol": pos.get("symbol"),
        "action": action_up,
        "decision_scope": "NEAR_TP" if action_up == "NEAR_TP" else ("MECHANICAL" if action_up == "UPDATE_STOP" else "PM"),
        "confidence": decision.get("confidence"),
        "confidence_threshold": float(getattr(config, "PM_ACTION_CONFIDENCE", 0.70)),
        "reduce_fraction": fraction,
        "issued_at": observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "valid_until": valid_until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": decision.get("reason", ""),
        "controller": decision.get("controller") or "llm_sidecar",
        "stop_loss": decision.get("stop_loss"),
    }
    dest = decision_dir / f"{decision_id}.json"
    try:
        temporary = dest.with_name(f".{dest.name}.tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, dest)
        return True
    except Exception:
        try:
            temporary.unlink()
        except (UnboundLocalError, FileNotFoundError):
            pass
        return False


def _mechanical_strategy_decision(pos: Dict[str, Any], intent: Optional[Dict[str, Any]],
                                  market_conn, cutoff: datetime) -> Optional[Dict[str, Any]]:
    """Evaluate deterministic strategy management without consulting the LLM."""
    if pos.get("strategy_id") != getattr(config, "EMA99_RETEST_STRATEGY_ID", ""):
        return None
    from strategies.v2.ema99_retest_adx_fundamo_v1 import evaluate_exit, evaluate_stop_revision

    bars = load_bars_for_interval(market_conn, pos["asset"], "5m", cutoff)
    exit_signal = evaluate_exit(bars, side=pos["side"], cutoff=cutoff)
    if exit_signal:
        return {
            "action": "exit", "confidence": 1.0, "controller": "mechanical_strategy",
            "reason": exit_signal["rule_name"], "metadata": {"signal": exit_signal},
        }
    metadata = intent.get("metadata") if intent else {}
    trigger = metadata.get("trigger_extreme") or (metadata.get("feature_snapshot") or {}).get("trigger_extreme")
    if trigger is None:
        return None
    revision = evaluate_stop_revision(
        bars, side=pos["side"], trigger_extreme=float(trigger), cutoff=cutoff,
    )
    if not revision:
        return None
    current = pos.get("stop_loss")
    if current is not None:
        favorable = revision["stop_loss"] > float(current) if pos["side"] == "long" else revision["stop_loss"] < float(current)
        if not favorable:
            return None
    return {
        "action": "update_stop", "stop_loss": revision["stop_loss"], "confidence": 1.0,
        "controller": "mechanical_strategy", "reason": revision["rule_name"],
        "metadata": {"signal": revision},
    }


def run_once(db_path: str | None = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """One PM sidecar pass. No-op when disabled or no open positions.

    Positions are read from bybit-executor 1m snapshots when EXECUTOR_SNAPSHOT_DIR
    is set, otherwise from the local positions_feed table (legacy). Each emitted
    advice is written to pm_advice and, when EXECUTOR_DECISION_DIR is set, exported
    as a PMDecision file the executor consumes.

    Cadence is enforced by the 5m cutoff + deterministic advice_id (one advice per
    position per cutoff). Safe to call every tick.
    """
    if not getattr(config, "PM_SIDECAR_ENABLED", False):
        return {"enabled": False, "advices": 0}
    now = now or _utcnow()
    cutoff = completed_cycle_for(now, f"{getattr(config, 'PM_CADENCE_MINUTES', 5)}m")
    conn = config.get_db_connection(read_only=False, db_path=db_path)
    market_conn = config.get_db_connection(read_only=True, db_path=config.MARKET_DB_PATH)
    try:
        positions: List[Dict[str, Any]] = []
        snapshot_dir = getattr(config, "EXECUTOR_SNAPSHOT_DIR", "") or ""
        if snapshot_dir:
            positions = _load_open_positions_from_snapshots(snapshot_dir, now=now)
        else:
            positions = _load_open_positions(conn)
        cycle_id = cutoff.strftime("%Y%m%dT%H%MZ")
        _log_event("pm_cycle_positions", cycle_id=cycle_id,
                   position_count=len(positions),
                   symbols=[p.get("symbol") or p.get("asset") for p in positions])
        advices = 0
        written = 0
        for pos in positions:
            asset = pos["asset"]
            if pos.get("strategy_id") == UNMANAGED_STRATEGY_ID:
                reason = "unmanaged position; no originating intent for PM analysis"
                decision = {
                    "action": "hold",
                    "reason": reason,
                    "confidence": None,
                    "proposed_action": "hold",
                    "proposed_confidence": None,
                    "normalization_reason": None,
                }
                _log_event("pm_unmanaged_position_hold",
                           strategy_id=UNMANAGED_STRATEGY_ID, asset=asset,
                           position_id=pos.get("position_id"), reason=reason,
                           cutoff=cutoff.isoformat())
                if _emit_advice(conn, pos, decision, None, None, cutoff, now):
                    advices += 1
                    if _write_decision_file(pos, decision, cutoff, now):
                        written += 1
                continue
            intent = _get_active_intent(conn, pos["strategy_id"], asset)
            mechanical = _mechanical_strategy_decision(pos, intent, market_conn, cutoff)
            if mechanical is not None:
                if _emit_advice(conn, pos, mechanical, None, None, cutoff, now):
                    advices += 1
                    if _write_decision_file(pos, mechanical, cutoff, now):
                        written += 1
                continue
            htf_bias, _ = _htf_bias(market_conn, asset, cutoff)
            ta = _ta_5m(market_conn, asset, cutoff)
            swings = _swings(market_conn, asset, cutoff)
            rr = _compute_rr(
                pos["side"], pos["entry"], ta.get("last_close"),
                (intent or {}).get("invalidation_price"),
            )
            prompt = _build_prompt(pos, intent, htf_bias, ta, swings, rr)
            request_id = str(uuid.uuid4())
            decision = call_pm_llm(prompt, request_id=request_id,
                                   cycle_id=cycle_id, symbol=pos.get("symbol") or asset)
            if decision is None:
                decision = {
                    "action": "hold",
                    "reason": "LLM unavailable/error; safety fallback HOLD, not a market judgment",
                    "confidence": None,
                    "proposed_action": None,
                    "proposed_confidence": None,
                    "normalization_reason": "llm unavailable or invalid response",
                }
            else:
                decision = dict(decision)
            _log_event("llm_management_decision", request_id=request_id,
                       strategy_id=pos.get("strategy_id"), asset=asset,
                       position_id=pos.get("position_id"),
                       action=decision["action"], confidence=decision.get("confidence"),
                       proposed_action=decision.get("proposed_action"),
                       reason=decision["reason"], cutoff=cutoff.isoformat())
            if _emit_advice(conn, pos, decision, htf_bias, rr, cutoff, now):
                advices += 1
                if _write_decision_file(pos, decision, cutoff, now):
                    written += 1
        return {"enabled": True, "positions": len(positions),
                "advices": advices, "decisions_written": written}
    finally:
        market_conn.close()
        conn.close()


if __name__ == "__main__":
    import sys
    if "--once" in sys.argv:
        db = next((arg for arg in sys.argv[1:] if arg != "--once"), None)
        print(json.dumps(run_once(db), default=str))
    else:
        interval_seconds = max(60, int(getattr(config, "PM_CADENCE_MINUTES", 5)) * 60)
        print(f"Starting independent PM sidecar at {interval_seconds}s cadence...", flush=True)
        while True:
            try:
                print(json.dumps(run_once(config.ANALYST_DB_PATH), default=str), flush=True)
            except Exception as exc:
                print(f"PM sidecar err: {exc}", file=sys.stderr, flush=True)
            time.sleep(interval_seconds)
