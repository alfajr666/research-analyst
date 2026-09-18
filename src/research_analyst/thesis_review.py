"""Independent LLM thesis review (specs/llm-thesis-review-v1.md, locked design).

One optional, fail-open review after deterministic admission, scoring, and
clash resolution, immediately before shared-bus publication. The reviewer is
an adversarial, veto-only thesis critic: it never rescues a deterministic
rejection, never mutates intent geometry, and never sizes or executes.

Blinding contract (spec 6.1): the review input carries point-in-time market
evidence and the strategy thesis only. Scorer totals, verdicts, thresholds,
weights, clash scores, margins, ranks, and publication labels are rejected at
construction so the model cannot anchor on the deterministic decision.

Application code derives the binary decision at the locked threshold
(``THESIS_REVIEW_PASS_THRESHOLD``); the model returns only a validated score.
Unavailable review is a recorded fail-open pass that never interrupts the
pipeline. The module stores no prompt, raw completion, or hidden reasoning.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

import config

FORBIDDEN_INPUT_KEYS = (
    "quality_score",
    "operational_score",
    "total_score",
    "score_verdict",
    "scorer_verdict",
    "score_threshold",
    "scorer_weights",
    "clash_score",
    "clash_margin",
    "clash_rank",
    "selected_for_publication",
    "publication_decision",
)

_FALLBACK_EXPLANATION = "LLM review unavailable; fail-open publication applied."
_EXPLANATION_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_MARKDOWN_RE = re.compile(r"```|\*\*|^#{1,6}\s", re.MULTILINE)
_EXECUTION_CLAIM_RE = re.compile(
    r"\b(order (placed|filled)|fill(ed)? (the )?(order|entry)|position opened|"
    r"executed (the )?(trade|order)|slippage)\b",
    re.IGNORECASE,
)
_PROMPT_TEMPLATE = (
    "You are an adversarial, veto-only thesis reviewer for one proposed trade "
    "intent. Answer one question: is there a concrete contradiction between the "
    "strategy thesis and the point-in-time evidence that makes this proposed "
    "intent unworthy of publication?\n\n"
    "Rubric (thesis_score):\n"
    "0-39: thesis clearly contradicted\n"
    "40-69: material contradiction or inadequate confirmation\n"
    "70-84: coherent thesis with no material contradiction\n"
    "85-100: strong confirmation across independent observations\n\n"
    "A low score requires one fatal contradiction or at least two independent "
    "material contradictions. Missing optional evidence alone is not a "
    "contradiction. Do not fabricate data.\n\n"
    "Return exactly one JSON object and nothing else:\n"
    '{{"schema_version": 1, "thesis_score": <integer 0-100>, '
    '"explanation": "<one line, max 180 characters>"}}\n\n'
    "Strategy: {strategy_id}\n"
    "Thesis: {strategy_thesis}\n"
    "Asset: {asset}\n"
    "Direction: {direction}\n"
    "Evaluation cutoff: {evaluation_cutoff}\n"
    "Entry: {entry}\n"
    "Stop: {stop}\n"
    "Target: {target}\n"
    "Reward/risk: {reward_risk}\n\n"
    "Point-in-time evidence (cutoff-bound, JSON):\n{evidence}"
)

PROVIDER_ERROR_CATEGORIES = frozenset({
    "timeout", "rate_limit", "provider_error", "parse_error",
    "serialization_error", "network_error", "circuit_open", "unexpected",
})


class ReviewInputError(ValueError):
    """A forbidden or malformed field was supplied to the review input."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def evidence_hash(evidence: Mapping[str, Any]) -> str:
    """Deterministic hash of cutoff-bound evidence (spec 16.3)."""
    return hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def build_review_input(
    candidate: Mapping[str, Any],
    *,
    strategy_thesis: str,
    evidence: Mapping[str, Any],
    provenance: Mapping[str, Any] | None = None,
    evaluation_cutoff: str | None = None,
) -> dict:
    """Build one immutable, blinded ThesisReviewInputV1 (spec 6).

    Forbidden scorer/clash conclusion fields anywhere in the candidate or
    evidence raise :class:`ReviewInputError` instead of reaching the prompt.
    """
    for container_name, container in (("candidate", candidate), ("evidence", evidence)):
        keys = set(container.keys())
        for forbidden in FORBIDDEN_INPUT_KEYS:
            if forbidden in keys:
                raise ReviewInputError(
                    f"{container_name} carries forbidden blinding field: {forbidden}"
                )
    strategy_id = str(candidate.get("strategy_id") or "").strip()
    asset = str(candidate.get("asset") or "").strip()
    direction = str(candidate.get("direction") or "").strip().lower()
    if not strategy_id or not asset or direction not in {"long", "short"}:
        raise ReviewInputError("review input requires strategy_id, asset, and direction")
    thesis = str(strategy_thesis or "").strip()
    if not thesis:
        raise ReviewInputError("strategy_thesis must be repository-owned text")
    observed_at = candidate.get("observed_at") or candidate.get("cutoff_at")
    cutoff = str(evaluation_cutoff or observed_at or "").strip()
    if not cutoff:
        raise ReviewInputError("review input requires an evaluation cutoff")
    entry = candidate.get("entry_price")
    if entry is None:
        entry = (candidate.get("entry_condition") or {}).get("price")
    stop = candidate.get("invalidation_price", candidate.get("stop_loss"))
    targets = candidate.get("targets") or []
    target = candidate.get("take_profit")
    if target is None and targets:
        first = targets[0]
        target = first.get("price") if isinstance(first, Mapping) else first
    entry, stop, target = float(entry), float(stop), float(target)
    if not all(math.isfinite(value) and value > 0 for value in (entry, stop, target)):
        raise ReviewInputError("geometry values must be finite and positive")
    risk = abs(entry - stop)
    if risk <= 0:
        raise ReviewInputError("stop must differ from entry")
    reward_risk = round(abs(target - entry) / risk, 6)
    fingerprint = str(
        candidate.get("candidate_fingerprint")
        or candidate.get("candidate_id")
        or ""
    ).strip()
    if not fingerprint:
        raise ReviewInputError("review input requires a candidate identity")
    for fact in evidence.get("observations", []) if isinstance(evidence, Mapping) else []:
        if isinstance(fact, Mapping):
            ts = fact.get("timestamp") or fact.get("source_end") or fact.get("observed_at")
            if ts is not None:
                raise ReviewInputError(
                    "evidence families must pre-normalize timestamps to the cutoff"
                )
    return {
        "schema_version": 1,
        "candidate_id": str(candidate.get("candidate_id") or candidate.get("dedupe_key") or ""),
        "candidate_fingerprint": fingerprint,
        "strategy_id": strategy_id,
        "strategy_thesis": thesis,
        "asset": asset,
        "direction": direction,
        "evaluation_cutoff": cutoff,
        "entry": entry,
        "stop": stop,
        "target": target,
        "reward_risk": reward_risk,
        "evidence": json.loads(json.dumps(evidence, sort_keys=True, default=str)),
        "provenance": json.loads(json.dumps(provenance or {}, sort_keys=True, default=str)),
    }


