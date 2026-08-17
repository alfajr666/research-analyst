"""Human-triggered questions and allowlisted deterministic research experiments."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from research_contracts import canonical_json, input_hash
from research_context import build_event_review


MAX_QUESTION_CHARS = 600
APPROVED_EXPERIMENTS = {"descriptive_outcome_summary"}


def queue_question(connection, alpha_id: str, question: str, as_of: datetime, enabled: bool, max_input_chars: int = 24000) -> tuple[bool, str]:
    """Persist an explicit operator question; it never gives the model tool choice."""
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
        raise ValueError("question must be a non-empty string up to 600 characters")
    request_id = str(uuid4())
    packet = build_event_review(connection, request_id, alpha_id, as_of, max_input_chars)
    packet["request_kind"] = "research_question"
    packet["question"] = question.strip()
    serialized = canonical_json(packet)
    digest = input_hash(packet)
    status = "pending" if enabled else "skipped"
    row = connection.execute("""INSERT INTO research_requests (request_id, subject_type, subject_id, request_kind, as_of, input_hash, status, created_at, completed_at, error_code, request_input_json)
        VALUES (?, 'alpha_event', ?, 'research_question', ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (subject_type, subject_id, request_kind, input_hash) DO NOTHING RETURNING request_id""",
        (request_id, alpha_id, as_of, digest, status, as_of, as_of if not enabled else None, "disabled" if not enabled else None, serialized)).fetchone()
    return row is not None, digest


def run_approved_experiment(connection, experiment: str, strategy_id: str) -> dict:
    """Run only a named, read-only aggregate; arbitrary SQL is intentionally absent."""
    if experiment not in APPROVED_EXPERIMENTS:
        raise ValueError("experiment is not pre-approved")
    row = connection.execute("""SELECT COUNT(*), AVG(o.net_return),
        SUM(CASE WHEN o.outcome = 'target' THEN 1 ELSE 0 END)
        FROM alpha_candidates c JOIN alpha_outcomes o USING (candidate_id)
        WHERE c.strategy_id = ?""", (strategy_id,)).fetchone()
    return {"experiment": experiment, "strategy_id": strategy_id,
            "sample_count": row[0], "average_net_return": row[1], "target_count": row[2],
            "label": "descriptive history, not probability calibration"}
