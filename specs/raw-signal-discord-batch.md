# Raw Signal Discord Batch

## Status

Proposed implementation specification, agreed in design on 2026-08-29.

This specification adds a 30-minute Discord batch for raw strategy candidates.
It does not change hard admission, scoring, clash resolution, or executor intent
delivery. The batch is observational and must never delay, suppress, or mutate a
true trade intent.

## Goal

Capture what the live strategies produced before hard SL/RR admission and before
multi-strategy scoring, then publish one compact Discord summary every 30
minutes. This creates an auditable view of raw strategy behavior without
creating one Discord message per candidate.

## Non-Goals

- Do not treat a raw signal as an admitted alpha event.
- Do not send raw signals to the executor.
- Do not make Discord availability part of the trading path.
- Do not delay executor intent delivery until the 30-minute boundary.
- Do not let raw-signal volume influence strategy scores or clash resolution.
- Do not run an additional strategy evaluation pass solely for Discord.

## Runtime Flow

```text
completed cutoff
      |
      v
strategy plugins
      |
      +--> raw signal ledger/outbox --> 30m Discord batch
      |
      v
hard SL/RR admission
      |
      v
score and clash resolution
      |
      +--> alpha ledger / advisory channels
      +--> immediate bybit / hyro intent, when selected
```

The raw capture and Discord batch are side effects of the same deterministic
evaluation result. They must not invoke the strategies a second time.

## Cadence

### Window

Use fixed UTC half-hour windows:

```text
[00:00, 00:30)
[00:30, 01:00)
...
```

The window key is the RFC 3339 UTC start time, for example:

```text
2026-08-29T05:30:00Z
```

### Trigger

The batch publisher may be triggered by:

- the existing orchestrator loop after evaluation, or
- a dedicated lightweight oxmgr publisher process.

The preferred implementation is a dedicated publisher loop reading the durable
raw ledger, because Discord outages and webhook latency must not affect the
orchestrator or executor path.

There must be only one active batch publisher for the configured Discord
destination. Delivery claiming must make duplicate launches safe.

### Boundary behavior

At each boundary, publish the completed preceding window. A candidate belongs
to a window by its normalized `observed_at`, not by when its file was noticed.

Late candidates are included in the next unclaimed batch and retain their
original `observed_at`. A claimed batch is immutable; late arrivals must not
rewrite it.

## Raw Signal Record

Raw candidates require a durable record before any batch can reference them.

Minimum fields:

```text
raw_signal_id       deterministic candidate identity
candidate_id        strategy candidate identity
strategy_id
plugin_version
asset
direction
observed_at
valid_until
entry_condition
invalidation_price
targets
raw_confidence
confidence_status
feature_snapshot
source_evidence_ids
created_at
```

The record must also contain the current downstream status when known:

```text
hard_gate_status       pending | pass | fail
hard_gate_reasons
score_status           pending | scored
clash_status           pending | selected | suppressed | conflict
executor_intent_status not_eligible | not_selected | written | failed
```

Raw records are append-only. Downstream status changes are recorded either in a
status-history table or as immutable batch updates with timestamps.

## Persistence Ownership

Raw strategy records and batch delivery state belong in `analyst.sqlite3`.
The market database remains owned by `ws_gateway` and is read-only to this
feature.

Recommended tables:

```sql
raw_signals(
  raw_signal_id PRIMARY KEY,
  candidate_id NOT NULL,
  strategy_id NOT NULL,
  asset NOT NULL,
  direction NOT NULL,
  observed_at NOT NULL,
  valid_until NOT NULL,
  payload_json NOT NULL,
  created_at NOT NULL
)

raw_signal_status_history(
  status_id PRIMARY KEY,
  raw_signal_id NOT NULL,
  hard_gate_status,
  score_status,
  clash_status,
  executor_intent_status,
  reason,
  recorded_at NOT NULL
)

discord_signal_batches(
  window_start PRIMARY KEY,
  window_end NOT NULL,
  status NOT NULL,
  candidate_count NOT NULL,
  message_count NOT NULL,
  claimed_at,
  sent_at,
  response_body,
  error_message
)
```

Use `INSERT ... ON CONFLICT DO NOTHING` for raw identity and a claim lease for
batch delivery. Never use Discord message IDs as the identity of a raw signal.

## Evaluation Integration

The orchestrator must capture a raw record immediately after a plugin returns a
candidate and before hard admission or clash resolution changes its status.

```text
candidate returned
  -> persist raw_signals
  -> continue synchronously to admission
  -> continue synchronously to scoring
  -> write selected intent immediately if eligible
```

Raw persistence must be short and local. Discord is never called from this
critical section.

If raw persistence fails, log the failure and continue the normal admission,
scoring, and executor path. The failure must be visible in operational metrics,
but it is not an execution failure.

If admission or scoring fails, the raw record must still be retained with the
appropriate failure status. A plugin failure must not prevent other plugins from
being captured or evaluated.

## Discord Message

