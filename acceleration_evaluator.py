"""Daemon that emits continuation research events from warmed data."""

import sys
import time

import config
from alpha_evaluator import EVALUATOR_INTERVAL_SECONDS, MINIMUM_SCORE, acceleration_candidates, event_from_candidate
from alpha_outbox import write_event


def run_once():
    conn = config.get_db_connection(read_only=True)
    try:
        candidates = acceleration_candidates(conn)
    finally:
        conn.close()
    for candidate in candidates:
        if candidate["score"] < MINIMUM_SCORE:
            continue
        created, path = write_event(event_from_candidate(candidate, "acceleration"))
        print(f"{'Emitted' if created else 'Deduplicated'} acceleration event: {path.name}")


def main():
    while True:
        try:
            run_once()
        except Exception as error:
            print(f"Acceleration evaluator error: {error}", file=sys.stderr)
        time.sleep(EVALUATOR_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
