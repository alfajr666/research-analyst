"""Daemon that emits pre-breakout ignition research events from warmed data."""

import sys
import time

import config
from alpha_evaluator import EVALUATOR_INTERVAL_SECONDS, MINIMUM_SCORE, event_from_candidate, ignition_candidates
from alpha_outbox import write_event


def run_once():
    conn = config.get_db_connection(read_only=True)
    try:
        candidates = ignition_candidates(conn)
    finally:
        conn.close()
    for candidate in candidates:
        if candidate["score"] < MINIMUM_SCORE:
            continue
        created, path = write_event(event_from_candidate(candidate, "ignition"))
        print(f"{'Emitted' if created else 'Deduplicated'} ignition event: {path.name}")


def main():
    while True:
        try:
            run_once()
        except Exception as error:
            print(f"Ignition evaluator error: {error}", file=sys.stderr)
        time.sleep(EVALUATOR_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
