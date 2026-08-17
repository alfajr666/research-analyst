"""Persist and deliver venue-neutral alpha events to Telegram only."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx

import config
from alpha_outbox import OUTBOX_DIR, dedupe_key


POLL_INTERVAL_SECONDS = 30
MAX_DELIVERY_ATTEMPTS = 5
RETRY_BASE_SECONDS = 30
REQUIRED_FIELDS = {
    "schema_version", "alpha_id", "strategy_id", "asset", "direction",
    "setup_class", "phase", "observed_at", "valid_until", "horizon_minutes",
    "confidence", "entry_condition", "invalidation_price", "targets",
    "feature_snapshot", "dedupe_key",
}


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
    family = "Continuation" if event["setup_class"].startswith("continuation") else (
        "Accumulation base" if event["setup_class"] == "accumulation_base" else "Impulse ignition"
    )
    trigger = event["entry_condition"]
    trigger_text = trigger["type"].replace("_", " ")
    if "price" in trigger:
        trigger_text += f" at {trigger['price']:g}"
    targets = ", ".join(f"{target:g}" for target in event["targets"])
    expiry = parse_timestamp(event["valid_until"]).strftime("%Y-%m-%d %H:%M UTC")
    observed = parse_timestamp(event["observed_at"]).strftime("%Y-%m-%d %H:%M UTC")
    return (
        "ALPHA SIGNAL\n"
        f"Strategy family: {family}\n"
        f"Strategy: {event['strategy_id']}\n"
        f"Asset: {event['asset']}\n"
        f"Direction: {event['direction'].upper()}\n"
        f"Phase: {event['phase']}\n"
        f"Confidence: {event['confidence']:.0%}\n"
        f"Trigger: {trigger_text}\n"
        f"Invalidation: {event['invalidation_price']:g}\n"
        f"Targets: {targets}\n"
        f"Expiry: {expiry}\n"
        f"Observed: {observed}"
    )


def format_research_note(report: dict) -> str:
    """Render persisted advisory content without changing deterministic signal fields."""
    limitations = "; ".join(report.get("limitations", [])[:2])
    note = f"\n\nResearch note (advisory)\nVerdict: {report['verdict']}\n{report['thesis_summary']}"
    if limitations:
        note += f"\nLimitations: {limitations}"
    return note[:900]


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
        transport: TelegramTransport | None = None,
        now: Callable[[], datetime] = utc_now,
    ):
        self.db_path = str(db_path or config.ALPHA_DB_PATH)
        self.outbox_dir = Path(outbox_dir)
        self.transport = transport or TelegramTransport()
        self.now = now

    def _connect(self):
        config.init_db(self.db_path)
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

    def _next_attempt(self, connection, key: str, now: datetime) -> int | None:
        row = connection.execute("""
            SELECT attempt_number, status, next_retry_at
            FROM signal_deliveries WHERE dedupe_key = ? AND channel = 'telegram'
            ORDER BY attempt_number DESC LIMIT 1
        """, (key,)).fetchone()
        if row is None:
            return 1
        attempt, status, next_retry_at = row
        if status == "sent" or attempt >= MAX_DELIVERY_ATTEMPTS:
            return None
        if next_retry_at is not None and next_retry_at > now:
            return None
        return attempt + 1

    def _deliver(self, connection, event: dict, now: datetime) -> str | None:
        row = connection.execute("SELECT status, valid_until FROM alpha_events WHERE dedupe_key = ?", (event["dedupe_key"],)).fetchone()
        if row is None or row[0] != "active" or row[1] <= now:
            return None
        attempt = self._next_attempt(connection, event["dedupe_key"], now)
        if attempt is None:
            return None
        try:
            message = format_signal(event)
            if config.LLM_INCLUDE_IN_TELEGRAM:
                from research_repository import latest_event_report
                report = latest_event_report(connection, event["alpha_id"])
                if report is not None:
                    message += format_research_note(report)
            response = self.transport.send(message)
        except Exception as error:
            delay = RETRY_BASE_SECONDS * 2 ** (attempt - 1)
            connection.execute("""
                INSERT INTO signal_deliveries VALUES (?, ?, 'telegram', ?, 'failed', ?, NULL, ?, NULL, ?)
            """, (f"{event['dedupe_key']}:telegram:{attempt}", event["dedupe_key"], attempt, now, now + timedelta(seconds=delay), str(error)))
            return "failed"
        connection.execute("""
            INSERT INTO signal_deliveries VALUES (?, ?, 'telegram', ?, 'sent', ?, ?, NULL, ?, NULL)
        """, (f"{event['dedupe_key']}:telegram:{attempt}", event["dedupe_key"], attempt, now, now, str(response)))
        return "sent"

    def _research_ready_for_delivery(self, connection, event: dict) -> bool:
        """Hold LLM-enabled delivery until its bounded review reaches a terminal state."""
        if not config.LLM_RESEARCH_ENABLED:
            return True
        from research_repository import event_review_status
        return event_review_status(connection, event["alpha_id"]) in {"completed", "failed", "skipped"}

    def _record_expired_outcomes(self, connection, now: datetime) -> None:
        """Evaluate expired events from the immutable local bar history once."""
        rows = connection.execute("""
            SELECT alpha_id, event_json FROM alpha_events
            WHERE status = 'expired'
              AND alpha_id NOT IN (SELECT candidate_id FROM alpha_outcomes)
        """).fetchall()
        for candidate_id, serialized_event in rows:
            event = json.loads(serialized_event)
            snapshot = event["feature_snapshot"]
            symbol = snapshot.get("source_symbol")
            if not symbol:
                continue
            observed_at = parse_timestamp(event["observed_at"])
            valid_until = parse_timestamp(event["valid_until"])
            bars = connection.execute("""
                SELECT timestamp, high, low, close FROM futures_data
                WHERE symbol = ? AND timestamp >= ? AND timestamp <= ? AND close > 0
                ORDER BY timestamp
            """, (symbol, observed_at, valid_until)).fetchall()
            if not bars:
                continue
            entry = float(event["entry_condition"].get("price", bars[0][3]))
            direction = event["direction"]
            trigger = event["entry_condition"]["type"]

            def triggered(bar) -> bool:
                _, high, low, _ = bar
                if trigger == "breakout_above":
                    return high >= entry
                if trigger == "breakout_below":
                    return low <= entry
                return low <= entry if direction == "long" else high >= entry

            entry_index = next((index for index, bar in enumerate(bars) if triggered(bar)), None)
            if entry_index is None:
                outcome = "not_triggered"
                entry_at = None
                returns = (None, None, None)
                favorable = adverse = None
                outcome_bars = []
            else:
                entry_at = bars[entry_index][0]
                later_bars = bars[entry_index:]
                target = min(event["targets"]) if direction == "long" else max(event["targets"])
                invalidation = float(event["invalidation_price"])

                def barrier_status(bar) -> tuple[bool, bool]:
                    _, high, low, _ = bar
                    if direction == "long":
                        return high >= target, low <= invalidation
                    return low <= target, high >= invalidation

                terminal_index = None
                for index, bar in enumerate(later_bars):
                    target_hit, invalidated = barrier_status(bar)
                    # OHLC cannot establish which condition came first inside a bar.
                    if index == 0 and (target_hit or invalidated):
                        outcome = "ambiguous_same_bar"
                        terminal_index = index
                        break
                    if target_hit and invalidated:
                        outcome = "ambiguous_same_bar"
                        terminal_index = index
                        break
                    if target_hit:
                        outcome = "target"
                        terminal_index = index
                        break
                    if invalidated:
                        outcome = "invalidated"
                        terminal_index = index
                        break
                else:
                    outcome = "expired"
                outcome_bars = later_bars if terminal_index is None else later_bars[:terminal_index + 1]

                def return_at(minutes: int) -> float | None:
                    target_at = entry_at + timedelta(minutes=minutes)
                    bar = next((item for item in outcome_bars if item[0] >= target_at), None)
                    if bar is None:
                        return None
                    raw_return = float(bar[3]) / entry - 1
                    return raw_return if direction == "long" else -raw_return

                returns = (return_at(15), return_at(60), return_at(240))
                highs = [float(bar[1]) / entry - 1 for bar in outcome_bars]
                lows = [float(bar[2]) / entry - 1 for bar in outcome_bars]
                favorable = max(highs) if direction == "long" else -min(lows)
                adverse = min(lows) if direction == "long" else -max(highs)
            connection.execute("""
                INSERT INTO alpha_outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """, (
                candidate_id, now, entry_at, entry if entry_index is not None else None, outcome,
                valid_until, *returns, favorable, adverse,
                json.dumps({"bars_observed": len(bars), "same_bar_policy": "ambiguous"}, sort_keys=True),
            ))

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
            for (alpha_id,) in expired:
                connection.execute("""
                    INSERT INTO alpha_event_status_history VALUES (?, ?, 'expired', ?, 'valid_until_elapsed')
                """, (f"{alpha_id}:expired:{now.isoformat()}", alpha_id, now))
            self._record_expired_outcomes(connection, self.now())
            valid_events = []
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
                    from research_repository import queue_event_review
                    queue_event_review(connection, event["alpha_id"], parse_timestamp(event["observed_at"]), config.LLM_RESEARCH_ENABLED)
                elif config.LLM_RESEARCH_ENABLED:
                    # Events persisted before LLM mode was enabled still need a review.
                    from research_repository import queue_event_review
                    queue_event_review(connection, event["alpha_id"], parse_timestamp(event["observed_at"]), True)
                valid_events.append(event)
            # Research runs before LLM-enabled delivery, but cannot alter the event.
            try:
                from research_repository import ResearchCoordinator
                ResearchCoordinator().process(connection)
            except Exception as error:
                print(f"Research coordinator error: {error}", file=sys.stderr)
            for event in valid_events:
                if not self._research_ready_for_delivery(connection, event):
                    continue
                outcome = self._deliver(connection, event, self.now())
                if outcome:
                    results[outcome] += 1
            # Execution delivery is independent of Telegram and alpha persistence.
            # Its failures are durable in execution_deliveries and never abort this loop.
            try:
                from execution_adapter import ExecutionAdapter
                ExecutionAdapter().deliver(connection)
            except Exception as error:
                print(f"Execution adapter error: {error}", file=sys.stderr)
        finally:
            connection.close()
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
