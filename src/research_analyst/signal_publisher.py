"""Persist and deliver venue-neutral alpha events to Telegram and optional Discord."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

import httpx

import config
from alpha_outbox import OUTBOX_DIR, dedupe_key
from discord_format import format_alpha_signal, format_discord_research_note, format_discord_signal


POLL_INTERVAL_SECONDS = 30
MAX_DELIVERY_ATTEMPTS = 5
RETRY_BASE_SECONDS = 30
CLAIM_LEASE_SECONDS = 60
REQUIRED_FIELDS = {
    "schema_version", "alpha_id", "strategy_id", "asset", "direction",
    "setup_class", "phase", "observed_at", "valid_until", "horizon_minutes",
    "confidence", "entry_condition", "invalidation_price", "targets",
    "feature_snapshot", "dedupe_key",
}


class MessageTransport(Protocol):
    def send(self, text: str) -> str: ...


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return timestamp.astimezone(timezone.utc)


def validate_event(event: dict) -> None:
    missing = REQUIRED_FIELDS - event.keys()
    if missing:
        raise ValueError(f"missing fields: {', '.join(sorted(missing))}")
    if event["schema_version"] != 1:
        raise ValueError("unsupported schema_version")
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


def format_signal(event: dict) -> str:
    return format_alpha_signal(event, markdown=False)


def format_research_note(report: dict) -> str:
    """Render persisted advisory content without changing deterministic signal fields."""
    limitations = "; ".join(report.get("limitations", [])[:2])
    note = f"\n\nResearch note (advisory)\nVerdict: {report['verdict']}\n{report['thesis_summary']}"
    if limitations:
        note += f"\nLimitations: {limitations}"
    return note[:900]


def default_transports(
    transport: MessageTransport | None = None,
) -> dict[str, MessageTransport]:
    """Build the active channel map. Explicit transport keeps tests on a single channel."""
    if transport is not None:
        return {"telegram": transport}
    channels: dict[str, MessageTransport] = {"telegram": TelegramTransport()}
    if config.DISCORD_ALPHA_WEBHOOK_URL:
        from discord_transport import DiscordWebhookTransport
        channels["discord"] = DiscordWebhookTransport(config.DISCORD_ALPHA_WEBHOOK_URL)
    return channels


class TelegramTransport:
    """Minimal transport boundary so publisher behavior is testable offline."""

    def send(self, text: str) -> str:
        if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
            raise RuntimeError("Telegram credentials are not configured")
        response = httpx.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(response.text)
        return response.text


class SignalPublisher:
    def __init__(
        self,
        db_path: str | Path | None = None,
        outbox_dir: Path = OUTBOX_DIR,
        transport: MessageTransport | None = None,
        transports: dict[str, MessageTransport] | None = None,
        now: Callable[[], datetime] = utc_now,
        market_db_path: str | Path | None = None,
    ):
        self.db_path = str(db_path or config.ANALYST_DB_PATH)
        self.market_db_path = str(market_db_path or config.MARKET_DB_PATH)
        self.outbox_dir = Path(outbox_dir)
        self.transports = dict(transports) if transports is not None else default_transports(transport)
        # Backward-compatible single-transport handle used by older tests/callers.
        self.transport = self.transports.get("telegram") or next(iter(self.transports.values()), TelegramTransport())
        self.now = now

    def _connect(self):
        config.init_alpha_db(self.db_path)
        return config.get_db_connection(db_path=self.db_path)

    def _persist_event(self, connection, event: dict, now: datetime) -> bool:
        expires_at = parse_timestamp(event["valid_until"])
        status = event.get("status", "active")
        if status == "active" and expires_at <= now:
            status = "expired"
        result = connection.execute("""
            INSERT INTO alpha_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING dedupe_key
        """, (
            event["dedupe_key"], event["alpha_id"], event["strategy_id"], event["asset"],
            event["direction"], event["setup_class"], event["phase"], status,
            parse_timestamp(event["observed_at"]), expires_at,
            json.dumps(event, sort_keys=True, separators=(",", ":")), now,
        )).fetchone()
        if result is None:
            return False
        connection.execute("""
            INSERT INTO alpha_event_status_history VALUES (?, ?, ?, ?, ?)
        """, (f"{event['alpha_id']}:persisted:{now.isoformat()}", event["alpha_id"], status, now, "persisted"))
        components = event["feature_snapshot"].get("confidence_components")
        valid_components = isinstance(components, dict) and all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in components.values()
        )
        connection.execute("""
            INSERT INTO alpha_confidence_observations VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            event["alpha_id"], event["confidence"],
            json.dumps(components, sort_keys=True) if valid_components else None,
            "observed" if valid_components else "unavailable",
            None if valid_components else "confidence_components_missing_or_invalid",
            parse_timestamp(event["observed_at"]), now,
        ))
        snapshot = event["feature_snapshot"]
        connection.execute("""
            INSERT INTO alpha_candidates (
                candidate_id, observed_at, asset, source_symbol, direction, setup_class,
                phase, strategy_id, liquidity_tier, status, valid_until, entry_condition,
                invalidation_price, targets, feature_snapshot, promoted_alpha_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (candidate_id) DO NOTHING
        """, (
            event["alpha_id"], parse_timestamp(event["observed_at"]), event["asset"],
            snapshot.get("source_symbol"), event["direction"], event["setup_class"],
            event["phase"], event["strategy_id"], snapshot.get("liquidity_tier", "unknown"),
            status, expires_at, json.dumps(event["entry_condition"], sort_keys=True),
            event["invalidation_price"], json.dumps(event["targets"]),
            json.dumps(snapshot, sort_keys=True, default=str), event["alpha_id"],
        ))
        return True

    def _next_attempt(self, connection, key: str, now: datetime, channel: str) -> int | None:
        row = connection.execute("""
            SELECT attempt_number, status, next_retry_at
            FROM signal_deliveries WHERE dedupe_key = ? AND channel = ?
            ORDER BY attempt_number DESC LIMIT 1
        """, (key, channel)).fetchone()
        if row is None:
            return 1
        attempt, status, next_retry_at = row
        if status == "sent" or attempt >= MAX_DELIVERY_ATTEMPTS:
            return None
        if status == "claimed":
            retry_at = parse_timestamp(next_retry_at) if isinstance(next_retry_at, str) else next_retry_at
            if retry_at is None or retry_at <= now:
                # lease expired, allow reclaim (reuse attempt)
                return attempt
            return None
        retry_at = parse_timestamp(next_retry_at) if isinstance(next_retry_at, str) else next_retry_at
        if retry_at is not None and retry_at > now:
            return None
        return attempt + 1

    def _render_message(self, connection, event: dict, channel: str) -> str:
        if channel == "discord":
            message = format_discord_signal(event)
            include_research = config.LLM_INCLUDE_IN_DISCORD
            note_formatter = format_discord_research_note
        else:
            message = format_signal(event)
            include_research = config.LLM_INCLUDE_IN_TELEGRAM
            note_formatter = format_research_note
        if include_research:
            from research_repository import latest_event_report
            report = latest_event_report(connection, event["alpha_id"])
            if report is not None:
                message += note_formatter(report)
        return message

    def _deliver(self, connection, event: dict, now: datetime, channel: str, transport: MessageTransport) -> str | None:
        row = connection.execute("SELECT status, valid_until FROM alpha_events WHERE dedupe_key = ?", (event["dedupe_key"],)).fetchone()
        valid_until = parse_timestamp(row[1]) if row is not None else None
        if row is None or row[0] != "active" or valid_until <= now:
            return None
        attempt = self._next_attempt(connection, event["dedupe_key"], now, channel)
        if attempt is None:
            return None
        claim_id = f"{event['dedupe_key']}:{channel}:{attempt}"
        lease_until = now + timedelta(seconds=CLAIM_LEASE_SECONDS)
        # claim (idempotent on conflict for this attempt)
        connection.execute("""
            INSERT INTO signal_deliveries (delivery_id, dedupe_key, channel, attempt_number, status, attempted_at, next_retry_at)
            VALUES (?, ?, ?, ?, 'claimed', ?, ?)
            ON CONFLICT (dedupe_key, channel, attempt_number) DO UPDATE SET
                status = 'claimed', attempted_at = ?, next_retry_at = ?
        """, (claim_id, event["dedupe_key"], channel, attempt, now, lease_until, now, lease_until))
        try:
            message = self._render_message(connection, event, channel)
            response = transport.send(message)
        except Exception as error:
            delay = RETRY_BASE_SECONDS * 2 ** (attempt - 1)
            connection.execute("""
                UPDATE signal_deliveries SET status='failed', completed_at=?, next_retry_at=?, error_message=?
                WHERE delivery_id=?
            """, (now, now + timedelta(seconds=delay), str(error), claim_id))
            return "failed"
        connection.execute("""
            UPDATE signal_deliveries SET status='sent', completed_at=?, response_body=?
            WHERE delivery_id=?
        """, (now, str(response), claim_id))
        return "sent"

    def _research_ready_for_delivery(self, connection, event: dict) -> bool:
        """Hold LLM-enabled delivery until its bounded review reaches a terminal state."""
        if not config.LLM_RESEARCH_ENABLED:
            return True
        from research_repository import event_review_status
        return event_review_status(connection, event["alpha_id"]) in {"completed", "failed", "skipped"}

    def run_once(self) -> dict[str, int]:
        results = {"persisted": 0, "sent": 0, "failed": 0, "invalid": 0}
        connection = self._connect()
        try:
            now = self.now()
            expired = connection.execute("""
                SELECT alpha_id FROM alpha_events
                WHERE status = 'active' AND valid_until <= ?
            """, (now,)).fetchall()
            connection.execute("""
                UPDATE alpha_events SET status = 'expired'
                WHERE status = 'active' AND valid_until <= ?
            """, (now,))
            connection.commit()
            for (alpha_id,) in expired:
                connection.execute("""
                    INSERT INTO alpha_event_status_history VALUES (?, ?, 'expired', ?, 'valid_until_elapsed')
                """, (f"{alpha_id}:expired:{now.isoformat()}", alpha_id, now))
            valid_events = []
            expired_outbox_paths = []
            for path in sorted(self.outbox_dir.glob("*.json")):
                try:
                    event = json.loads(path.read_text(encoding="utf-8"))
                    validate_event(event)
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    print(f"Invalid alpha outbox event {path.name}: {error}", file=sys.stderr)
                    results["invalid"] += 1
                    continue
                now = self.now()
                if self._persist_event(connection, event, now):
                    results["persisted"] += 1
                    if config.LLM_RESEARCH_ENABLED:
                        from research_repository import queue_event_review
                        queue_event_review(connection, event["alpha_id"], parse_timestamp(event["observed_at"]), True)
                elif config.LLM_RESEARCH_ENABLED:
                    # Events persisted before LLM mode was enabled still need a review.
                    from research_repository import queue_event_review
                    queue_event_review(connection, event["alpha_id"], parse_timestamp(event["observed_at"]), True)
                try:
                    if parse_timestamp(event["valid_until"]) <= self.now():
                        expired_outbox_paths.append(path)
                    else:
                        valid_events.append(event)
                except (KeyError, TypeError, ValueError, OverflowError):
                    valid_events.append(event)
            # Research runs before LLM-enabled delivery, but cannot alter the event.
            if config.LLM_RESEARCH_ENABLED:
                try:
                    from research_repository import ResearchCoordinator
                    ResearchCoordinator().process(connection)
                except Exception as error:
                    print(f"Research coordinator error: {error}", file=sys.stderr)
            for event in valid_events:
                if not self._research_ready_for_delivery(connection, event):
                    continue
                for channel, transport in self.transports.items():
                    outcome = self._deliver(connection, event, self.now(), channel, transport)
                    if outcome:
                        results[outcome] += 1
            # Executor delivery is performed by alpha_outbox through the shared
            # SQLite intent bus. Do not invoke the retired filesystem adapter here.
            connection.commit()
        finally:
            connection.close()
        for path in expired_outbox_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return results


def main() -> None:
    publisher = SignalPublisher()
    while True:
        try:
            print(f"Signal publisher: {publisher.run_once()}")
        except Exception as error:
            print(f"Signal publisher error: {error}", file=sys.stderr)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
