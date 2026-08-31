"""Research Analyst -> Shared SQLite Intent Bus publisher (spec 3.2, 7).

Wires the existing schema-v1 Bybit envelope (built by intent_outbox) into the
shared bus. Routing defaults stay authoritative in intent_outbox: bybit/hyro,
compact strategies forced to bybit/hyro, sizing executor-owned, order_type
absent. This module is a thin transport - it contains no venue or sizing logic.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

# The shared bus package lives at the canonical neutral location (spec 4).
_SHARED_BUS_DIR = os.getenv(
    "INTENT_BUS_PACKAGE_DIR", "/home/ubuntu/shared/intent-bus"
)
if _SHARED_BUS_DIR not in sys.path:
    sys.path.insert(0, _SHARED_BUS_DIR)

from intent_bus import IntentBus, producer_adapters  # noqa: E402

import config  # noqa: E402


def publisher_enabled() -> bool:
    # Spec §14: research-analyst bus publish requires a configured DB path and
    # the bybit target switch. INTENT_DELIVERY_ENABLED is the overall gate
    # checked by the caller (alpha_outbox).
    return bool(getattr(config, "INTENT_BUS_DB", None)) and bybit_enabled()


def bybit_enabled() -> bool:
    return bool(getattr(config, "INTENT_BUS_BYBIT_ENABLED", False))


def propr_enabled() -> bool:
    return bool(getattr(config, "INTENT_BUS_PROPR_ENABLED", False))


def _bus() -> IntentBus:
    return IntentBus(db_path=getattr(config, "INTENT_BUS_DB", None) or None)


def publish_research_intent(
    intent: dict,
    *,
    target: str = "bybit",
    max_retries: int = 3,
) -> tuple[bool, Optional[str], Optional[Exception]]:
    """Publish an already-built schema-v1 envelope to the shared bus.

    Returns (ok, delivery_id, error). Never raises into the callers (spec 7:
    a publish failure is retryable and must not crash the pipeline).
    """
    if not getattr(config, "INTENT_BUS_DB", None):
        return False, None, None
    if target == "bybit" and not bybit_enabled():
        return False, None, None
    if target == "propr" and not propr_enabled():
        return False, None, None
    try:
        delivery = producer_adapters.build_research_analyst_delivery(
            envelope=intent, target=target, source_event_id=intent.get("delivery_id")
        )
    except Exception as exc:  # noqa: BLE001 - validation failure, do not crash
        return False, None, exc
    bus = _bus()
    try:
        ok, result, err = producer_adapters.publish_event_safe(
            bus,
            producer="research-analyst",
            producer_event_id=str(intent.get("delivery_id") or delivery.delivery_id),
            source=intent,
            schema_version=delivery.payload_schema_version,
            deliveries=[delivery],
            max_retries=max_retries,
        )
        if ok and result is not None:
            return True, result[1][0] if result[1] else delivery.delivery_id, None
        return ok, delivery.delivery_id, err
    finally:
        bus.close()
