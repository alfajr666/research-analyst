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
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import config

FUNDAMO_STRATEGY_IDS = frozenset((
    "dual-zone-follower-v2",
    "dual-zone-short-follower-v2",
    "ema20-pullback-h4-trend-v1",
    "ema-stack-15m-adx-stochrsi-5m-v1",
    "gold-trend-ema-bb-stoch-v1",
    "mtf-exhaustion-reversal-v1",
    "trend-wall-v1",
))
from trade_admission import derive_2r_target, resolved_account


def to_ccxt_perp_symbol(asset: str, quote: str = "USDT") -> str:
    """Render a CCXT unified perpetual symbol, e.g. BTC -> BTC/USDT:USDT."""
    return f"{asset.upper()}/{quote}:{quote}"


def _iso(ts) -> str:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(ts)


def _norm_direction(d: str) -> str:
    d = str(d).strip().lower()
    if d in ("long", "bullish", "buy"):
        return "LONG"
    if d in ("short", "bearish", "sell"):
        return "SHORT"
    return d.upper()


def build_executor_intent(event: dict, *, source=None, exchange_id=None,
                          account_id=None, take_profit_mode=None,
                          validity_minutes=None) -> dict:
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

    # Sizing is executor-owned: the analyst never dictates quantity/risk_amount.
    # Pass through any non-sizing metadata the strategy attached; the executor
    # sizes from its account profile when no quantity/risk_amount is present.
    meta = {k: v for k, v in (event.get("metadata") or {}).items()
            if k not in ("quantity", "amount", "risk_amount")}
    if event.get("strategy_id"):
        meta.setdefault("strategy_id", event["strategy_id"])
    if target_source:
        meta.setdefault("target_source", target_source)

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
        "observed_at": observed_at,
        "entry_valid_until": entry_valid_until,
        "metadata": meta,
    }


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
    max_stop = float(getattr(config, "INTENT_MAX_STOP_DISTANCE_PCT", 0.05))
    if stop_pct < min_stop:
        return False, f"stop distance {stop_pct:.4%} below minimum {min_stop:.4%}"
    if stop_pct > max_stop:
        return False, f"stop distance {stop_pct:.4%} above maximum {max_stop:.4%}"
    return True, ""


def write_intent(intent: dict, inbox_dir: Path | None = None) -> tuple[bool, Path]:
    """Atomically write an intent envelope to the inbox; returns (created, path).

    `delivery_id` is the filename, so re-writing the same intent is idempotent at
    the file level (the executor also dedupes by delivery_id in its journal).
    """
    inbox_dir = Path(inbox_dir if inbox_dir is not None else getattr(config, "INTENT_INBOX", None))
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
