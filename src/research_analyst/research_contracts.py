"""Pure schemas and canonical serialization for bounded research artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import re


REPORT_SCHEMA_VERSION = 1
VERDICTS = {"support", "neutral", "contradict", "insufficient_evidence"}
STANCES = {"support", "contradict", "uncertain"}
RISK_TYPES = {"data_quality", "crowding", "regime", "extension", "liquidity", "other"}
SEVERITIES = {"low", "medium", "high"}
_PROHIBITED = re.compile(r"\b(guaranteed|certain|leverage|position sizing|execute|execution|buy|sell)\b", re.I)
_PROBABILITY = re.compile(r"\b(probability|\d+(?:\.\d+)?\s*%)\b", re.I)
_SENSITIVE_KEY = re.compile(r"(api[_-]?key|secret|password|credential|authorization|access[_-]?token)", re.I)


def canonical_json(value: object) -> str:
    """Serialize a replayable packet, rejecting non-finite numeric evidence."""
    def reject_non_finite(item: object) -> object:
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("research input contains a non-finite number")
        if isinstance(item, dict):
            return {str(key): reject_non_finite(value) for key, value in item.items()}
        if isinstance(item, list):
            return [reject_non_finite(value) for value in item]
        return item
    return json.dumps(reject_non_finite(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def reject_sensitive_keys(value: object) -> None:
    """Keep credentials out of the canonical packet before any provider call."""
    if isinstance(value, dict):
        for key, child in value.items():
            if _SENSITIVE_KEY.search(str(key)):
                raise ValueError("research input contains a sensitive key")
            reject_sensitive_keys(child)
    elif isinstance(value, list):
        for child in value:
            reject_sensitive_keys(child)


def input_hash(packet: dict) -> str:
    return hashlib.sha256(canonical_json(packet).encode("utf-8")).hexdigest()


def evidence_ids(packet: dict) -> set[str]:
    """Return all evidence IDs present in a canonical input packet."""
    found: set[str] = set()
    def walk(value: object) -> None:
        if isinstance(value, dict):
            identifier = value.get("evidence_id")
            if isinstance(identifier, str):
                found.add(identifier)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(packet)
    return found


def _bounded_text(value: object, limit: int, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"{field} must be a non-empty string up to {limit} characters")
    if _PROHIBITED.search(value) or _PROBABILITY.search(value):
        raise ValueError(f"{field} contains prohibited policy language")
    return value


def _citations(value: object, known_ids: set[str], field: str) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 12 or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a bounded non-empty evidence ID list")
    if any(item not in known_ids for item in value):
        raise ValueError(f"{field} references unknown evidence")
    return value


def validate_event_review_output(value: str | dict, packet: dict, max_chars: int) -> dict:
    """Strictly validate an untrusted provider result before it reaches storage."""
    if isinstance(value, str):
        if len(value) > max_chars:
            raise ValueError("provider output exceeds configured bound")
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("provider output is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("provider output must be a JSON object")
    expected = {"schema_version", "verdict", "thesis_summary", "claims", "risks", "limitations", "operator_questions"}
    if set(value) != expected or value.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise ValueError("provider output does not match the report schema")
    if value["verdict"] not in VERDICTS:
        raise ValueError("unknown verdict")
    known_ids = evidence_ids(packet)
    report = {"schema_version": REPORT_SCHEMA_VERSION, "verdict": value["verdict"],
              "thesis_summary": _bounded_text(value["thesis_summary"], 600, "thesis_summary")}
    claims = value["claims"]
    if not isinstance(claims, list) or len(claims) > 12:
        raise ValueError("claims must be a bounded list")
    report["claims"] = []
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {"claim", "stance", "evidence_ids"} or claim.get("stance") not in STANCES:
            raise ValueError("invalid claim")
        report["claims"].append({"claim": _bounded_text(claim["claim"], 400, "claim"), "stance": claim["stance"],
                                 "evidence_ids": _citations(claim["evidence_ids"], known_ids, "claim evidence_ids")})
    risks = value["risks"]
    if not isinstance(risks, list) or len(risks) > 10:
        raise ValueError("risks must be a bounded list")
    report["risks"] = []
    for risk in risks:
        if not isinstance(risk, dict) or set(risk) != {"type", "severity", "detail", "evidence_ids"} or risk.get("type") not in RISK_TYPES or risk.get("severity") not in SEVERITIES:
            raise ValueError("invalid risk")
        report["risks"].append({"type": risk["type"], "severity": risk["severity"],
                                "detail": _bounded_text(risk["detail"], 300, "risk detail"),
                                "evidence_ids": _citations(risk["evidence_ids"], known_ids, "risk evidence_ids")})
    for field in ("limitations", "operator_questions"):
        values = value[field]
        if not isinstance(values, list) or len(values) > 10:
            raise ValueError(f"{field} must be a bounded list")
        report[field] = [_bounded_text(item, 300, field) for item in values]
    return report
