"""Read-only operator CLI for persisted local research requests and artifacts."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import config
from research_workflow import queue_question, run_approved_experiment


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect or request bounded local research")
    parser.add_argument("action", choices=("event", "candidate", "question", "experiment"))
    parser.add_argument("subject_id", help="Event/candidate ID or strategy ID for experiment")
    parser.add_argument("detail", nargs="?", help="Question text or approved experiment name")
    parser.add_argument("--since", help="Inclusive UTC ISO timestamp")
    parser.add_argument("--until", help="Exclusive UTC ISO timestamp")
    args = parser.parse_args()
    if args.action == "question":
        if not args.detail:
            parser.error("question requires question text")
        config.init_db()
        connection = config.get_db_connection()
        try:
            created, digest = queue_question(
                connection, args.subject_id, args.detail, datetime.now(timezone.utc),
                config.LLM_RESEARCH_ENABLED, config.LLM_MAX_INPUT_CHARS,
            )
        finally:
            connection.close()
        print(json.dumps({"created": created, "input_hash": digest}))
        return
    if args.action == "experiment":
        if not args.detail:
            parser.error("experiment requires an approved experiment name")
        connection = config.get_db_connection(read_only=True)
        try:
            print(json.dumps(run_approved_experiment(connection, args.detail, args.subject_id), indent=2))
        finally:
            connection.close()
        return
    connection = config.get_db_connection(read_only=True)
    try:
        clauses = ["r.subject_type = ?", "r.subject_id = ?"]
        values = ["alpha_event" if args.action == "event" else "candidate", args.subject_id]
        if args.since:
            clauses.append("a.generated_at >= CAST(? AS TIMESTAMP WITH TIME ZONE)")
            values.append(args.since)
        if args.until:
            clauses.append("a.generated_at < CAST(? AS TIMESTAMP WITH TIME ZONE)")
            values.append(args.until)
        rows = connection.execute(f"""SELECT a.generated_at, a.verdict, a.report_json FROM research_artifacts a
            JOIN research_requests r ON r.request_id = a.request_id
            WHERE {' AND '.join(clauses)} ORDER BY a.generated_at DESC""", values).fetchall()
    finally:
        connection.close()
    print(json.dumps([{"generated_at": row[0].isoformat(), "verdict": row[1], "report": json.loads(row[2])} for row in rows], indent=2))


if __name__ == "__main__":
    main()
