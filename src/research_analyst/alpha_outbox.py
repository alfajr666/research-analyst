"""Append-only, file-backed delivery seam for venue-neutral alpha events."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import config


OUTBOX_DIR = config.DEFAULT_DB_DIR / "alpha_outbox"


def dedupe_key(event: dict) -> str:
    """Return stable identity for one strategy observation and input snapshot."""
    observed_at = event["observed_at"]
    if hasattr(observed_at, "isoformat"):
        observed_at = observed_at.isoformat()
    material = "|".join((
        str(event["strategy_id"]), str(event.get("plugin_version", "")),
        str(event["asset"]), str(event["direction"]), str(observed_at),
        str(event.get("input_snapshot_id", event.get("cutoff_id", ""))),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def write_event(event: dict, outbox_dir: Path = OUTBOX_DIR) -> tuple[bool, Path]:
    """Atomically append an event, returning whether it was newly written.

    Enforces emit gate per ca-truth-venue-agg-failover.md:
    - MIXED strategies require data_purity == 'pure_ca' else refuse (log)
    - PRICE_STRUCTURE allowed with stamp
    """
    # Capture before any purity/admission gate; failures are deliberately isolated.
    from raw_signal_batch import capture
    from trade_admission import admit
    from raw_signal_batch import record_status
    raw_id = capture(event)
    admission_event = dict(event)
    admission_event.setdefault("candidate_id", event.get("alpha_id") or event.get("dedupe_key"))
    if not admission_event.get("valid_until") and event.get("observed_at"):
        from datetime import datetime, timedelta, timezone
        observed = event["observed_at"]
        if isinstance(observed, str):
            observed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        admission_event["valid_until"] = observed + timedelta(minutes=getattr(config, "INTENT_VALIDITY_MINUTES", 5))
    # Older advisory alpha envelopes omit expiry; executor construction supplies
    # the configured validity. New strategy candidates must provide it explicitly.
    if not event.get("valid_until"):
        admission_event["valid_until"] = datetime.now(timezone.utc) + timedelta(minutes=1)
    if not admission_event.get("targets"):
        target = admission_event.get("take_profit")
        if target is None:
            from trade_admission import derive_2r_target
            entry = admission_event.get("entry_price")
            if entry is None:
                entry = (admission_event.get("entry_condition") or {}).get("price")
            target = derive_2r_target(
                admission_event.get("direction"),
                entry,
                admission_event.get("invalidation_price", admission_event.get("stop_loss")),
            )
        if target is not None:
            admission_event["take_profit"] = target
            admission_event["targets"] = [target]
    admission = event.get("_admission_result") or admit(admission_event)
    complete_candidate = (admission_event.get("entry_price") is not None or
                          (admission_event.get("entry_condition") or {}).get("price") is not None) and \
        admission_event.get("invalidation_price", admission_event.get("stop_loss")) is not None and \
        bool(admission_event.get("targets") or admission_event.get("take_profit")) and bool(event.get("valid_until"))
    if raw_id and complete_candidate:
        record_status(raw_id, hard_gate_status=admission["hard_gate"],
                      reason="; ".join(admission["hard_gate_reasons"]))
    if admission.get("symbol_account_gate") == "fail":
        if raw_id:
            record_status(
                raw_id,
                hard_gate_status="fail",
                score_status="not_evaluated",
                clash_status="not_evaluated",
                executor_intent_status="rejected",
                reason=(
                    f"{admission.get('rejection_reason')}; canonical_asset={admission.get('canonical_asset')}; "
                    f"resolved_account={admission.get('resolved_account')}; policy_version={admission.get('policy_version')}"
                ),
            )
        print(f"write_event blocked by symbol-account policy: {admission.get('rejection_reason')}")
        return False, outbox_dir / "blocked.json"
    sid = event.get("strategy_id", "")
    dp = event.get("data_purity", "pure_ca")
    MIXED = getattr(config, "MIXED_STRATEGY_IDS", set())
    PRICE = getattr(config, "PRICE_STRUCTURE_STRATEGY_IDS", set())
    # "pure" sources (coinalyze, websocket feeds) pass; failover (venue_agg_v1) is
    # intentionally non-pure and must not produce deterministic events.
    is_pure = str(dp).startswith("pure_")
    if sid in MIXED and not is_pure:
        print(f"write_event blocked: mixed {sid} on non-pure {dp}")
        # still "write" metadata? no: refuse
        return False, outbox_dir / "blocked.json"
    if complete_candidate and admission["hard_gate"] != "pass":
        print(f"write_event blocked by hard admission: {admission['hard_gate_reasons']}")
        return False, outbox_dir / "blocked.json"
    if sid not in (MIXED | PRICE) and not is_pure:
        # unknown -> fail closed
        print(f"write_event blocked: unknown {sid} on non-pure {dp}")
        return False, outbox_dir / "blocked.json"

    key = dedupe_key(event)
    outbox_dir.mkdir(parents=True, exist_ok=True)
    destination = outbox_dir / f"{key}.json"
    payload = dict(event)
    if not payload.get("targets") and admission_event.get("targets"):
        payload["targets"] = admission_event["targets"]
    if not payload.get("valid_until") and admission_event.get("valid_until"):
        payload["valid_until"] = admission_event["valid_until"]
    payload["alpha_id"] = str(uuid5(NAMESPACE_URL, key))
    payload["dedupe_key"] = key
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str) + "\n"

    fd, temporary = tempfile.mkstemp(prefix=".alpha-", suffix=".tmp", dir=outbox_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            try:
                existing = json.loads(destination.read_text(encoding="utf-8"))
                _maybe_deliver_intent(existing)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(f"duplicate alpha outbox event unreadable: {exc}")
            return False, destination
        _maybe_deliver_intent(payload)
        if raw_id:
            record_status(raw_id, executor_intent_status="written")
        return True, destination
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _maybe_deliver_intent(payload: dict) -> None:
    """Best-effort: emit an executor TradeIntent envelope for a newly written event.

    Gated by INTENT_DELIVERY_ENABLED; geometry-invalid events are skipped (the
    advisory alpha event is still emitted). Failures are logged, never raised.
    """
    if not getattr(config, "INTENT_DELIVERY_ENABLED", False):
        return
    try:
        from intent_outbox import build_executor_intent, validate_geometry, write_intent
        intent = build_executor_intent(payload)
        ok, reason = validate_geometry(intent)
        if not ok:
            print(f"intent skipped (geometry): {reason} for {payload.get('strategy_id')}/{payload.get('asset')}")
            return
        # Legacy filesystem inbox (kept during rollout unless disabled).
        if getattr(config, "INTENT_BUS_LEGACY_INBOX_ENABLED", False):
            write_intent(intent, config.INTENT_INBOX)
        # Shared SQLite intent bus fan-out (spec 3.2, 7).
        _maybe_publish_to_bus(intent)
    except Exception as exc:  # never break the advisory emit path
        print(f"intent delivery error: {exc}")


def _maybe_publish_to_bus(intent: dict) -> None:
    """Best-effort fan-out of a built schema-v1 envelope to the shared bus."""
    try:
        from intent_bus_publisher import publish_research_intent
        for target in ("bybit", "propr"):
            ok, delivery_id, err = publish_research_intent(intent, target=target)
            if not ok and err is not None:
                print(f"intent bus {target} publish failed: {err} for {intent.get('delivery_id')}")
            elif ok:
                print(f"intent bus published target={target} delivery={delivery_id}")
    except Exception as exc:  # bus fan-out must never break the pipeline
        print(f"intent bus publish error: {exc}")
