"""Deliver validated, immutable research alpha events to target bot inboxes."""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

import config
from signal_publisher import parse_timestamp


TARGETS = frozenset({"bybit", "bybit-test", "mexc", "propr"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def execution_targets(configured_targets: Mapping[str, Mapping] = config.EXECUTION_TARGETS) -> dict[str, Mapping]:
    """Return only explicitly enabled destinations."""
    return {
        target: settings for target, settings in configured_targets.items()
        if target in TARGETS and settings.get("enabled", False)
    }


def validate_event(event: dict, now: datetime) -> tuple[str | None, dict | None]:
    """Return a terminal skip reason or normalized executable event values."""
    if event.get("schema_version") != 1:
        return "unsupported_schema_version", None
    if event.get("status") == "expired":
        return "expired", None
    if event.get("status") != "active":
        return "inactive", None
    try:
        if parse_timestamp(event["valid_until"]) <= now:
            return "expired", None
        entry = float(event["entry_condition"]["price"])
        stop = float(event["invalidation_price"])
        targets = event["targets"]
        target = float(targets[0]) if isinstance(targets, list) and len(targets) == 1 else None
    except (KeyError, TypeError, ValueError):
        return "invalid_execution_fields", None
    if event.get("entry_condition", {}).get("type") != "limit_at_ema_context":
        return "unsupported_entry_condition", None
    if target is None:
        return "multi_target", None
    if not all(math.isfinite(value) and value > 0 for value in (entry, stop, target)):
        return "invalid_execution_fields", None
    direction = event.get("direction", "").lower()
    valid_geometry = (
        direction == "long" and stop < entry < target
    ) or (
        direction == "short" and target < entry < stop
    )
    if not valid_geometry:
        return "invalid_directional_geometry", None
    confidence = event.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
        return "invalid_execution_fields", None
    asset = event.get("asset")
    if not isinstance(asset, str) or not asset:
        return "invalid_execution_fields", None
    return None, {"entry_price": entry, "stop_loss": stop, "take_profit": target}


class ExecutionAdapter:
    """Own the durable research-to-bot delivery boundary, never bot execution."""

    def __init__(
        self,
        outbox_dir: Path = config.EXECUTION_OUTBOX_DIR,
        targets: Mapping[str, Mapping] = config.EXECUTION_TARGETS,
        now: Callable[[], datetime] = utc_now,
    ):
        self.outbox_dir = Path(outbox_dir)
        self.targets = execution_targets(targets)
        self.now = now

    def _record(self, connection, alpha_id: str, target: str, status: str, reason: str | None = None, inbox_path: Path | None = None, written_at: datetime | None = None) -> None:
        connection.execute("""
            INSERT INTO execution_deliveries (alpha_id, target, status, reason, inbox_path, written_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (alpha_id, target) DO NOTHING
        """, (alpha_id, target, status, reason, str(inbox_path) if inbox_path else None, written_at))

    def _mark_written(self, connection, alpha_id: str, target: str, path: Path, written_at: datetime) -> None:
        connection.execute("""
            INSERT INTO execution_deliveries (alpha_id, target, status, inbox_path, written_at)
            VALUES (?, ?, 'written', ?, ?)
            ON CONFLICT (alpha_id, target) DO UPDATE SET
                status = 'written', reason = NULL, inbox_path = excluded.inbox_path,
                written_at = excluded.written_at
        """, (alpha_id, target, str(path), written_at))

    def _write_inbox(self, path: Path, item: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return
        serialized = json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".execution-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            # The delivery ledger prevents normal replacement; this is also
            # recoverable if a process dies after the rename but before DB state.
            if not path.exists():
                os.replace(temporary, path)
                temporary = None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def _item(self, event: dict, target: str, values: dict) -> dict:
        strategy_id = f"RS-{event['strategy_id']}"
        return {
            "schema_version": 1,
            "target": target,
            "delivery_id": f"{event['alpha_id']}:{target}",
            "alpha_id": event["alpha_id"],
            "source": "research_analyst",
            "strategy_id": strategy_id,
            "entry_tag": strategy_id,
            "asset": event["asset"].upper(),
            "symbol": f"{event['asset'].upper()}/USDT:USDT",
            "direction": event["direction"].upper(),
            "order_type": "limit",
            **values,
            "take_profit_mode": "fixed_full_close",
            "observed_at": format_timestamp(parse_timestamp(event["observed_at"])),
            "entry_valid_until": format_timestamp(parse_timestamp(event["valid_until"])),
            "confidence": event["confidence"],
            "research_event": {
                "strategy_id": event["strategy_id"],
                "setup_class": event["setup_class"],
                "phase": event["phase"],
            },
        }

    def _propr_supports(self, asset: str, settings: Mapping) -> bool:
        """Read Propr's locally refreshed tradeable-assets snapshot, never infer it."""
        try:
            snapshot = json.loads(Path(settings["tradeable_assets_path"]).read_text(encoding="utf-8"))
            assets = snapshot["assets"] if isinstance(snapshot, dict) else snapshot
            supported = {
                str(value.get("base") or value.get("asset") or value.get("id", "")).upper()
                for value in assets if isinstance(value, dict) and value.get("enabled", True)
            }
            supported.update(str(value).upper() for value in assets if not isinstance(value, dict))
            return asset.upper() in supported
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def reconcile_receipts(self, connection) -> int:
        """Record terminal bot receipts written under each target's inbox directory."""
        reconciled = 0
        for target in self.targets:
            for path in (self.outbox_dir / target / "receipts").glob("*.json"):
                try:
                    receipt = json.loads(path.read_text(encoding="utf-8"))
                    alpha_id = receipt["alpha_id"]
                    if receipt["delivery_id"] != f"{alpha_id}:{target}":
                        continue
                    receipt_status = receipt["status"]
                    if receipt_status.startswith("accepted_"):
                        status = "acknowledged"
                    elif receipt_status.startswith("skipped_"):
                        status = "skipped"
                    elif receipt_status.startswith("failed_"):
                        status = "failed"
                    else:
                        continue
                    result = connection.execute("""
                        UPDATE execution_deliveries
                        SET status = ?, reason = ?, acknowledged_at = ?, bot_trade_id = ?, bot_order_id = ?
                        WHERE alpha_id = ? AND target = ? AND status = 'written'
                        RETURNING alpha_id
                    """, (
                        status, receipt.get("reason", receipt_status), self.now(), receipt.get("bot_trade_id"),
                        receipt.get("bot_order_id"), alpha_id, target,
                    ))
                    reconciled += 1 if result.fetchone() is not None else 0
                except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                    continue
        return reconciled

    def observability(self, connection) -> dict:
        """Return the target-level delivery state required for operational reporting."""
        states = connection.execute("""
            SELECT target, status, COUNT(*) FROM execution_deliveries GROUP BY target, status
        """).fetchall()
        reasons = connection.execute("""
            SELECT target, reason, COUNT(*) FROM execution_deliveries
            WHERE reason IS NOT NULL GROUP BY target, reason
        """).fetchall()
        oldest = connection.execute("""
            SELECT target, MIN(written_at) FROM execution_deliveries
            WHERE status = 'written' GROUP BY target
        """).fetchall()
        return {"states": states, "reasons": reasons, "oldest_unacknowledged": oldest}

    def deliver(self, connection) -> dict[str, int]:
        """Write each eligible alpha once. Atomic write failures remain retryable."""
        results = {"written": 0, "acknowledged": self.reconcile_receipts(connection), "skipped": 0, "failed": 0}
        if not self.targets:
            return results
        now = self.now()
        rows = connection.execute("""
            SELECT alpha_id, status, event_json FROM alpha_events
        """).fetchall()
        for alpha_id, status, serialized_event in rows:
            event = json.loads(serialized_event)
            event["alpha_id"] = alpha_id
            event["status"] = status
            reason, values = validate_event(event, now)
            for target, settings in self.targets.items():
                existing = connection.execute("""
                    SELECT status, reason FROM execution_deliveries WHERE alpha_id = ? AND target = ?
                """, (alpha_id, target)).fetchone()
                if existing is not None and (
                    existing[0] != "failed" or not (existing[1] or "").startswith("atomic_write_failed:")
                ):
                    continue
                if reason:
                    self._record(connection, alpha_id, target, "skipped", reason)
                    results["skipped"] += 1
                    continue
                allowlist = settings.get("asset_allowlist", frozenset())
                supported = self._propr_supports(event["asset"], settings) if target == "propr" else (
                    not allowlist or event["asset"].upper() in allowlist
                )
                if not supported:
                    self._record(connection, alpha_id, target, "skipped", "unsupported_symbol")
                    results["skipped"] += 1
                    continue
                path = self.outbox_dir / target / f"{alpha_id}.json"
                try:
                    self._write_inbox(path, self._item(event, target, values))
                except OSError as error:
                    connection.execute("""
                        INSERT INTO execution_deliveries (alpha_id, target, status, reason)
                        VALUES (?, ?, 'failed', ?)
                        ON CONFLICT (alpha_id, target) DO UPDATE SET status = 'failed', reason = excluded.reason
                    """, (alpha_id, target, f"atomic_write_failed:{error.__class__.__name__}"))
                    results["failed"] += 1
                    continue
                self._mark_written(connection, alpha_id, target, path, now)
                results["written"] += 1
        return results
