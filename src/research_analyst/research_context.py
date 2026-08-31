"""Build bounded, replayable local-evidence packets for event reviews."""

from __future__ import annotations

import json
from datetime import datetime


def _text(value: object, limit: int) -> str:
    return str(value)[:limit]


def _event_evidence(event: dict) -> dict:
    return {
        "evidence_id": f"event:{event['alpha_id']}",
        "source_type": "alpha_event",
        "source_ref": event["alpha_id"],
        "value": {
            "strategy_id": event["strategy_id"],
            "asset": event["asset"],
            "direction": event["direction"],
            "setup_class": event["setup_class"],
            "phase": event["phase"],
            "observed_at": event["observed_at"],
            "event": _text(event["event_json"], 12000),
        },
    }


def build_event_review(connection, request_id: str, alpha_id: str,
                       as_of: datetime, max_chars: int) -> dict:
    """Build a deterministic review packet from persisted local data only."""
    event = connection.execute(
        """SELECT alpha_id, strategy_id, asset, direction, setup_class, phase,
                  observed_at, event_json
           FROM alpha_events WHERE alpha_id = ?""",
        (alpha_id,),
    ).fetchone()
    if event is None:
        raise ValueError("alpha event is unavailable")

    event_data = dict(zip(
        ("alpha_id", "strategy_id", "asset", "direction", "setup_class",
         "phase", "observed_at", "event_json"), event,
    ))
    evidence = [_event_evidence(event_data)]
    snapshots = connection.execute(
        """SELECT snapshot_id, feature_set, payload_json
           FROM feature_snapshots
           WHERE asset = ? AND computed_at <= ?
           ORDER BY computed_at DESC, snapshot_id DESC LIMIT 8""",
        (event_data["asset"], as_of),
    ).fetchall()
    for snapshot_id, feature_set, payload in snapshots:
        try:
            value = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            value = {"raw": _text(payload, 1000)}
        evidence.append({
            "evidence_id": f"feature:{snapshot_id}",
            "source_type": "feature_snapshot",
            "source_ref": str(snapshot_id),
            "value": {"feature_set": feature_set, "payload": value},
        })

    packet = {
        "schema_version": 1,
        "request_id": request_id,
        "question": None,
        "subject": {
            "type": "alpha_event",
            "id": alpha_id,
            "as_of": as_of.isoformat(),
        },
        "evidence": evidence,
    }
    serialized = json.dumps(packet, sort_keys=True, separators=(",", ":"), default=str)
    if len(serialized) > max_chars:
        packet["evidence"] = evidence[:1]
    return packet
