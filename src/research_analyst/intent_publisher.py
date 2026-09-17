"""Persist admitted alpha events and retry trade-intent publication to the shared bus."""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import config
from alpha_outbox import OUTBOX_DIR, dedupe_key


_EVENT_STORAGE_DROP_KEYS = (
    "_score_result",
    "_admission_result",
    "structural_context",
    "engine_htf_provenance",
)

REQUIRED_FIELDS = {
    "schema_version", "alpha_id", "strategy_id", "asset", "direction",
    "setup_class", "phase", "observed_at", "valid_until", "horizon_minutes",
    "confidence", "entry_condition", "invalidation_price", "targets",
    "feature_snapshot", "dedupe_key",
}


def _slim_event_for_storage(event: dict) -> dict:
    """Drop large proof blocks that already have normalized ledger records."""
    return {key: value for key, value in event.items() if key not in _EVENT_STORAGE_DROP_KEYS}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return timestamp.astimezone(timezone.utc)


def validate_event(event: dict) -> None:
    """Validate the durable alpha envelope before ledger or bus processing."""
    missing = REQUIRED_FIELDS - event.keys()
    if missing:
        raise ValueError(f"missing fields: {', '.join(sorted(missing))}")
    if event["schema_version"] not in {1, 2}:
        raise ValueError("unsupported schema_version")
    if event["schema_version"] == 2:
        quality_score = event.get("quality_score")
        if not isinstance(quality_score, (int, float)) or not 0 <= quality_score <= 1:
            raise ValueError("quality_score must be between 0 and 1")
    if event["direction"] not in {"long", "short"}:
        raise ValueError("direction must be long or short")
    if event.get("status", "active") not in {"active", "expired", "invalidated"}:
        raise ValueError("invalid event status")
    if not isinstance(event["confidence"], (int, float)) or not 0 <= event["confidence"] <= 1:
        raise ValueError("confidence must be between 0 and 1")
    if not isinstance(event["entry_condition"], dict) or "type" not in event["entry_condition"]:
        raise ValueError("entry_condition must contain a type")
    if not isinstance(event["targets"], list):
        raise ValueError("targets must be a list")
    observed_at = parse_timestamp(event["observed_at"])
    if parse_timestamp(event["valid_until"]) <= observed_at:
        raise ValueError("valid_until must be after observed_at")
    if event["dedupe_key"] != dedupe_key(event):
        raise ValueError("dedupe_key does not match event identity")

    entry = event.get("entry_price")
    if entry is None:
        entry = (event.get("entry_condition") or {}).get("price")
    stop = event.get("invalidation_price", event.get("stop_loss"))
    if entry is None or stop is None or event.get("data_freshness_seconds") is None:
        raise ValueError("candidate admission fields are incomplete")

    if event["schema_version"] == 2 or event.get("_score_result"):
        admission = event.get("_score_result") or event.get("_admission_result") or {}
        if not math.isclose(
            float(event.get("quality_score", -1)),
            float(admission.get("quality_score", -2)),
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise ValueError("quality_score does not match score proof")
        if admission.get("score_decision") != "eligible":
            raise ValueError(
                "trade quality rejected: " + "; ".join(admission.get("hard_gate_reasons", []))
            )
    else:
        from structural_stop import _normalise_closed_bar_timestamp
        from trade_admission import admit, preserve_score_result

        admission = admit(
            event,
            now=_normalise_closed_bar_timestamp(event["observed_at"]),
            structural_context=event.get("structural_context"),
        )
        admission = preserve_score_result(admission, event.get("_admission_result"))
        if admission["hard_gate"] != "pass":
            raise ValueError(
                "admission failed: " + "; ".join(admission["hard_gate_reasons"])
            )
    event["_admission_result"] = admission


def normalize_event(event: dict) -> dict:
    """Repair a legacy admission-only target before validation and publication."""
    if event.get("targets"):
        return event
    admission = event.get("_admission_result") or {}
    target = event.get("take_profit", admission.get("selected_take_profit"))
    if target is None:
        from trade_admission import derive_2r_target

        entry = event.get("entry_price")
        if entry is None:
            entry = (event.get("entry_condition") or {}).get("price")
        target = derive_2r_target(
            event.get("direction"),
            entry,
            event.get("invalidation_price", event.get("stop_loss")),
        )
    if target is None:
        return event
    normalized = dict(event)
    normalized["targets"] = [target]
    return normalized


class IntentPublisher:
    """Own the analyst ledger and idempotent shared-bus retry pass."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        outbox_dir: Path = OUTBOX_DIR,
        now: Callable[[], datetime] = utc_now,
    ):
        self.db_path = str(db_path or config.ANALYST_DB_PATH)
        self.outbox_dir = Path(outbox_dir)
        self.now = now

    def _connect(self):
        config.init_alpha_db(self.db_path)
        return config.get_db_connection(db_path=self.db_path)

    def _persist_event(self, connection, event: dict, now: datetime) -> bool:
        expires_at = parse_timestamp(event["valid_until"])
        status = event.get("status", "active")
        if status == "active" and expires_at <= now:
            status = "expired"
        result = connection.execute(
            """
            INSERT INTO alpha_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING dedupe_key
            """,
            (
                event["dedupe_key"], event["alpha_id"], event["strategy_id"],
                event["asset"], event["direction"], event["setup_class"],
                event["phase"], status, parse_timestamp(event["observed_at"]),
                expires_at,
                json.dumps(_slim_event_for_storage(event), sort_keys=True, separators=(",", ":")),
                now,
            ),
        ).fetchone()
        if result is None:
            return False
        connection.execute(
            "INSERT INTO alpha_event_status_history VALUES (?, ?, ?, ?, ?)",
            (f"{event['alpha_id']}:persisted:{now.isoformat()}", event["alpha_id"], status, now, "persisted"),
        )
        components = event["feature_snapshot"].get("confidence_components")
        valid_components = isinstance(components, dict) and all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in components.values()
        )
        connection.execute(
            "INSERT INTO alpha_confidence_observations VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event["alpha_id"], event["confidence"],
                json.dumps(components, sort_keys=True) if valid_components else None,
                "observed" if valid_components else "unavailable",
                None if valid_components else "confidence_components_missing_or_invalid",
                parse_timestamp(event["observed_at"]), now,
            ),
        )
        snapshot = event["feature_snapshot"]
        connection.execute(
            """
            INSERT INTO alpha_candidates (
                candidate_id, observed_at, asset, source_symbol, direction, setup_class,
                phase, strategy_id, liquidity_tier, status, valid_until, entry_condition,
                invalidation_price, targets, feature_snapshot, promoted_alpha_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (candidate_id) DO NOTHING
            """,
            (
                event["alpha_id"], parse_timestamp(event["observed_at"]), event["asset"],
                snapshot.get("source_symbol"), event["direction"], event["setup_class"],
                event["phase"], event["strategy_id"], snapshot.get("liquidity_tier", "unknown"),
                status, expires_at, json.dumps(event["entry_condition"], sort_keys=True),
                event["invalidation_price"], json.dumps(event["targets"]),
                json.dumps(snapshot, sort_keys=True, default=str), event["alpha_id"],
            ),
        )
        return True

    def run_once(self) -> dict[str, int]:
        results = {"persisted": 0, "published": 0, "failed": 0, "invalid": 0, "skipped": 0}
        connection = self._connect()
        expired_outbox_paths: list[Path] = []
        try:
            now = self.now()
            expired = connection.execute(
                "SELECT alpha_id FROM alpha_events WHERE status = 'active' AND valid_until <= ?",
                (now,),
            ).fetchall()
            connection.execute(
                "UPDATE alpha_events SET status = 'expired' WHERE status = 'active' AND valid_until <= ?",
                (now,),
            )
            for (alpha_id,) in expired:
                connection.execute(
                    "INSERT INTO alpha_event_status_history VALUES (?, ?, 'expired', ?, 'valid_until_elapsed')",
                    (f"{alpha_id}:expired:{now.isoformat()}", alpha_id, now),
                )

            for path in sorted(self.outbox_dir.glob("*.json")):
                try:
                    event = normalize_event(json.loads(path.read_text(encoding="utf-8")))
                    validate_event(event)
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    print(f"Invalid alpha outbox event {path.name}: {error}", file=sys.stderr)
                    results["invalid"] += 1
                    continue
                now = self.now()
                if self._persist_event(connection, event, now):
                    results["persisted"] += 1
                if parse_timestamp(event["valid_until"]) <= now:
                    expired_outbox_paths.append(path)
                    results["skipped"] += 1
                    continue

                from alpha_outbox import _is_complete_candidate, _maybe_deliver_intent

                outcome = _maybe_deliver_intent(
                    event,
                    complete_candidate=_is_complete_candidate(event),
                )
                if outcome == "published":
                    results["published"] += 1
                elif outcome == "failed":
                    results["failed"] += 1
                else:
                    results["skipped"] += 1
            connection.commit()
        finally:
            connection.close()

        for path in expired_outbox_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return results
