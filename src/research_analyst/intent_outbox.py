"""Build and deliver bybit-executor TradeIntent envelopes (schema_version 1).

The internal alpha event (alpha_outbox) is the advisory record consumed by
Discord/signal_publisher. This module converts that event into the envelope the
bybit-executor "Trade Intent Contract" (see bybit-executor/AGENTS.md) expects,
and writes it to INTENT_INBOX. The executor polls that directory, rejects
duplicate `delivery_id`s, and never trusts intent leverage.

Geometry rules mirrored from the contract:
  LONG  -> stop_loss < entry_price < take_profit
  SHORT -> take_profit < entry_price < stop_loss

Entry admission requires the configured minimum reward/risk and stop distance.
The emitted TradeIntent deliberately contains no order-type instruction.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import config

FUNDAMO_STRATEGY_IDS = frozenset((
    "ema99-retest-adx-fundamo-v1",
    "dual-zone-follower-v2",
    "dual-zone-short-follower-v2",
    "ema20-pullback-h4-trend-v1",
    "ema-stack-15m-adx-stochrsi-5m-v1",
    "gold-trend-ema-bb-stoch-v1",
    "mtf-exhaustion-reversal-v1",
    "trend-wall-v1",
    "ema99-double-touch-stochrsi-state-v1",
    "ema7-26-cross-hammer-shooting-star-1h-adx-v1",
))
from trade_admission import (
    canonical_asset,
    candidate_admission_fingerprint,
    derive_2r_target,
    resolved_account,
    admit_symbol_account,
)


def to_ccxt_perp_symbol(asset: str, quote: str = "USDT") -> str:
    """Render a CCXT unified perpetual symbol, e.g. BTC -> BTC/USDT:USDT."""
    return f"{asset.upper()}/{quote}:{quote}"


def _iso(ts) -> str:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(ts)


def _same_timestamp(left, right) -> bool:
    try:
        def parse(value):
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        return parse(left) == parse(right)
    except (TypeError, ValueError, OverflowError):
        return False


def _norm_direction(d: str) -> str:
    d = str(d).strip().lower()
    if d in ("long", "bullish", "buy"):
        return "LONG"
    if d in ("short", "bearish", "sell"):
        return "SHORT"
    return d.upper()


def build_executor_intent(event: dict, *, source=None, exchange_id=None,
                          account_id=None, take_profit_mode=None,
                          validity_minutes=None, admission=None) -> dict:
    """Convert an internal alpha event into a bybit-executor TradeIntent envelope.

    Precedence for routing fields: explicit argument > per-strategy INTENT_ROUTING
    entry > global INTENT_* default. Compact strategies are subsequently forced to
    the deployment profile (bybit/hyro).
    """
    route = (getattr(config, "INTENT_ROUTING", {}) or {}).get(event.get("strategy_id"), {}) or {}
    source = source or route.get("source") or getattr(config, "INTENT_SOURCE", "research-analyst")
    exchange_id = exchange_id or route.get("exchange_id") or getattr(config, "INTENT_EXCHANGE_ID", "bybit")
    account_id = account_id or route.get("account_id") or getattr(config, "INTENT_ACCOUNT_ID", "hyro")
    # Compact strategies and the Fundamo portfolio must never be diverted by
    # stale routing config or caller-supplied overrides.
    if event.get("strategy_id") in getattr(config, "COMPACT_STRATEGY_IDS", ()) or event.get("strategy_id") in FUNDAMO_STRATEGY_IDS:
        exchange_id, account_id = "bybit", resolved_account(event.get("strategy_id"))
    take_profit_mode = take_profit_mode or route.get("take_profit_mode") or getattr(config, "INTENT_TAKE_PROFIT_MODE", "fixed_full_close")
    validity_minutes = (
        validity_minutes if validity_minutes is not None
        else route.get("validity_minutes", getattr(config, "INTENT_VALIDITY_MINUTES", 5))
    )

    asset = event["asset"]
    direction = _norm_direction(event.get("direction", ""))
    observed_at = _iso(event.get("observed_at"))

    valid_until = event.get("valid_until") or event.get("entry_valid_until")
    if valid_until:
        entry_valid_until = _iso(valid_until)
    else:
        base = event.get("observed_at") or datetime.now(timezone.utc)
        if isinstance(base, str):
            try:
                base = datetime.fromisoformat(base.replace("Z", "+00:00"))
            except ValueError:
                base = datetime.now(timezone.utc)
        if getattr(base, "tzinfo", None) is None:
            base = base.replace(tzinfo=timezone.utc)
        entry_valid_until = _iso(base + timedelta(minutes=validity_minutes))

    entry_condition = event.get("entry_condition") or {}
    entry_price = event.get("entry_price") or entry_condition.get("price")

    stop_loss = event.get("invalidation_price", event.get("stop_loss"))
    targets = event.get("targets") or []
    take_profit = targets[0] if targets else event.get("take_profit")
    target_source = "strategy_target" if take_profit is not None else None
    if take_profit is None:
        take_profit = derive_2r_target(direction, entry_price, stop_loss)
        if take_profit is not None:
            target_source = "producer_derived_2r"

    admission = admission or event.get("_admission_result")
    # Sizing is executor-owned: the analyst never dictates quantity/risk_amount.
    # Pass through any non-sizing metadata the strategy attached; the executor
    # sizes from its account profile when no quantity/risk_amount is present.
    meta = {k: v for k, v in (event.get("metadata") or {}).items()
            if k not in ("quantity", "amount", "risk_amount")}
    if event.get("strategy_id"):
        meta.setdefault("strategy_id", event["strategy_id"])
    meta.setdefault("candidate_id", admission.get("candidate_id") if admission else None)
    if not meta.get("candidate_id"):
        meta["candidate_id"] = event.get("candidate_id") or event.get("dedupe_key") or event.get("alpha_id")
    if isinstance(event.get("structural_context"), dict):
        meta["structural_context"] = event["structural_context"]
    if target_source:
        meta.setdefault("target_source", target_source)
    if admission is not None:
        meta["admission_result"] = admission

    delivery_id = (
        event.get("alpha_id") or event.get("dedupe_key")
        or str(uuid5(NAMESPACE_URL, f"{event.get('strategy_id', '')}|{asset}|{direction}|{observed_at}"))
    )

    return {
        "schema_version": 1,
        "delivery_id": delivery_id,
        "source": source,
        "exchange_id": exchange_id,
        "account_id": account_id,
        "asset": asset,
        "symbol": to_ccxt_perp_symbol(asset),
        "direction": direction,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "take_profit_mode": take_profit_mode,
        "setup_class": event.get("setup_class"),
        "phase": event.get("phase"),
        "observed_at": observed_at,
        "entry_valid_until": entry_valid_until,
        "metadata": meta,
    }


def verify_intent_admission(intent: dict, admission: dict | None = None, *, now: datetime | None = None) -> tuple[bool, str]:
    """Verify the immutable admission proof before any intent handoff."""
    proof = admission or (intent.get("metadata") or {}).get("admission_result")
    if not isinstance(proof, dict):
        return False, "admission proof is missing"
    if proof.get("hard_gate") != "pass":
        return False, "admission hard gate did not pass"
    if proof.get("structural_stop_gate") != "pass":
        return False, "structural admission did not pass"
    metadata = intent.get("metadata") or {}
    candidate_id = metadata.get("candidate_id")
    if not candidate_id or proof.get("candidate_id") != candidate_id:
        return False, "admission proof candidate identity is inconsistent"
    candidate = {
        "candidate_id": candidate_id,
        "strategy_id": metadata.get("strategy_id"),
        "asset": intent.get("asset"),
        "direction": intent.get("direction"),
        "entry_price": intent.get("entry_price"),
        "invalidation_price": intent.get("stop_loss"),
        "take_profit": intent.get("take_profit"),
        "observed_at": intent.get("observed_at"),
        "valid_until": intent.get("entry_valid_until"),
        "data_freshness_seconds": proof.get("data_freshness_seconds"),
    }
    if proof.get("candidate_fingerprint") != candidate_admission_fingerprint(candidate):
        return False, "admission proof candidate fingerprint is inconsistent"
    context = metadata.get("structural_context")
    if not isinstance(context, dict):
        return False, "admission structural context is missing"
    from trade_admission import admit
    recomputed = admit(candidate, structural_context=context)
    if recomputed.get("hard_gate") != "pass":
        return False, "admission structural context no longer passes"
    for field in (
        "selected_zone_id", "selected_zone_kind", "selected_zone_asset", "selected_zone_timeframe",
        "selected_zone_state", "selected_zone_created_at", "selected_zone_confirmed_at",
        "selected_zone_coverage_status", "selected_zone_source_evidence_ids",
        "selected_zone_low", "selected_zone_high", "selected_zone_boundary",
        "structural_atr", "structural_atr_period", "structural_atr_method",
        "structural_atr_source_bar_ids", "entry_zone_buffer", "entry_zone_buffer_atr",
        "structural_stop_buffer", "structural_stop_buffer_atr", "structural_context_cutoff",
    ):
        if field in ("selected_zone_low", "selected_zone_high", "selected_zone_boundary", "structural_atr",
                     "entry_zone_buffer", "entry_zone_buffer_atr", "structural_stop_buffer", "structural_stop_buffer_atr"):
            if not math.isclose(float(recomputed.get(field)), float(proof.get(field)), rel_tol=1e-9, abs_tol=1e-9):
                return False, f"admission proof {field} is inconsistent"
        elif field == "structural_context_cutoff":
            if not _same_timestamp(recomputed.get(field), proof.get(field)):
                return False, "admission proof cutoff is inconsistent"
        elif field in ("selected_zone_created_at", "selected_zone_confirmed_at"):
            if not _same_timestamp(recomputed.get(field), proof.get(field)):
                return False, f"admission proof {field} is inconsistent"
        elif recomputed.get(field) != proof.get(field):
            return False, f"admission proof {field} is inconsistent"
    if (
        not proof.get("selected_zone_id")
        or proof.get("selected_zone_timeframe") not in ("1h", "4h")
        or proof.get("structural_atr_method") != "wilder"
        or proof.get("structural_atr_period") != 14
        or not isinstance(proof.get("structural_atr_source_bar_ids"), list)
        or not proof["structural_atr_source_bar_ids"]
        or proof.get("selected_zone_kind") not in ("fvg", "order_block")
        or proof.get("selected_zone_state") not in ("active", "partial")
        or proof.get("selected_zone_coverage_status") != "covered"
        or not isinstance(proof.get("selected_zone_source_evidence_ids"), list)
        or not proof["selected_zone_source_evidence_ids"]
    ):
        return False, "admission proof provenance is incomplete"
    if canonical_asset(proof.get("selected_zone_asset")) != canonical_asset(intent.get("asset")):
        return False, "admission proof asset is inconsistent"
    entry = intent.get("entry_price")
    stop = intent.get("stop_loss")
    direction = intent.get("direction")
    atr = proof.get("structural_atr")
    low = proof.get("selected_zone_low")
    high = proof.get("selected_zone_high")
    boundary = proof.get("selected_zone_boundary")
    generic_atr = proof.get("atr14_4h")
    entry_multiple_recorded = proof.get("entry_zone_buffer_atr")
    stop_multiple_recorded = proof.get("structural_stop_buffer_atr")
    if not all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0 for value in (entry, stop, atr, low, high, boundary, generic_atr)):
        return False, "admission proof geometry is incomplete"
    if not all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0 for value in (entry_multiple_recorded, stop_multiple_recorded)):
        return False, "admission proof ATR multiples are incomplete"
    if low > high:
        return False, "admission proof zone bounds are invalid"
    expected_boundary = low if direction == "LONG" else high if direction == "SHORT" else None
    if expected_boundary is None or not math.isclose(boundary, expected_boundary, rel_tol=1e-9, abs_tol=1e-9):
        return False, "admission proof boundary is inconsistent"
    entry_buffer = entry - high if direction == "LONG" else low - entry if direction == "SHORT" else None
    stop_buffer = low - stop if direction == "LONG" else stop - high if direction == "SHORT" else None
    if entry_buffer is None or stop_buffer is None:
        return False, "admission proof direction is invalid"
    if not math.isclose(entry_buffer, proof.get("entry_zone_buffer"), rel_tol=1e-9, abs_tol=1e-9):
        return False, "admission entry buffer is inconsistent"
    if not math.isclose(stop_buffer, proof.get("structural_stop_buffer"), rel_tol=1e-9, abs_tol=1e-9):
        return False, "admission stop buffer is inconsistent"
    entry_multiple = entry_buffer / atr
    stop_multiple = stop_buffer / atr
    if not math.isclose(entry_multiple, entry_multiple_recorded, rel_tol=1e-9, abs_tol=1e-9):
        return False, "admission entry ATR multiple is inconsistent"
    if not math.isclose(stop_multiple, stop_multiple_recorded, rel_tol=1e-9, abs_tol=1e-9):
        return False, "admission stop ATR multiple is inconsistent"
    min_multiple = float(getattr(config, "STRUCTURAL_STOP_MIN_ATR_MULTIPLE", 0.5))
    max_multiple = float(getattr(config, "STRUCTURAL_STOP_MAX_ATR_MULTIPLE", 3.0))
    if not min_multiple <= entry_multiple <= max_multiple:
        return False, "admission entry buffer is outside policy"
    if not min_multiple <= stop_multiple <= max_multiple:
        return False, "admission stop buffer is outside policy"
    risk = abs(entry - stop)
    min_stop_pct = float(getattr(config, "INTENT_MIN_STOP_DISTANCE_PCT", 0.001))
    atr_floor = float(generic_atr) / entry * float(getattr(config, "INTENT_MIN_STOP_ATR_MULTIPLIER", 0.25))
    if risk / entry < max(min_stop_pct, atr_floor):
        return False, "admission generic ATR stop floor is not satisfied"
    if not proof.get("structural_context_cutoff"):
        return False, "admission proof cutoff is missing"
    if proof.get("structural_context_cutoff"):
        try:
            proof_cutoff = datetime.fromisoformat(str(proof["structural_context_cutoff"]).replace("Z", "+00:00"))
            intent_observed = datetime.fromisoformat(str(intent.get("observed_at")).replace("Z", "+00:00"))
            proof_cutoff = (proof_cutoff if proof_cutoff.tzinfo else proof_cutoff.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
            intent_observed = (intent_observed if intent_observed.tzinfo else intent_observed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
            if proof_cutoff != intent_observed:
                return False, "admission proof cutoff is inconsistent"
        except (TypeError, ValueError, OverflowError):
            return False, "admission proof cutoff is invalid"
    for field in ("selected_zone_created_at", "selected_zone_confirmed_at"):
        try:
            timestamp = datetime.fromisoformat(str(proof[field]).replace("Z", "+00:00"))
            cutoff = datetime.fromisoformat(str(proof["structural_context_cutoff"]).replace("Z", "+00:00"))
            timestamp = (timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
            cutoff = (cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
            if timestamp > cutoff:
                return False, "admission proof zone timestamp is after cutoff"
        except (KeyError, TypeError, ValueError, OverflowError):
            return False, "admission proof zone timestamps are invalid"
    strategy_id = metadata.get("strategy_id")
    symbol_policy = admit_symbol_account({"strategy_id": strategy_id, "asset": intent.get("asset")})
    if symbol_policy["symbol_account_gate"] != "pass":
        return False, "admission proof symbol-account policy is invalid"
    if intent.get("exchange_id") != "bybit" or intent.get("account_id") != symbol_policy["resolved_account"]:
        return False, "intent routing is inconsistent with admission policy"
    freshness = proof.get("data_freshness_seconds")
    if not isinstance(freshness, (int, float)) or not math.isfinite(freshness) or freshness < 0 or freshness > float(getattr(config, "DATA_FRESHNESS_MAX_SECONDS", 600)):
        return False, "admission proof freshness is invalid"
    return True, ""


def validate_intent_handoff(intent: dict, admission: dict | None = None, *, now: datetime | None = None) -> tuple[bool, str]:
    """Validate generic and structural admission at the final handoff seam."""
    ok, reason = verify_intent_admission(intent, admission, now=now)
    if not ok:
        return False, reason
    ok, reason = validate_geometry(intent)
    if not ok:
        return False, reason
    try:
        observed_at = datetime.fromisoformat(str(intent["observed_at"]).replace("Z", "+00:00"))
        valid_until = datetime.fromisoformat(str(intent["entry_valid_until"]).replace("Z", "+00:00"))
        if valid_until <= observed_at:
            return False, "entry validity is not after observation"
        if now is not None and valid_until <= now.astimezone(timezone.utc):
            return False, "entry validity has expired"
    except (KeyError, TypeError, ValueError, OverflowError):
        return False, "intent timestamps are invalid"
    return True, ""


def validate_geometry(intent: dict) -> tuple[bool, str]:
    """Return (ok, reason). Mirrors the executor's geometry acceptance rules."""
    direction = intent.get("direction")
    sl = intent.get("stop_loss")
    tp = intent.get("take_profit")
    ep = intent.get("entry_price")
    if not (isinstance(sl, (int, float)) and sl > 0):
        return False, "stop_loss must be positive"
    if not (isinstance(tp, (int, float)) and tp > 0):
        return False, "take_profit must be positive"
    if direction not in ("LONG", "SHORT"):
        return False, "direction must be LONG or SHORT"
    if ep is None:
        return True, ""  # market entry: geometry relative to entry is not yet known
    if not isinstance(ep, (int, float)) or ep <= 0:
        return False, "entry_price must be positive"
    if direction == "LONG" and not (sl < ep < tp):
        return False, "LONG requires stop_loss < entry_price < take_profit"
    if direction == "SHORT" and not (tp < ep < sl):
        return False, "SHORT requires take_profit < entry_price < stop_loss"
    risk = abs(ep - sl)
    reward = abs(tp - ep)
    rr = reward / risk if risk else 0.0
    min_rr = float(getattr(config, "INTENT_MIN_RR", 2.0))
    if rr < min_rr:
        return False, f"reward/risk {rr:.2f} below minimum {min_rr:.2f}"
    stop_pct = risk / ep
    min_stop = float(getattr(config, "INTENT_MIN_STOP_DISTANCE_PCT", 0.001))
    if stop_pct < min_stop:
        return False, f"stop distance {stop_pct:.4%} below minimum {min_stop:.4%}"
    return True, ""


def write_intent(intent: dict, inbox_dir: Path | None = None, *, admission=None) -> tuple[bool, Path]:
    """Atomically write an intent envelope to the inbox; returns (created, path).

    `delivery_id` is the filename, so re-writing the same intent is idempotent at
    the file level (the executor also dedupes by delivery_id in its journal).
    """
    inbox_dir = Path(inbox_dir if inbox_dir is not None else getattr(config, "INTENT_INBOX", None))
    ok, reason = validate_intent_handoff(intent, admission, now=datetime.now(timezone.utc))
    if not ok:
        return False, inbox_dir / "blocked.json"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    destination = inbox_dir / f"{intent['delivery_id']}.json"
    if destination.exists():
        return False, destination
    serialized = json.dumps(intent, sort_keys=True, separators=(",", ":"), default=str) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".intent-", suffix=".tmp", dir=inbox_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
        return True, destination
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
