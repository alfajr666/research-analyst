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
    """Return the stable identity for one strategy observation."""
    observed_at = event["observed_at"]
    if hasattr(observed_at, "isoformat"):
        observed_at = observed_at.isoformat()
    material = "|".join((event["strategy_id"], event["asset"], event["direction"], str(observed_at)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def write_event(event: dict, outbox_dir: Path = OUTBOX_DIR) -> tuple[bool, Path]:
    """Atomically append an event, returning whether it was newly written.

    Enforces emit gate per ca-truth-venue-agg-failover.md:
    - MIXED strategies require data_purity == 'pure_ca' else refuse (log)
    - PRICE_STRUCTURE allowed with stamp
    """
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
    if sid not in (MIXED | PRICE) and not is_pure:
        # unknown -> fail closed
        print(f"write_event blocked: unknown {sid} on non-pure {dp}")
        return False, outbox_dir / "blocked.json"

    key = dedupe_key(event)
    outbox_dir.mkdir(parents=True, exist_ok=True)
    destination = outbox_dir / f"{key}.json"
    payload = dict(event)
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
            return False, destination
        _maybe_deliver_intent(payload)
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
        write_intent(intent, config.INTENT_INBOX)
    except Exception as exc:  # never break the advisory emit path
        print(f"intent delivery error: {exc}")
