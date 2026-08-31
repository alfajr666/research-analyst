"""Research-ledger persistence, state transitions, and bounded completion work."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx

import config
from llm_client import ClientConfigurationError, configured_client
from research_contracts import canonical_json, evidence_ids, input_hash, validate_event_review_output
from research_context import build_event_review
from research_prompt import PROMPT_VERSION, SYSTEM_PROMPT, task_prompt


STALE_GRACE_SECONDS = 5


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def queue_event_review(connection, alpha_id: str, as_of: datetime, enabled: bool) -> tuple[bool, str]:
    request_id = str(uuid4())
    packet = build_event_review(connection, request_id, alpha_id, as_of, config.LLM_MAX_INPUT_CHARS)
    digest = input_hash(packet)
    status = "pending" if enabled else "skipped"
    row = connection.execute("""INSERT INTO research_requests (request_id, subject_type, subject_id, request_kind, as_of, input_hash, status, created_at, completed_at, error_code, request_input_json)
        VALUES (?, 'alpha_event', ?, 'event_review', ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (subject_type, subject_id, request_kind, input_hash) DO NOTHING RETURNING request_id""", (request_id, alpha_id, as_of, digest, status, as_of, as_of if not enabled else None, "disabled" if not enabled else None, canonical_json(packet))).fetchone()
    return row is not None, digest


def _evidence_map(packet: dict) -> dict[str, dict]:
    found: dict[str, dict] = {}
    def walk(value: object) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("evidence_id"), str):
                found[value["evidence_id"]] = value
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(packet)
    return found


class ResearchCoordinator:
    """Runs a small number of independently persisted local research requests."""

    def __init__(self, settings=config, client=None, now=utc_now):
        self.settings, self.client, self.now = settings, client, now

    def _recover_stale(self, connection, now: datetime) -> int:
        cutoff = now - timedelta(seconds=self.settings.LLM_TIMEOUT_SECONDS + STALE_GRACE_SECONDS)
        exhausted = connection.execute("""UPDATE research_requests SET status = 'failed', completed_at = ?, error_code = 'stale_exhausted', error_message = 'running request exceeded recovery window'
            WHERE status = 'running' AND started_at < ? AND attempt_count > ? RETURNING request_id""", (now, cutoff, self.settings.LLM_MAX_RETRIES)).fetchall()
        recovered = connection.execute("""UPDATE research_requests SET status = 'pending', started_at = NULL, next_attempt_at = ?, error_code = 'stale_recovered', error_message = 'running request exceeded recovery window'
            WHERE status = 'running' AND started_at < ? RETURNING request_id""", (now, cutoff)).fetchall()
        return len(exhausted) + len(recovered)

    def _claim(self, connection, now: datetime):
        row = connection.execute("""SELECT request_id FROM research_requests
            WHERE status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY created_at LIMIT 1""", (now,)).fetchone()
        if row is None:
            return None
        return connection.execute("""UPDATE research_requests SET status = 'running', started_at = ?, attempt_count = attempt_count + 1,
            error_code = NULL, error_message = NULL WHERE request_id = ? AND status = 'pending' RETURNING request_id, subject_id, request_kind, as_of, input_hash, attempt_count, request_input_json""", (now, row[0])).fetchone()

    def _finish(self, connection, request_id: str, status: str, now: datetime, code: str | None = None, message: str | None = None, next_attempt_at=None) -> None:
        connection.execute("""UPDATE research_requests SET status = ?, completed_at = ?, next_attempt_at = ?, error_code = ?, error_message = ?
            WHERE request_id = ?""", (status, now if status in {"completed", "failed", "skipped"} else None, next_attempt_at, code, (message or "")[:500] or None, request_id))

    def _monthly_cost(self, connection, now: datetime) -> float:
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        rows = connection.execute(
            "SELECT provider_usage_json FROM research_artifacts WHERE generated_at >= ?",
            (month_start,),
        ).fetchall()
        return sum(float((json.loads(row[0]) if row[0] else {}).get("cost_usd", 0)) for row in rows)

    def _cost_usage(self, usage: dict) -> dict:
        input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
        output_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
        cost = input_tokens * self.settings.LLM_INPUT_COST_PER_1K_USD / 1000 + output_tokens * self.settings.LLM_OUTPUT_COST_PER_1K_USD / 1000
        return {"provider_usage": usage, "input_tokens": input_tokens, "output_tokens": output_tokens,
                "cost_usd": cost, "pricing_version": self.settings.LLM_PRICING_VERSION}

    def _persist_artifact(self, connection, request_id: str, report: dict, packet: dict, completion, now: datetime) -> None:
        artifact_id = str(uuid4())
        usage = self._cost_usage(completion.usage)
        connection.execute("""INSERT INTO research_artifacts VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            artifact_id, request_id, self.settings.LLM_PROVIDER, self.settings.LLM_MODEL, PROMPT_VERSION, now,
            report["verdict"], canonical_json(report), canonical_json(packet), canonical_json(usage),
        ))
        sources = _evidence_map(packet)
        cited = {citation for claim in report["claims"] for citation in claim["evidence_ids"]}
        cited.update(citation for risk in report["risks"] for citation in risk["evidence_ids"])
        for evidence_id in cited:
            evidence = sources[evidence_id]
            connection.execute("""INSERT INTO research_evidence VALUES (?, ?, ?, ?, ?, ?, ?)""", (
                str(uuid4()), artifact_id, evidence["source_type"], evidence["source_ref"], None, now,
                canonical_json(evidence["value"])[:1000],
            ))

    def _record_metrics(self, connection, now: datetime, results: dict[str, int]) -> None:
        queue_depth, oldest = connection.execute("""SELECT COUNT(*), MIN(created_at) FROM research_requests
            WHERE status = 'pending'""").fetchone()
        oldest_report = connection.execute("SELECT MIN(generated_at) FROM research_artifacts").fetchone()[0]
        durations = connection.execute(
            "SELECT completed_at, started_at FROM research_requests WHERE completed_at >= ? AND started_at IS NOT NULL",
            (now - timedelta(days=1),),
        ).fetchall()
        latency = sum((completed - started).total_seconds() for completed, started in durations) / len(durations) if durations else None
        connection.execute("INSERT INTO research_run_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            str(uuid4()), now, queue_depth, (now - oldest).total_seconds() if oldest else None,
            self._monthly_cost(connection, now), results["completed"], results["rejected"], latency,
            (now - oldest_report).total_seconds() if oldest_report else None,
        ))

    def process(self, connection) -> dict[str, int]:
        now = self.now()
        results = {"completed": 0, "failed": 0, "skipped": 0, "retried": 0, "recovered": self._recover_stale(connection, now), "rejected": 0}
        for _ in range(self.settings.LLM_MAX_REPORTS_PER_CYCLE):
            claim = self._claim(connection, self.now())
            if claim is None:
                break
            request_id, alpha_id, request_kind, as_of, digest, attempts, stored_input = claim
            now = self.now()
            if not self.settings.LLM_RESEARCH_ENABLED:
                self._finish(connection, request_id, "skipped", now, "disabled", "LLM research is disabled")
                results["skipped"] += 1
                continue
            event = connection.execute("SELECT valid_until FROM alpha_events WHERE alpha_id = ?", (alpha_id,)).fetchone()
            valid_until = event[0] if event is not None else None
            if isinstance(valid_until, str):
                valid_until = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
            if valid_until is not None and valid_until.tzinfo is None:
                valid_until = valid_until.replace(tzinfo=timezone.utc)
            if event is None or (request_kind == "event_review" and valid_until <= now):
                self._finish(connection, request_id, "skipped", now, "stale_subject", "event is unavailable or expired")
                results["skipped"] += 1
                continue
            if self.settings.LLM_MONTHLY_BUDGET_USD <= 0 or self._monthly_cost(connection, now) >= self.settings.LLM_MONTHLY_BUDGET_USD:
                self._finish(connection, request_id, "skipped", now, "budget_limit", "monthly LLM budget is unavailable")
                results["skipped"] += 1
                continue
            try:
                packet = json.loads(stored_input) if stored_input else build_event_review(connection, request_id, alpha_id, as_of, self.settings.LLM_MAX_INPUT_CHARS)
                if input_hash(packet) != digest:
                    raise ValueError("stored request input no longer reproduces its input hash")
                client = self.client or configured_client(self.settings)
                completion = client.complete(SYSTEM_PROMPT, task_prompt(packet.get("question")), canonical_json(packet))
                report = validate_event_review_output(completion.output, packet, self.settings.LLM_MAX_OUTPUT_CHARS)
                # Finalize the request before adding its immutable child artifact.
                self._finish(connection, request_id, "completed", now)
                self._persist_artifact(connection, request_id, report, packet, completion, now)
                results["completed"] += 1
            except ClientConfigurationError as error:
                self._finish(connection, request_id, "failed", now, "configuration_error", str(error))
                results["failed"] += 1
            except ValueError as error:
                self._finish(connection, request_id, "failed", now, "invalid_response", str(error))
                results["failed"] += 1
                results["rejected"] += 1
            except (httpx.HTTPError, TimeoutError, OSError) as error:
                if attempts <= self.settings.LLM_MAX_RETRIES:
                    delay = self.settings.LLM_RETRY_BASE_SECONDS * (2 ** (attempts - 1))
                    self._finish(connection, request_id, "pending", now, "provider_error", str(error), now + timedelta(seconds=delay))
                    results["retried"] += 1
                else:
                    self._finish(connection, request_id, "failed", now, "retries_exhausted", str(error))
                    results["failed"] += 1
        self._record_metrics(connection, self.now(), results)
        return results


def latest_event_report(connection, alpha_id: str) -> dict | None:
    """Return only a persisted, already validated report for publisher rendering."""
    row = connection.execute("""SELECT a.report_json FROM research_artifacts a
        JOIN research_requests r ON r.request_id = a.request_id
        WHERE r.subject_type = 'alpha_event' AND r.subject_id = ? AND r.request_kind = 'event_review' AND r.status = 'completed'
        ORDER BY a.generated_at DESC LIMIT 1""", (alpha_id,)).fetchone()
    return json.loads(row[0]) if row else None


def event_review_status(connection, alpha_id: str) -> str | None:
    """Return the one idempotent event-review state that controls delivery."""
    row = connection.execute("""SELECT status FROM research_requests
        WHERE subject_type = 'alpha_event' AND subject_id = ? AND request_kind = 'event_review'
        ORDER BY created_at DESC LIMIT 1""", (alpha_id,)).fetchone()
    return row[0] if row else None
