# Agent Notes

## Runtime Contract

- The WebSocket gateway owns `data/market.sqlite3` and publishes completed 5m
  evaluation triggers.
- The orchestrator consumes one trigger per completed 5m cutoff and writes
  selected intents to the shared executor inbox.
- Market freshness is a hard admission gate. Missing data or data older than
  `DATA_FRESHNESS_MAX_SECONDS` (default 600) rejects the candidate.
- Health freshness queries must exclude open/future bars with `source_end <= now`.
- The scorer ranks candidates that pass all hard gates; it is not another gate.
- Never point both services at one database or run duplicate database writers.

## Verification

Run `python3 -m pytest -q` and `git diff --check` before committing. Production
services are managed with `oxmgr`; do not start duplicate gateway or orchestrator
processes manually.