One batch message represents one UTC window. It should be compact enough for
Discord limits and deterministic enough to reproduce from the ledger.

Suggested structure:

```text
RAW STRATEGY SIGNALS
Window: 2026-08-29 05:30-06:00 UTC
Status: observation only; not execution-authorized

BTC
  LONG  failed-break-v3  entry=... stop=... target=...
  LONG  bb-rsi-meanrev-v1  entry=... stop=... target=...

ETH
  SHORT ema9-continuation-stochrsi-v1 entry=... stop=... target=...

Totals: 3 raw | 2 hard-gate pass | 1 hard-gate fail
Clashes: 1 same-direction group | 0 opposite-direction conflicts
Selected intents: 1 routed to bybit / hyro
```

The message must label status per candidate where useful:

- `RAW`
- `HARD-FAIL`
- `ELIGIBLE`
- `SELECTED`
- `SUPPRESSED`
- `CONFLICT`

Do not include secrets, credentials, internal file paths, or full unbounded
feature snapshots. Truncate reasons and serialize values consistently.

## Delivery Independence

The two delivery paths have separate failure domains:

```text
raw ledger -> Discord batch

eligible selected candidate -> intent outbox -> bybit / hyro executor
```

Rules:

1. Discord webhook timeout cannot delay intent writing.
2. Discord rate limiting cannot cause intent retry or suppression.
3. Intent write failure cannot prevent raw batch publication.
4. A batch retry cannot duplicate an intent.
5. A Discord retry cannot re-run strategy evaluation.
6. The batch publisher must not acquire a long-lived write lock while sending.

The orchestrator should commit candidate and intent state before handing work
to the batch publisher. The publisher reads committed state using short SQLite
transactions.

## Batch Claim and Retry

Batch delivery follows this state machine:

```text
pending -> claimed -> sent
                    \-> failed -> pending/retry
```

- Claim with a lease before sending.
- Reclaim expired leases after `RAW_BATCH_CLAIM_LEASE_SECONDS`.
- Use bounded exponential retry for transient HTTP failures.
- Stop after `RAW_BATCH_MAX_ATTEMPTS` and retain the failed batch for inspection.
- Never create a second batch row for the same `window_start`.

The rendered message should be generated from the immutable ledger records at
claim time and stored as a hash or payload snapshot for audit.

## Configuration

Recommended configuration:

```dotenv
RAW_SIGNAL_DISCORD_BATCH_ENABLED=true
RAW_SIGNAL_DISCORD_BATCH_MINUTES=30
RAW_SIGNAL_DISCORD_WEBHOOK_URL=${DISCORD_ALPHA_WEBHOOK_URL}
RAW_BATCH_CLAIM_LEASE_SECONDS=120
RAW_BATCH_MAX_ATTEMPTS=5
```

The 30-minute interval must be validated as a positive divisor of 60 in the
first rollout. The raw batch and admitted alpha message may use different
webhooks or channels, but a shared webhook is acceptable when message labels
make the distinction unambiguous.

## Operational Metrics

Expose at least:

- raw candidates captured per window
- raw persistence failures
- pending/claimed/sent/failed batches
- Discord latency and HTTP status
- oldest unbatched candidate age
- hard-gate pass/fail totals
- selected/suppressed/conflict totals
- executor intents written independently of batch status

Health must report raw-batch lag separately from market freshness and evaluator
freshness. A stale Discord batch must not mark market ingestion or evaluation
unhealthy if those paths are current.

## Test Requirements

Tests must prove:

1. Every returned candidate is captured before admission status changes.
2. Raw capture preserves candidates that fail SL/RR admission.
3. Raw capture preserves candidates suppressed by clash resolution.
4. Two candidates in one 30-minute window produce one batch, not two messages.
5. Candidates on opposite sides of a UTC half-hour boundary are separated.
6. Late candidates do not mutate a claimed batch.
7. Duplicate publisher runs cannot send the same batch twice.
8. Discord timeout does not prevent a selected Hyro intent from being written.
9. Intent write failure does not prevent raw batch persistence.
10. A batch retry never re-runs strategies or creates another intent.
11. Empty windows are either skipped deterministically or recorded explicitly.
12. Message output is bounded, labeled as observational, and reproducible.
13. Discord batch lag is reported independently from ingestion/evaluation health.

## Rollout

1. Add schema and raw capture with Discord delivery disabled.
2. Verify raw candidate counts against plugin results for several cycles.
3. Enable batch rendering to a test webhook or private channel.
4. Verify executor intent timestamps and counts are unchanged with Discord
   unavailable.
5. Enable the production webhook.
6. Keep individual admitted Discord signal delivery separately configurable;
   enabling raw batching must not implicitly disable or duplicate it.

## Related Documents

- `specs/trade-admission-and-clash-resolution.md`
- `specs/adr-strategy-confluence-scoring.md`
- `specs/research-to-bot-execution-adapter.md`
- `specs/llm-position-sidecar.md`
- `README.md`
- `agent.md`