def build_prompt(review_input: Mapping[str, Any]) -> str:
    """Render the bounded review prompt from an already-validated input."""
    return _PROMPT_TEMPLATE.format(
        strategy_id=review_input["strategy_id"],
        strategy_thesis=review_input["strategy_thesis"],
        asset=review_input["asset"],
        direction=review_input["direction"],
        evaluation_cutoff=review_input["evaluation_cutoff"],
        entry=review_input["entry"],
        stop=review_input["stop"],
        target=review_input["target"],
        reward_risk=review_input["reward_risk"],
        evidence=json.dumps(
            review_input.get("evidence") or {}, sort_keys=True, default=str
        ),
    )


def parse_model_output(raw: Any) -> dict:
    """Validate the exact three-key model object (spec 8). Returns the dict."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        raise ValueError("model output must be a JSON string")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"model output is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("model output must be a JSON object")
    if set(payload.keys()) != {"schema_version", "thesis_score", "explanation"}:
        raise ValueError("model output key set must be exactly {schema_version, thesis_score, explanation}")
    if payload["schema_version"] != 1:
        raise ValueError("model output schema_version must be 1")
    score = payload["thesis_score"]
    if isinstance(score, bool) or not isinstance(score, int):
        raise ValueError("thesis_score must be an integer")
    if not 0 <= score <= 100:
        raise ValueError("thesis_score must be within 0..100")
    explanation = payload["explanation"]
    if not isinstance(explanation, str):
        raise ValueError("explanation must be a string")
    stripped = explanation.strip()
    if not stripped:
        raise ValueError("explanation must be non-empty")
    if len(stripped) > config.THESIS_REVIEW_MAX_EXPLANATION_CHARS:
        raise ValueError("explanation exceeds the 180-character limit")
    if "\n" in explanation or "\r" in explanation:
        raise ValueError("explanation must be one line")
    if _EXPLANATION_URL_RE.search(explanation) or _MARKDOWN_RE.search(explanation):
        raise ValueError("explanation must not contain URLs or Markdown blocks")
    if _EXECUTION_CLAIM_RE.search(explanation):
        raise ValueError("explanation must not claim execution")
    return {"schema_version": 1, "thesis_score": score, "explanation": stripped}


def derive_decision(thesis_score: int | None) -> tuple[str, str]:
    """Derive (decision, score_status); exactly 70 passes (spec 8)."""
    if thesis_score is None:
        return "pass", "unavailable"
    return ("pass" if thesis_score >= config.THESIS_REVIEW_PASS_THRESHOLD else "veto"), "uncalibrated"


def fallback_result(reason: str) -> dict:
    """Binary fail-open pass with a null score (spec 9). Never fabricates 70."""
    return {
        "decision": "pass",
        "thesis_score": None,
        "score_status": "unavailable",
        "explanation": _FALLBACK_EXPLANATION,
        "reviewed": False,
        "fallback_reason": str(reason or "unavailable")[:120],
    }


def review_thesis(review_input: Mapping[str, Any], reviewer, *, circuit: ThesisReviewCircuit | None = None) -> dict:
    """Deep module entry point: one validated review result (spec 4).

    ``reviewer`` is the injected provider adapter; it must expose
    ``complete(prompt: str, *, timeout_seconds: float) -> str`` and
    ``model_version: str``. Provider failure of any category produces the
    fail-open result; nothing here raises into the publication path.
    """
    started = time.monotonic()
    review_input = dict(review_input)
    review_input.setdefault("_model_version", getattr(reviewer, "model_version", None))
    prompt = build_prompt(review_input)
    evidence_digest = evidence_hash(review_input.get("evidence") or {})
    if circuit is not None and circuit.open:
        return _finalize(
            review_input, fallback_result("circuit_open"), evidence_digest, started
        )
    try:
        raw = reviewer.complete(prompt, timeout_seconds=config.THESIS_REVIEW_TIMEOUT_SECONDS)
        payload = parse_model_output(raw)
    except CircuitOpenError as exc:
        return _finalize(review_input, fallback_result(f"circuit_open: {exc}"), evidence_digest, started)
    except TimeoutError:
        return _finalize(review_input, fallback_result("timeout"), evidence_digest, started)
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        reason = str(exc)[:120] or exc.__class__.__name__
        return _finalize(review_input, fallback_result(reason), evidence_digest, started)
    result = {
        "decision": derive_decision(payload["thesis_score"])[0],
        "thesis_score": payload["thesis_score"],
        "score_status": "uncalibrated",
        "explanation": payload["explanation"],
        "reviewed": True,
        "fallback_reason": None,
    }
    return _finalize(review_input, result, evidence_digest, started)


def _finalize(review_input: Mapping[str, Any], result: dict, digest: str, started: float) -> dict:
    latency_ms = int((time.monotonic() - started) * 1000)
    return {
        "schema_version": 1,
        "review_id": _review_id(review_input, digest),
        "candidate_id": review_input["candidate_id"],
        "candidate_fingerprint": review_input["candidate_fingerprint"],
        "evaluation_cutoff": review_input["evaluation_cutoff"],
        "mode": config.LLM_THESIS_REVIEW_MODE,
        "decision": result["decision"],
        "thesis_score": result["thesis_score"],
        "score_status": result["score_status"],
        "explanation": result["explanation"],
        "reviewed": result["reviewed"],
        "fallback_reason": result.get("fallback_reason"),
        "model_version": review_input.get("_model_version"),
        "prompt_version": config.THESIS_REVIEW_PROMPT_VERSION,
        "review_policy_version": config.THESIS_REVIEW_POLICY_VERSION,
        "evidence_hash": digest,
        "latency_ms": latency_ms,
    }


def _review_id(review_input: Mapping[str, Any], digest: str) -> str:
    material = "|".join((
        review_input["candidate_fingerprint"], digest,
        config.THESIS_REVIEW_PROMPT_VERSION,
        str(review_input.get("_model_version") or ""),
        config.THESIS_REVIEW_POLICY_VERSION,
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class CircuitOpenError(RuntimeError):
    """The provider circuit breaker is open; no provider call is attempted."""


class ThesisReviewCircuit:
    """Versioned 3-failure / 15-minute circuit breaker (spec 9)."""

    def __init__(self, *, failure_threshold: int | None = None,
                 open_seconds: float | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.failure_threshold = int(
            failure_threshold or config.THESIS_REVIEW_CIRCUIT_FAILURES)
        self.open_seconds = float(
            open_seconds or config.THESIS_REVIEW_CIRCUIT_OPEN_SECONDS)
        self._clock = clock
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if self._clock() - self._opened_at >= self.open_seconds:
                self._opened_at = None
                self._consecutive_failures = 0
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._opened_at = self._clock()

    def state(self) -> dict:
        with self._lock:
            return {
                "open": self._opened_at is not None,
                "consecutive_failures": self._consecutive_failures,
                "open_seconds": self.open_seconds,
                "failure_threshold": self.failure_threshold,
            }


_REVIEWER_CACHE: dict[str, tuple[Any, ThesisReviewCircuit]] = {}
_REVIEWER_LOCK = threading.Lock()


def load_reviewer():
    """Return (adapter, circuit) for the configured provider, memoized."""
    key = "|".join((
        config.THESIS_REVIEW_PROVIDER, config.THESIS_REVIEW_BASE_URL,
        config.THESIS_REVIEW_MODEL,
    ))
    with _REVIEWER_LOCK:
        cached = _REVIEWER_CACHE.get(key)
        if cached is not None:
            return cached
        if config.THESIS_REVIEW_PROVIDER == "zai":
            from thesis_review_provider import ZaiThesisReviewer
            reviewer = ZaiThesisReviewer(
                api_key=config.THESIS_REVIEW_API_KEY,
                model=config.THESIS_REVIEW_MODEL,
                base_url=config.THESIS_REVIEW_BASE_URL,
            )
        elif config.THESIS_REVIEW_PROVIDER == "null":
            from thesis_review_provider import NullThesisReviewer
            reviewer = NullThesisReviewer()
        else:
            raise ValueError(
                f"unknown THESIS_REVIEW_PROVIDER: {config.THESIS_REVIEW_PROVIDER}"
            )
        entry = (reviewer, ThesisReviewCircuit())
        _REVIEWER_CACHE[key] = entry
        return entry


def review_selected_candidate(
    candidate: Mapping[str, Any],
    *,
    strategy_thesis: str,
    evidence: Mapping[str, Any],
    provenance: Mapping[str, Any] | None = None,
    db_path: str | None = None,
    reviewer: Any | None = None,
) -> dict | None:
    """Run one review for an already-selected candidate and persist it.

    Returns None when the feature is off. Fail-open results are recorded and
    published; nothing here may fail the evaluation cutoff (spec 9).
    """
    mode = config.LLM_THESIS_REVIEW_MODE
    if mode == "off":
        return None
    review_input = build_review_input(
        candidate,
        strategy_thesis=strategy_thesis,
        evidence=evidence,
        provenance=provenance,
    )
    if reviewer is None:
        adapter, circuit = load_reviewer()
    else:
        adapter, circuit = reviewer, ThesisReviewCircuit()
    review_input = {**review_input, "_model_version": getattr(adapter, "model_version", None)}
    result = review_thesis(review_input, adapter, circuit=circuit)
    fallback_reason = result.get("fallback_reason")
    if result["reviewed"]:
        circuit.record_success()
    elif not (isinstance(fallback_reason, str) and fallback_reason.startswith("circuit_open")):
        circuit.record_failure()
    persist_review(result, db_path=db_path)
    return result


def persist_review(result: Mapping[str, Any], db_path: str | None = None) -> bool:
    """Persist one compact review row (spec 11). Never raises."""
    try:
        import sqlite3

        target = db_path or config.ANALYST_DB_PATH
        conn = config.get_db_connection(db_path=target)
        try:
            conn.execute(
                """INSERT OR REPLACE INTO thesis_reviews (
                       review_id, candidate_id, candidate_fingerprint,
                       evaluation_cutoff, mode, decision, thesis_score,
                       score_status, explanation, reviewed, fallback_reason,
                       model_version, prompt_version, review_policy_version,
                       evidence_hash, latency_ms, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result["review_id"], result["candidate_id"],
                    result["candidate_fingerprint"], result["evaluation_cutoff"],
                    result["mode"], result["decision"], result["thesis_score"],
                    result["score_status"], result["explanation"],
                    1 if result["reviewed"] else 0, result.get("fallback_reason"),
                    result.get("model_version"), result["prompt_version"],
                    result["review_policy_version"], result["evidence_hash"],
                    result.get("latency_ms"), _utc_iso(_utc_now()),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001 - persistence must not fail the cycle
        print(f"thesis review persistence error: {exc}", file=sys.stderr)
        return False


def load_review(review_id: str, db_path: str | None = None) -> dict | None:
    """Reload a persisted review for a publisher retry (spec 10)."""
    try:
        conn = config.get_db_connection(read_only=True, db_path=db_path or config.ANALYST_DB_PATH)
        try:
            row = conn.execute(
                "SELECT review_id, candidate_id, candidate_fingerprint, evaluation_cutoff,"
                " mode, decision, thesis_score, score_status, explanation, reviewed,"
                " fallback_reason, model_version, prompt_version,"
                " review_policy_version, evidence_hash, latency_ms, created_at"
                " FROM thesis_reviews WHERE review_id = ?",
                (review_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    return {
        "schema_version": 1,
        "review_id": row[0], "candidate_id": row[1],
        "candidate_fingerprint": row[2], "evaluation_cutoff": row[3],
        "mode": row[4], "decision": row[5], "thesis_score": row[6],
        "score_status": row[7], "explanation": row[8],
        "reviewed": bool(row[9]), "fallback_reason": row[10],
        "model_version": row[11], "prompt_version": row[12],
        "review_policy_version": row[13], "evidence_hash": row[14],
        "latency_ms": row[15], "created_at": row[16],
    }


def review_metadata(result: Mapping[str, Any]) -> dict | None:
    """Versioned nested TradeIntent metadata (spec 12). None when off."""
    if result is None:
        return None
    return {
        "schema_version": 1,
        "review_id": result["review_id"],
        "mode": result["mode"],
        "enforced": result["mode"] == "enforce",
        "decision": result["decision"],
        "thesis_score": result["thesis_score"],
        "score_status": result["score_status"],
        "explanation": result["explanation"],
        "reviewed": bool(result["reviewed"]),
        "model_version": result.get("model_version"),
        "prompt_version": result["prompt_version"],
        "review_policy_version": result["review_policy_version"],
        "evidence_hash": result["evidence_hash"],
    }


def validate_review_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    candidate_id: str,
    review: Mapping[str, Any] | None = None,
    db_path: str | None = None,
) -> tuple[bool, str]:
    """Bus-handoff validation for nested ``metadata.thesis_review`` (spec 12)."""
    if metadata is None:
        return True, ""
    if not isinstance(metadata, dict) or metadata.get("schema_version") != 1:
        return False, "thesis review metadata schema is invalid"
    persisted = review or load_review(str(metadata.get("review_id")), db_path=db_path)
    if persisted is None:
        return False, "thesis review metadata does not resolve to a persisted review"
    if persisted["candidate_id"] != candidate_id:
        return False, "thesis review candidate identity is inconsistent"
    if metadata.get("review_id") != persisted["review_id"]:
        return False, "thesis review id is inconsistent"
    mode = metadata.get("mode")
    if mode not in {"shadow", "enforce"}:
        return False, "thesis review mode is invalid"
    if bool(metadata.get("enforced")) != (mode == "enforce"):
        return False, "thesis review enforcement flag is inconsistent"
    decision = metadata.get("decision")
    if decision not in {"pass", "veto"}:
        return False, "thesis review decision is invalid"
    score = metadata.get("thesis_score")
    if bool(metadata.get("reviewed")):
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
            return False, "thesis review score is invalid"
        if decision != derive_decision(score)[0]:
            return False, "thesis review decision contradicts its score"
    else:
        if score is not None or metadata.get("score_status") != "unavailable" or decision != "pass":
            return False, "fail-open thesis review metadata is inconsistent"
    if mode == "enforce" and decision != "pass":
        return False, "enforce-mode intents must carry a passing thesis review"
    if metadata.get("prompt_version") != persisted["prompt_version"] or \
            metadata.get("review_policy_version") != persisted["review_policy_version"]:
        return False, "thesis review policy provenance is inconsistent"
    if metadata.get("evidence_hash") != persisted["evidence_hash"]:
        return False, "thesis review evidence hash is inconsistent"
    if any(
        key in metadata
        for key in ("quantity", "amount", "risk_amount", "qty", "size")
    ):
        return False, "thesis review metadata must not carry sizing fields"
    return True, ""


def prune_reviews(conn, now: datetime | None = None, *, batch_size: int = 1000) -> int:
    """Bounded daily deletion of expired review rows (spec 11.1)."""
    now = now or _utc_now()
    limit = _utc_iso(now - __import__("datetime").timedelta(days=config.THESIS_REVIEW_RETENTION_DAYS))
    total = 0
    while True:
        cursor = conn.execute(
            "DELETE FROM thesis_reviews WHERE rowid IN ("
            "SELECT rowid FROM thesis_reviews WHERE created_at < ? LIMIT ?)",
            (limit, max(1, batch_size)),
        )
        deleted = max(int(cursor.rowcount or 0), 0)
        conn.commit()
        total += deleted
        if deleted < batch_size:
            break
    return total
