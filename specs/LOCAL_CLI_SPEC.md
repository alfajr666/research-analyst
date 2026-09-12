# Research Analyst Local CLI

**Status:** LOCKED
**Version:** 1
**Date:** 2026-09-10

## 1. Purpose

`research-analyst/cli.py` is the local operator and agent interface for the
Research Analyst repository. It exposes research state, pipeline health, and
validated delivery state as machine-readable JSON. It does not become a
strategy engine, executor, portfolio manager, or second worker process.

The CLI is a local module owned by this repository. It must not import or
control the implementation of Bybit, Propr, Binance Scanner, or Thalex.

## 2. Invocation

```bash
./cli.py <command> [options]
```

The script is executable and may be run from any working directory. Paths are
resolved from the repository root and then overridden by the same environment
variables used by the workers.

The CLI is enabled by default. Set `RESEARCH_ANALYST_LOCAL_CLI_ENABLED=false`
to refuse all commands, including service controls.

All commands emit JSON by default. `--pretty` changes indentation only. Human
diagnostics go to stderr; stdout is reserved for the result envelope.

## 3. Common Result Contract

```json
{
  "ok": true,
  "command": "research.candidates",
  "observed_at": "2026-09-10T10:40:00Z",
  "data": {},
  "warnings": [],
  "provenance": {
    "service": "research-analyst",
    "source": "data/analyst.sqlite3",
    "fresh": true,
    "observed_at": "2026-09-10T10:39:58Z"
  }
}
```

Rules:

- Timestamps are UTC ISO-8601 strings ending in `Z`.
- Normalized CLI fields use `snake_case`; source contract values remain exact.
- Missing data is `null` or an explicit `unknown`, never an invented zero.
- Stale health or venue-independent data sets `fresh: false` and adds a warning.
- Results are bounded by `--limit`; the default is 50 and the maximum is 500.
- Sensitive keys and values are redacted before serialization.
- Database reads use read-only connections and never run migrations or `VACUUM`.

Exit codes:

| Code | Meaning |
|---:|---|
| 0 | Successful result, including an empty valid result |
| 1 | Invalid command or argument |
| 2 | Requested record was not found |
| 3 | Required source is unavailable or stale |
| 4 | Safety policy refused the requested action |
| 5 | Unexpected CLI failure |

## 4. Commands

### 4.1 `status`

Returns the Research Analyst service inventory and the latest health state.
The inventory contains:

- `research-analyst-symbol-rotation`
- `research-analyst-ws`
- `research-analyst-regime-session`
- `research-analyst-strategy-runner`
- `research-analyst-orchestrator`

Each service includes process-manager status, health-file status where one
exists, last observed cycle, and a normalized stale reason. The strategy
runner has no health file; its health status and observation timestamp come
from the oxmgr health probe record. `status` does not start or restart anything.

### 4.2 `health`

Reads the orchestrator health artifact and `ws_health.json` without modifying
them, and reads the strategy-runner health state from oxmgr. It returns data
freshness, cutoff progress, regime readiness, pipeline counts, publisher state,
worker anomalies, and strategy-runner health. The command must distinguish:

```text
healthy      fresh health and no blocking anomaly
degraded     health exists but a blocking anomaly or stale component exists
unknown      the required health artifact cannot be read
```

### 4.3 `research candidates`

Reads bounded candidate rows from `alpha_candidates` and returns candidate
identity, asset, direction, strategy, evaluation cutoff, admission status, score
status, structural proof status, and timestamps. It must not label a candidate
as an executable trade unless the persisted admission contract says so.

Supported filters: `--asset`, `--strategy`, `--direction`, `--status`,
`--since`, `--limit`.

### 4.4 `research signals`

Reads `raw_signals`, status history, and delivery status. The result separates
raw candidate state, admission state, alpha event state, and executor delivery
state. A published intent is reported as `published`; it is never reported as
filled or open here.

### 4.5 `research signal <id>`

Returns one complete bounded signal record, including its admission proof,
cutoff, strategy identity, source evidence references, and target delivery
references. Sensitive or unbounded provider payloads are omitted.

### 4.6 `research reports` and `research report <id>`

Reads validated research reports and their evidence references from the
Research Analyst store. Reports remain research artifacts. The CLI must reject
wording or output that upgrades `support` into an execution instruction.

### 4.7 `research regime`

Reads the latest regime scores and gate decisions from the regime-owned
database. It returns readiness, active families, blocked assets, exact cutoff,
and provenance. It must show `shadow` blocks as hypothetical rather than
operational blocks.

### 4.8 `research watchlist`

Reads the persisted rotation feed and effective universe. It returns feed
identity, universe version, freshness, permanent assets, rotating assets,
expiry, and source status. It never edits the watchlist.

### 4.9 `bus deliveries`, `bus delivery <id>`, `bus receipts <id>`

These are read-only views of the shared intent bus. They may inspect both
targets but never claim, retry, complete, expire, or write receipts. Payloads
are redacted and bounded.

### 4.10 `service start|stop|restart|logs <name>`

Controls only the five registered Research Analyst oxmgr targets. `stop` and
`restart` require `--confirm` because they interrupt data collection. The CLI
must call `oxmgr`, not spawn worker Python modules directly.

### 4.11 `research publish <alpha-id>`

This action is intentionally excluded from version 1. Existing worker-owned
publishing remains authoritative. A future command may request publication only
for an already persisted, admitted alpha event and must use the existing
publisher idempotency path; it must never accept hand-authored trade geometry.

## 5. Data Ownership

| Source | Access | Rule |
|---|---|---|
| `data/market.sqlite3` | read-only | owned by `ws_gateway` |
| `data/regime.sqlite3` | read-only | owned by regime-session worker |
| `data/analyst.sqlite3` | read-only | owned by orchestrator |
| shared intent bus DB | read-only | inspect only; never claim |
| health JSON files | read-only | atomic snapshots are authoritative for freshness |

The CLI must not call `init_db(force_*)`, create tables, update status rows, or
open a writer connection for an observational command.

## 6. Safety Invariants

- Research output is never execution confirmation.
- The CLI cannot set leverage, size, account, stop, target, or execution mode.
- The CLI cannot publish an intent from an unadmitted or manually constructed
  candidate.
- Service controls cannot silently change `.env` or production settings.
- Health failures are visible as `unknown` or `degraded`, never healthy.

## 7. Verification

Add hermetic tests for envelope serialization, redaction, stale health,
read-only database access, candidate/report queries, bus inspection, service
allowlisting, and refusal of mutation commands. Required repository checks:

```bash
python3 -m pytest -q
python3 -m compileall -q src tests cli.py
```
